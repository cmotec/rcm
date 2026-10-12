"""FTP / FTPS file system using the standard library ``ftplib``.

``ftplib`` is blocking. Every FTP session gets a dedicated single-thread
executor, so all operations of one session run in order on one control
connection (FTP cannot multiplex). Streaming transfers pass data between
the worker thread and the event loop through bounded queues.

Supported: passive/active mode, explicit FTPS (AUTH TLS) and implicit FTPS
(TLS from the first byte, usually port 990), TLS session reuse on data
connections (required by many servers), binary/ASCII transfer types,
configurable control-channel encoding and timeouts.
"""

from __future__ import annotations

import asyncio
import ftplib
import posixpath
import queue
import re
import secrets
import socket
import ssl
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, TypeVar

from app.services.sessions.files.base import (
    Download,
    Entry,
    FileExists,
    FileNotFound,
    FileOpError,
    RemoteFS,
    Transfer,
    clean_path,
    join,
)

T = TypeVar("T")
CHUNK = 256 * 1024
_SENTINEL = object()


class _ReuseSessionMixin:
    """Reuse the control channel's TLS session on data channels (vsftpd require_ssl_reuse, FileZilla)."""

    def ntransfercmd(self, cmd: str, rest: Any = None) -> tuple[socket.socket, int | None]:  # type: ignore[override]
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)  # type: ignore[arg-type]
        if getattr(self, "_prot_p", False):
            conn = self.context.wrap_socket(  # type: ignore[attr-defined]
                conn, server_hostname=self.host, session=self.sock.session  # type: ignore[attr-defined]
            )
        return conn, size


class ExplicitFTPS(_ReuseSessionMixin, ftplib.FTP_TLS):
    pass


class ImplicitFTPS(_ReuseSessionMixin, ftplib.FTP_TLS):
    """FTPS where the control connection is TLS from the start."""

    def connect(self, host: str = "", port: int = 0, timeout: float = -999, source_address: Any = None) -> str:
        if host:
            self.host = host
        if port > 0:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        if source_address is not None:
            self.source_address = source_address
        sock = socket.create_connection((self.host, self.port), self.timeout, source_address=self.source_address)
        self.af = sock.family
        self.sock = self.context.wrap_socket(sock, server_hostname=self.host)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


_LIST_RE = re.compile(
    r"^(?P<mode>[\-ldcbps][\-rwxsStTlL]{9})[+@.]?\s+\d+\s+(?P<owner>\S+)\s+(?P<group>\S+)\s+(?P<size>\d+)\s+"
    r"(?P<date>\w{3}\s+\d{1,2}\s+(\d{1,2}:\d{2}|\d{4}))\s+(?P<name>.+)$"
)
_DOS_RE = re.compile(
    r"^(?P<date>\d{2}-\d{2}-\d{2,4})\s+(?P<time>\d{1,2}:\d{2}(AM|PM)?)\s+(?P<size><DIR>|\d+)\s+(?P<name>.+)$",
    re.IGNORECASE,
)


def _mode_bits(perm: str) -> int:
    bits = 0
    for i, ch in enumerate(perm[1:10]):
        if ch not in "-ST":
            bits |= 1 << (8 - i)
    return bits


def parse_list_line(directory: str, line: str) -> Entry | None:
    m = _LIST_RE.match(line)
    if m:
        mode = m.group("mode")
        name = m.group("name")
        target = None
        typ = {"d": "dir", "-": "file", "l": "link"}.get(mode[0], "other")
        if typ == "link" and " -> " in name:
            name, target = name.split(" -> ", 1)
        if name in (".", ".."):
            return None
        modified = None
        for fmt in ("%b %d %H:%M", "%b %d %Y"):
            try:
                dt = datetime.strptime(" ".join(m.group("date").split()), fmt)
                if fmt == "%b %d %H:%M":
                    now = datetime.now(UTC)
                    dt = dt.replace(year=now.year)
                    if dt.replace(tzinfo=UTC) > now:
                        dt = dt.replace(year=now.year - 1)
                modified = dt.replace(tzinfo=UTC)
                break
            except ValueError:
                continue
        return Entry(name=name, path=join(directory, name), type=typ, size=int(m.group("size")),
                     modified=modified, permissions=mode, mode=_mode_bits(mode), owner=m.group("owner"),
                     group=m.group("group"), link_target=target)
    d = _DOS_RE.match(line)
    if d:
        is_dir = d.group("size").upper() == "<DIR>"
        name = d.group("name")
        return Entry(name=name, path=join(directory, name), type="dir" if is_dir else "file",
                     size=None if is_dir else int(d.group("size")))
    return None


