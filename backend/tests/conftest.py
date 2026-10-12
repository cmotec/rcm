"""Test fixtures.

* A real PostgreSQL (``pgserver`` starts a temporary instance) with all migrations applied.
* A fake guacd speaking the real Guacamole wire protocol.
* An in-process SSH server (asyncssh) with a shell and a chrooted SFTP subsystem.
* An in-process FTP server (pyftpdlib).
* One FastAPI ``TestClient`` (lifespan runs once) shared by all API tests.
"""

from __future__ import annotations

import asyncio
import base64
import os
import tempfile
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

ADMIN_USER = "admin"
ADMIN_PASSWORD = "admin-password-123"  # noqa: S105
SSH_USER, SSH_PASSWORD = "tester", "ssh-secret-pw"
FTP_USER, FTP_PASSWORD = "ftpuser", "ftp-secret-pw"


# --------------------------------------------------------------------------- background event loop


class LoopThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro: Any, timeout: float = 30) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


@pytest.fixture(scope="session")
def bg_loop() -> Iterator[LoopThread]:
    lt = LoopThread()
    yield lt
    lt.stop()


# --------------------------------------------------------------------------- fake guacd


@dataclass
class FakeGuacd:
    port: int = 0
    received: list[list[str]] = field(default_factory=list)
    connections: int = 0
    fail_with: str | None = None  # make the next handshake fail with this error message
    args: tuple[str, ...] = ("hostname", "port", "username", "password", "domain", "security", "ignore-cert",
                             "cert-tofu", "resize-method", "enable-drive", "drive-path", "read-only")

    def reset(self) -> None:
        self.received.clear()
        self.fail_with = None


async def _fake_guacd_handler(fake: FakeGuacd, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    from app.services.sessions.guacd_protocol import Parser, encode

    fake.connections += 1
    parser = Parser()
    pending: list[list[str]] = []

    async def read() -> list[str] | None:
        while not pending:
            data = await reader.read(65536)
            if not data:
                return None
            pending.extend(parser.feed(data.decode()))
        return pending.pop(0)

    try:
        instr = await read()
        if instr is None:
            return
        fake.received.append(instr)
        if fake.fail_with:
            writer.write(encode("error", fake.fail_with, "519").encode())
            await writer.drain()
            return
        writer.write(encode("args", "VERSION_1_5_0", *fake.args).encode())
        await writer.drain()
        while True:
            instr = await read()
            if instr is None:
                return
            fake.received.append(instr)
            if instr[0] == "connect":
                break
        writer.write((encode("ready", "$fake-connection") + encode("sync", "1000")).encode())
        await writer.drain()
        while True:
            instr = await read()
            if instr is None:
                return
            fake.received.append(instr)
            if instr[0] == "disconnect":
                return
            if instr[0] == "key":
                # echo something back so the test sees guacd → browser traffic
                writer.write(encode("clipboard", "0", "text/plain").encode())
                await writer.drain()
    finally:
        writer.close()


@pytest.fixture(scope="session")
def fake_guacd(bg_loop: LoopThread) -> FakeGuacd:
    fake = FakeGuacd()

    async def start() -> int:
        server = await asyncio.start_server(lambda r, w: _fake_guacd_handler(fake, r, w), "127.0.0.1", 0)
        return server.sockets[0].getsockname()[1]

    fake.port = bg_loop.run(start())
    return fake


# --------------------------------------------------------------------------- SSH / SFTP server


@dataclass
class SshServerInfo:
    port: int
    host_key_fingerprint: str
    root: Path
    commands: list[str] = field(default_factory=list)


@pytest.fixture(scope="session")
def ssh_server(bg_loop: LoopThread, tmp_path_factory: pytest.TempPathFactory) -> SshServerInfo:
    import asyncssh

    root = tmp_path_factory.mktemp("sftp-root")
    (root / "hello.txt").write_text("hello from sftp\n")
    (root / "docs").mkdir()
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    info = SshServerInfo(port=0, host_key_fingerprint=host_key.convert_to_public().get_fingerprint("sha256"),
                         root=root)

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username: str) -> bool:
            return True

        def password_auth_supported(self) -> bool:
            return True

        def validate_password(self, username: str, password: str) -> bool:
            return username == SSH_USER and password == SSH_PASSWORD

    async def handle_process(process: asyncssh.SSHServerProcess) -> None:
        process.stdout.write(f"welcome {process.get_extra_info('username')}\r\n")
        size = process.get_terminal_size()
        process.stdout.write(f"size {size[0]}x{size[1]}\r\n")
        while True:
            try:
                line = await process.stdin.readline()
            except asyncssh.TerminalSizeChanged as exc:
                process.stdout.write(f"resized {exc.width}x{exc.height}\r\n")
                continue
            except asyncssh.BreakReceived:
                break
            if not line:
                break
            line = line.rstrip("\r\n")
            info.commands.append(line)
            if line == "exit":
                break
            process.stdout.write(f"echo:{line}\r\n")
        process.exit(0)

    class ChrootSFTP(asyncssh.SFTPServer):
        def __init__(self, chan: asyncssh.SSHServerChannel) -> None:
            super().__init__(chan, chroot=str(root).encode())

    async def start() -> int:
        server = await asyncssh.listen(
            "127.0.0.1", 0, server_host_keys=[host_key], server_factory=Server,
            process_factory=handle_process, sftp_factory=ChrootSFTP, line_editor=False,
        )
        return server.sockets[0].getsockname()[1]

    info.port = bg_loop.run(start())
    return info


