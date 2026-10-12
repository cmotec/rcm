"""Local stack for the browser tests: embedded PostgreSQL (pgserver), SSH/SFTP (asyncssh) and FTP (pyftpdlib)
test servers, and the real API on 127.0.0.1:8000. Run the Vite dev server separately (npm run dev).
No guacd is started: RDP/VNC sessions report "cannot reach guacd" (covered by the smoke test)."""

import asyncio
import base64
import os
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

BACKEND = str(Path(__file__).resolve().parents[1] / "backend")
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

import asyncssh  # noqa: E402
import pgserver  # noqa: E402

ROOT = Path(tempfile.mkdtemp(prefix="devstack-"))
(ROOT / "sftp").mkdir()
(ROOT / "sftp" / "hello.txt").write_text("hello from sftp\n")
(ROOT / "sftp" / "docs").mkdir()
(ROOT / "ftp").mkdir()
(ROOT / "ftp" / "readme.txt").write_text("ftp file\n")

srv = pgserver.get_server(str(ROOT / "pg"), cleanup_mode="stop")
srv.psql("CREATE DATABASE app;")
u = urlsplit(srv.get_uri())

loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
host_key = asyncssh.generate_private_key("ssh-ed25519")


class Server(asyncssh.SSHServer):
    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return username == "tester" and password == "ssh-secret-pw"  # noqa: S105 - test server


async def handle(process):
    process.stdout.write(f"welcome {process.get_extra_info('username')} to the test server\r\n$ ")
    while True:
        try:
            data = await process.stdin.read(1024)
        except asyncssh.TerminalSizeChanged as exc:
            process.stdout.write(f"\r\n[resized {exc.width}x{exc.height}]\r\n$ ")
            continue
        if not data:
            break
        if "\x04" in data:
            break
        process.stdout.write(data.replace("\r", "\r\n$ "))
    process.exit(0)


class ChrootSFTP(asyncssh.SFTPServer):
    def __init__(self, chan):
        super().__init__(chan, chroot=str(ROOT / "sftp").encode())


async def start_ssh():
    await asyncssh.listen("127.0.0.1", 2222, server_host_keys=[host_key], server_factory=Server,
                          process_factory=handle, sftp_factory=ChrootSFTP, line_editor=False)


asyncio.run_coroutine_threadsafe(start_ssh(), loop).result(10)

from pyftpdlib.authorizers import DummyAuthorizer  # noqa: E402
from pyftpdlib.handlers import FTPHandler  # noqa: E402
from pyftpdlib.servers import ThreadedFTPServer  # noqa: E402

auth = DummyAuthorizer()
auth.add_user("ftpuser", "ftp-secret-pw", str(ROOT / "ftp"), perm="elradfmwMT")
FTPHandler.authorizer = auth
FTPHandler.passive_ports = range(60100, 60200)
ftp = ThreadedFTPServer(("127.0.0.1", 2121), FTPHandler)
threading.Thread(target=ftp.serve_forever, kwargs={"timeout": 0.5}, daemon=True).start()

os.environ.update({
    "ENVIRONMENT": "development", "APP_URL": "http://localhost:5173", "LOG_LEVEL": "INFO", "LOG_FORMAT": "console",
    "DB_HOST": u.hostname, "DB_PORT": str(u.port), "DB_USER": "postgres", "DB_PASSWORD": "", "DB_NAME": "app",
    "DB_SSLMODE": "disable", "ENCRYPTION_KEYS": "k1:" + base64.b64encode(os.urandom(32)).decode(),
    "LOCAL_AUTH_ENABLED": "true", "LOCAL_ADMIN_USERNAME": "admin", "LOCAL_ADMIN_PASSWORD": "admin-password-123",
    "COOKIE_SECURE": "false", "GUACD_HOST": "127.0.0.1", "GUACD_PORT": "4999", "SECRET_SYNC_ENABLED": "false",
})
print("devstack: ssh 2222, ftp 2121, root", ROOT, "fingerprint", host_key.convert_to_public().get_fingerprint("sha256"),
      flush=True)

import uvicorn  # noqa: E402

from app.main import create_app  # noqa: E402

uvicorn.run(create_app(), host="127.0.0.1", port=8000, log_level="warning", ws_max_size=16 * 1024 * 1024)