def _mlsd_entry(directory: str, name: str, facts: dict[str, str]) -> Entry | None:
    typ = facts.get("type", "").lower()
    if typ in ("cdir", "pdir") or name in (".", ".."):
        return None
    kind = {"dir": "dir", "file": "file"}.get(typ, "link" if typ.startswith("os.unix=slink") else "other")
    modified = None
    if "modify" in facts:
        try:
            modified = datetime.strptime(facts["modify"][:14], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            pass
    mode = None
    perms = None
    if "unix.mode" in facts:
        try:
            mode = int(facts["unix.mode"], 8)
            import stat as statmod

            prefix = statmod.S_IFDIR if kind == "dir" else statmod.S_IFREG
            perms = statmod.filemode(prefix | mode)
        except ValueError:
            pass
    return Entry(name=name, path=join(directory, name), type=kind,
                 size=int(facts["size"]) if facts.get("size", "").isdigit() else None,
                 modified=modified, mode=mode, permissions=perms or facts.get("perm"),
                 owner=facts.get("unix.owner") or facts.get("unix.ownername"),
                 group=facts.get("unix.group") or facts.get("unix.groupname"))


def _map_error(exc: Exception, path: str) -> Exception:
    msg = str(exc)
    if isinstance(exc, ftplib.error_perm):
        if msg.startswith("550") and ("exist" in msg.lower() and "not" not in msg.lower()):
            return FileExists(f"{path}: {msg}")
        if msg.startswith(("550", "450")):
            return FileNotFound(f"{path}: {msg}")
        return FileOpError(f"{path}: {msg}")
    if isinstance(exc, ssl.SSLError):
        return FileOpError(f"TLS error: {exc}", code="tls_error")
    if isinstance(exc, (OSError, EOFError, ftplib.Error)):
        return FileOpError(f"{path}: {msg or type(exc).__name__}", code="connection_error")
    return exc


class FtpFS(RemoteFS):
    kind = "ftp"

    def __init__(self, factory: Callable[[], ftplib.FTP], transfer_type: str, show_hidden: bool,
                 start_directory: str | None) -> None:
        self._factory = factory
        self._ftp: ftplib.FTP | None = None
        self._dirty = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ftp")
        self._transfer_type = transfer_type
        self._show_hidden = show_hidden
        self._start = start_directory
        self._mlsd: bool | None = None

    # ----------------------------------------------------------------- plumbing
    @classmethod
    async def open(cls, *, host: str, port: int, username: str | None, password: str | None, tls: str | None,
                   tls_verify: bool, ca_bundle: str | None, passive: bool, trust_pasv_address: bool,
                   encoding: str, timeout: float, transfer_type: str, show_hidden: bool,
                   start_directory: str | None) -> FtpFS:
        def factory() -> ftplib.FTP:
            if tls:
                ctx = ssl.create_default_context(cafile=ca_bundle)
                if not tls_verify:
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                klass = ImplicitFTPS if tls == "implicit" else ExplicitFTPS
                ftp: ftplib.FTP = klass(context=ctx, timeout=timeout, encoding=encoding)
            else:
                ftp = ftplib.FTP(timeout=timeout, encoding=encoding)
            ftp.trust_server_pasv_ipv4_address = trust_pasv_address
            ftp.connect(host, port, timeout=timeout)
            if tls == "explicit":
                ftp.auth()  # type: ignore[attr-defined]
            ftp.login(username or "anonymous", password or "")
            if tls:
                ftp.prot_p()  # type: ignore[attr-defined]
            ftp.set_pasv(passive)
            return ftp

        fs = cls(factory, transfer_type, show_hidden, start_directory)
        await fs._run(lambda ftp: None, "/")
        return fs

    def _conn(self) -> ftplib.FTP:
        if self._ftp is None or self._dirty:
            if self._ftp is not None:
                try:
                    self._ftp.close()
                except Exception:  # noqa: S110
                    pass
            self._ftp = self._factory()
            self._dirty = False
        return self._ftp

    async def _run(self, fn: Callable[[ftplib.FTP], T], path: str) -> T:
        loop = asyncio.get_running_loop()

        def call() -> T:
            ftp = self._conn()
            try:
                return fn(ftp)
            except (OSError, EOFError, ftplib.error_temp, ftplib.error_reply, ftplib.error_proto):
                self._dirty = True
                raise

        try:
            return await loop.run_in_executor(self._executor, call)
        except (ftplib.Error, OSError, EOFError) as exc:
            raise _map_error(exc, path) from exc

    # ----------------------------------------------------------------- operations
    async def home(self) -> str:
        if self._start:
            start = clean_path(self._start)
            try:
                await self._run(lambda f: f.cwd(start), start)
                return start
            except (FileNotFound, FileOpError):
                pass
        return clean_path(await self._run(lambda f: f.pwd(), "."))

    def _supports_mlsd(self, ftp: ftplib.FTP) -> bool:
        if self._mlsd is None:
            try:
                feat = ftp.sendcmd("FEAT")
                self._mlsd = "MLSD" in feat.upper()
            except ftplib.Error:
                self._mlsd = False
        return self._mlsd

    async def list(self, path: str) -> list[Entry]:
        path = clean_path(path)

        def do(ftp: ftplib.FTP) -> list[Entry]:
            entries: list[Entry] = []
            if self._supports_mlsd(ftp):
                try:
                    for name, facts in ftp.mlsd(path, facts=["type", "size", "modify", "unix.mode", "perm",
                                                             "unix.owner", "unix.group"]):
                        e = _mlsd_entry(path, name, facts)
                        if e:
                            entries.append(e)
                    return entries
                except ftplib.error_perm as exc:
                    if not str(exc).startswith(("500", "501", "502")):
                        raise
                    self._mlsd = False
            lines: list[str] = []
            ftp.cwd(path)
            ftp.retrlines("LIST -a" if self._show_hidden else "LIST", lines.append)
            for line in lines:
                e = parse_list_line(path, line)
                if e:
                    entries.append(e)
            return entries

        return await self._run(do, path)

    async def stat(self, path: str) -> Entry:
        path = clean_path(path)
        if path == "/":
            return Entry(name="/", path="/", type="dir")
        parent, name = posixpath.split(path)
        for e in await self.list(parent or "/"):
            if e.name == name:
                return e
        raise FileNotFound(f"{path}: no such file or directory")

    async def mkdir(self, path: str) -> None:
        path = clean_path(path)
        await self._run(lambda f: f.mkd(path), path)

    async def rename(self, source: str, target: str, overwrite: bool) -> None:
        source, target = clean_path(source), clean_path(target)
        if await self.exists(target):
            if not overwrite:
                raise FileExists(f"{target}: already exists")
            await self._run(lambda f: f.delete(target), target)
        await self._run(lambda f: f.rename(source, target), source)

    async def delete(self, path: str, recursive: bool) -> None:
        path = clean_path(path)
        if path == "/":
            raise FileOpError("refusing to delete the root directory")
        entry = await self.stat(path)
        if entry.type != "dir":
            await self._run(lambda f: f.delete(path), path)
            return
        if recursive:
            for child in await self.list(path):
                await self.delete(child.path, recursive=True)
        await self._run(lambda f: f.rmd(path), path)

    async def download(self, path: str) -> Download:
        path = clean_path(path)
        size: int | None = None
        try:
            size = await self._run(lambda f: (f.voidcmd("TYPE I"), f.size(path))[1], path)
        except FileOpError:
            pass
        except FileNotFound:
            raise
        q: queue.Queue[Any] = queue.Queue(maxsize=16)
        cancelled = False
        ascii_mode = self._transfer_type == "ascii"

        def worker(ftp: ftplib.FTP) -> None:
            def put(data: bytes) -> None:
                if cancelled:
                    raise _Cancelled()
                q.put(data)

            try:
                if ascii_mode:
                    enc = ftp.encoding
                    ftp.retrlines(f"RETR {path}", lambda line: put((line + "\n").encode(enc)))
                else:
                    ftp.retrbinary(f"RETR {path}", put, blocksize=CHUNK)
                q.put(_SENTINEL)
            except _Cancelled:
                self._dirty = True
                q.put(_SENTINEL)
            except BaseException as exc:
                q.put(exc)
                raise

        loop = asyncio.get_running_loop()

        def call() -> None:
            try:
                worker(self._conn())
            except (OSError, EOFError, ftplib.Error):
                self._dirty = True

        fut = loop.run_in_executor(self._executor, call)

        async def chunks() -> AsyncIterator[bytes]:
            nonlocal cancelled
            try:
                while True:
                    item = await asyncio.to_thread(q.get)
                    if item is _SENTINEL:
                        break
                    if isinstance(item, BaseException):
                        raise _map_error(item, path)
                    yield item
            finally:
                cancelled = True
                # drain so the worker is never blocked on a full queue
                while not fut.done():
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.01)

        first = await asyncio.to_thread(q.get)
        if isinstance(first, BaseException):
            raise _map_error(first, path)

        async def with_first() -> AsyncIterator[bytes]:
            if first is not _SENTINEL:
                yield first
                async for c in chunks():
                    yield c

        return Download(name=posixpath.basename(path), size=size, chunks=with_first())

    async def upload(self, path: str, chunks: AsyncIterator[bytes], overwrite: bool,
                     progress: Transfer | None = None) -> int:
        path = clean_path(path)
        if not overwrite and await self.exists(path):
            raise FileExists(f"{path}: already exists")
        tmp = f"{path}.part-{secrets.token_hex(4)}"
        q: queue.Queue[Any] = queue.Queue(maxsize=16)
        ascii_mode = self._transfer_type == "ascii"
        aborted = False

        class _Reader:
            """File-like object fed from the event loop; aborts the STOR when the upload is cancelled."""

            def __init__(self) -> None:
                self.buf = b""
                self.done = False

            def _next(self) -> None:
                while True:
                    if aborted:
                        raise _Cancelled()
                    try:
                        item = q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if item is _SENTINEL:
                        self.done = True
                    else:
                        self.buf += item
                    return

            def read(self, n: int = -1) -> bytes:
                while not self.done and (n < 0 or len(self.buf) < n):
                    self._next()
                if n < 0:
                    out, self.buf = self.buf, b""
                else:
                    out, self.buf = self.buf[:n], self.buf[n:]
                return out

            def readline(self, limit: int = -1) -> bytes:
                while not self.done and b"\n" not in self.buf:
                    self._next()
                idx = self.buf.find(b"\n")
                cut = len(self.buf) if idx < 0 else idx + 1
                out, self.buf = self.buf[:cut], self.buf[cut:]
                return out

        reader = _Reader()

        def store(ftp: ftplib.FTP) -> None:
            try:
                if ascii_mode:
                    ftp.storlines(f"STOR {tmp}", reader)  # type: ignore[arg-type]
                else:
                    ftp.storbinary(f"STOR {tmp}", reader, blocksize=CHUNK)  # type: ignore[arg-type]
            except _Cancelled:
                self._dirty = True  # control channel state is unknown after an aborted STOR
                raise

        fut = asyncio.ensure_future(self._run(store, path))

        async def put(item: Any) -> None:
            while True:
                try:
                    q.put_nowait(item)
                    return
                except queue.Full:
                    if fut.done():
                        return
                    await asyncio.sleep(0.005)

        written = 0
        try:
            async for chunk in chunks:
                if fut.done():
                    break
                if chunk:
                    await put(chunk)
                    written += len(chunk)
                    if progress:
                        progress.transferred = written
            await put(_SENTINEL)
            await fut
        except BaseException:
            aborted = True
            try:
                await asyncio.wait_for(asyncio.shield(fut), 30)
            except BaseException:  # noqa: S110 - the original error is what matters
                pass
            try:
                await self._run(lambda f: f.delete(tmp), tmp)
            except Exception:  # noqa: S110
                pass
            raise
        await self.rename(tmp, path, overwrite=True)
        return written

    async def close(self) -> None:
        def quit_() -> None:
            if self._ftp is not None:
                try:
                    self._ftp.quit()
                except Exception:
                    try:
                        self._ftp.close()
                    except Exception:  # noqa: S110
                        pass
                self._ftp = None

        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(loop.run_in_executor(self._executor, quit_), 5)
        except Exception:  # noqa: S110
            pass
        self._executor.shutdown(wait=False, cancel_futures=True)


class _Cancelled(Exception):
    pass