# --------------------------------------------------------------------------- FTP server


@dataclass
class FtpServerInfo:
    port: int
    root: Path


@pytest.fixture(scope="session")
def ftp_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[FtpServerInfo]:
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import ThreadedFTPServer

    root = tmp_path_factory.mktemp("ftp-root")
    (root / "readme.txt").write_text("ftp file\n")
    authorizer = DummyAuthorizer()
    authorizer.add_user(FTP_USER, FTP_PASSWORD, str(root), perm="elradfmwMT")

    class Handler(FTPHandler):
        pass

    Handler.authorizer = authorizer
    Handler.passive_ports = range(60000, 60100)
    server = ThreadedFTPServer(("127.0.0.1", 0), Handler)
    port = server.socket.getsockname()[1]
    t = threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.5}, daemon=True)
    t.start()
    yield FtpServerInfo(port=port, root=root)
    server.close_all()


# --------------------------------------------------------------------------- database + app


@pytest.fixture(scope="session")
def pg_uri() -> Iterator[str]:
    import pgserver

    data_dir = tempfile.mkdtemp(prefix="pgtest-")
    srv = pgserver.get_server(data_dir, cleanup_mode="stop")
    srv.psql("DROP DATABASE IF EXISTS apptest;")
    srv.psql("CREATE DATABASE apptest;")
    yield srv.get_uri()


@pytest.fixture(scope="session")
def settings(pg_uri: str, fake_guacd: FakeGuacd):  # noqa: ANN201
    from app.config import get_settings, reset_settings_cache

    u = urlsplit(pg_uri)
    os.environ.update({
        "ENVIRONMENT": "test",
        "APP_URL": "http://testserver",
        "LOG_LEVEL": "WARNING",
        "LOG_FORMAT": "console",
        "DB_HOST": u.hostname or "127.0.0.1",
        "DB_PORT": str(u.port),
        "DB_USER": u.username or "postgres",
        "DB_PASSWORD": u.password or "",
        "DB_NAME": "apptest",
        "DB_SSLMODE": "disable",
        "ENCRYPTION_KEYS": "k1:" + base64.b64encode(os.urandom(32)).decode(),
        "LOCAL_AUTH_ENABLED": "true",
        "LOCAL_ADMIN_USERNAME": ADMIN_USER,
        "LOCAL_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "COOKIE_SECURE": "false",
        "GUACD_HOST": "127.0.0.1",
        "GUACD_PORT": str(fake_guacd.port),
        "RATE_LIMIT_LOGIN_PER_MINUTE": "1000",
        "RATE_LIMIT_API_PER_MINUTE": "100000",
        "SECRET_SYNC_ENABLED": "true",
        "DNS_SEARCH_DOMAINS": "",
        "SSH_CONNECT_TIMEOUT_SECONDS": "10",
    })
    reset_settings_cache()
    return get_settings()


@pytest.fixture(scope="session")
def client(settings) -> Iterator[Any]:  # noqa: ANN001
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(settings)
    with TestClient(app, base_url="http://testserver") as c:
        yield c


class UserClient:
    """Requests as one user (cookie jar swapping on the shared TestClient)."""

    def __init__(self, client: Any, username: str, password: str) -> None:
        self.c = client
        self.username = username
        client.cookies.clear()
        r = client.post("/api/v1/auth/local/login", json={"username": username, "password": password})
        assert r.status_code == 204, r.text
        self.cookies = {k: v for k, v in client.cookies.items()}
        me = client.get("/api/v1/auth/me").json()
        self.csrf = me["csrf_token"]
        self.id = me["id"]

    def _prepare(self) -> None:
        self.c.cookies.clear()
        for k, v in self.cookies.items():
            self.c.cookies.set(k, v)

    def request(self, method: str, url: str, **kw: Any) -> Any:
        self._prepare()
        headers = dict(kw.pop("headers", {}) or {})
        headers.setdefault("X-CSRF-Token", self.csrf)
        return self.c.request(method, url, headers=headers, **kw)

    def get(self, url: str, **kw: Any) -> Any:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> Any:
        return self.request("POST", url, **kw)

    def put(self, url: str, **kw: Any) -> Any:
        return self.request("PUT", url, **kw)

    def patch(self, url: str, **kw: Any) -> Any:
        return self.request("PATCH", url, **kw)

    def delete(self, url: str, **kw: Any) -> Any:
        return self.request("DELETE", url, **kw)

    def websocket(self, url: str, **kw: Any) -> Any:
        self._prepare()
        headers = {"origin": "http://testserver", **(kw.pop("headers", {}) or {})}
        return self.c.websocket_connect(url, headers=headers, **kw)


@pytest.fixture(scope="session")
def admin(client: Any) -> UserClient:
    return UserClient(client, ADMIN_USER, ADMIN_PASSWORD)


@pytest.fixture(scope="session")
def make_user(admin: UserClient, client: Any) -> Callable[..., UserClient]:
    counter = {"n": 0}

    def _make(roles: list[str], password: str = "user-password-123") -> UserClient:  # noqa: S107
        counter["n"] += 1
        name = f"user{counter['n']}-{'-'.join(roles) or 'none'}"
        r = admin.post("/api/v1/users", json={"username": name, "password": password, "roles": roles})
        assert r.status_code == 201, r.text
        return UserClient(client, name, password)

    return _make


def run_in_app_loop(client: Any, coro_fn: Callable[[], Any]) -> Any:
    """Run a coroutine inside the TestClient's event loop (where the DB engine lives)."""
    return client.portal.call(coro_fn)
