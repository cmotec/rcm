"""SFTP file system (asyncssh)."""

from __future__ import annotations

import posixpath
import secrets
import stat as statmod
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import asyncssh

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
from app.services.sessions.ssh_native import SshConnectionSet

CHUNK = 256 * 1024


def _err(exc: Exception, path: str) -> Exception:
    if isinstance(exc, asyncssh.SFTPNoSuchFile):
        return FileNotFound(f"{path}: no such file or directory")
    if isinstance(exc, asyncssh.SFTPFileAlreadyExists):
        return FileExists(f"{path}: already exists")
    if isinstance(exc, asyncssh.SFTPPermissionDenied):
        return FileOpError(f"{path}: permission denied", code="permission_denied")
    if isinstance(exc, asyncssh.SFTPError):
        return FileOpError(f"{path}: {exc.reason}")
    return FileOpError(f"{path}: {exc}")


def _entry(directory: str, name: asyncssh.SFTPName) -> Entry:
    a = name.attrs
    mode = a.permissions
    if a.type == asyncssh.FILEXFER_TYPE_DIRECTORY or (mode is not None and statmod.S_ISDIR(mode)):
        typ = "dir"
    elif a.type == asyncssh.FILEXFER_TYPE_SYMLINK or (mode is not None and statmod.S_ISLNK(mode)):
        typ = "link"
    elif a.type == asyncssh.FILEXFER_TYPE_REGULAR or (mode is not None and statmod.S_ISREG(mode)):
        typ = "file"
    else:
        typ = "other"
    owner = a.owner
    group = a.group
    if (owner is None or group is None) and name.longname:
        # SFTP v3 has no owner names; OpenSSH's "ls -l"-style long name carries them:
        # perms nlink owner group size month day time|year name
        cols = (name.longname.decode(errors="replace") if isinstance(name.longname, bytes)
                else str(name.longname)).split()
        if len(cols) >= 9 and cols[1].isdigit():
            owner, group = owner or cols[2], group or cols[3]
    if owner is None and a.uid is not None:
        owner = str(a.uid)
    if group is None and a.gid is not None:
        group = str(a.gid)
    fname = name.filename.decode(errors="replace") if isinstance(name.filename, bytes) else str(name.filename)
    return Entry(
        name=fname,
        path=join(directory, fname),
        type=typ,
        size=a.size,
        modified=datetime.fromtimestamp(a.mtime, UTC) if a.mtime is not None else None,
        permissions=statmod.filemode(mode) if mode is not None else None,
        mode=statmod.S_IMODE(mode) if mode is not None else None,
        owner=owner,
        group=group,
    )


class SftpFS(RemoteFS):
    kind = "sftp"
    supports_chmod = True

    def __init__(self, conns: SshConnectionSet, sftp: asyncssh.SFTPClient, start_directory: str | None) -> None:
        self._conns = conns
        self._sftp = sftp
        self._start = start_directory

    @classmethod
    async def open(cls, conns: SshConnectionSet, start_directory: str | None) -> SftpFS:
        sftp = await conns.conn.start_sftp_client()
        return cls(conns, sftp, start_directory)

    async def home(self) -> str:
        if self._start:
            try:
                return clean_path(await self._sftp.realpath(self._start))
            except asyncssh.SFTPError:
                pass
        return clean_path(await self._sftp.realpath("."))

    async def list(self, path: str) -> list[Entry]:
        path = clean_path(path)
        try:
            names = await self._sftp.readdir(path)
        except (asyncssh.SFTPError, OSError) as exc:
            raise _err(exc, path) from exc
        out = []
        for n in names:
            fname = n.filename if isinstance(n.filename, str) else n.filename.decode(errors="replace")
            if fname in (".", ".."):
                continue
            e = _entry(path, n)
            if e.type == "link":
                try:
                    target = await self._sftp.stat(e.path)
                    if target.type == asyncssh.FILEXFER_TYPE_DIRECTORY:
                        e.type = "dir"
                    e.link_target = await self._sftp.readlink(e.path)
                except asyncssh.SFTPError:
                    pass
            out.append(e)
        return out

    async def stat(self, path: str) -> Entry:
        path = clean_path(path)
        try:
            attrs = await self._sftp.stat(path)
        except (asyncssh.SFTPError, OSError) as exc:
            raise _err(exc, path) from exc
        name = asyncssh.SFTPName(posixpath.basename(path) or "/", "", attrs)
        e = _entry(posixpath.dirname(path) or "/", name)
        e.path = path
        return e

    async def mkdir(self, path: str) -> None:
        path = clean_path(path)
        try:
            await self._sftp.mkdir(path)
        except (asyncssh.SFTPError, OSError) as exc:
            raise _err(exc, path) from exc

    async def rename(self, source: str, target: str, overwrite: bool) -> None:
        source, target = clean_path(source), clean_path(target)
        try:
            if await self._sftp.exists(target):
                if not overwrite:
                    raise FileExists(f"{target}: already exists")
                try:
                    await self._sftp.posix_rename(source, target)
                    return
                except asyncssh.SFTPError:
                    await self._sftp.remove(target)
            await self._sftp.rename(source, target)
        except FileExists:
            raise
        except (asyncssh.SFTPError, OSError) as exc:
            raise _err(exc, source) from exc

    async def delete(self, path: str, recursive: bool) -> None:
        path = clean_path(path)
        if path == "/":
            raise FileOpError("refusing to delete the root directory")
        try:
            attrs = await self._sftp.lstat(path)
            if attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY:
                if recursive:
                    await self._sftp.rmtree(path)
                else:
                    await self._sftp.rmdir(path)
            else:
                await self._sftp.remove(path)
        except (asyncssh.SFTPError, OSError) as exc:
            raise _err(exc, path) from exc

    async def chmod(self, path: str, mode: int) -> None:
        path = clean_path(path)
        try:
            await self._sftp.chmod(path, mode)
        except (asyncssh.SFTPError, OSError) as exc:
            raise _err(exc, path) from exc

    async def download(self, path: str) -> Download:
        path = clean_path(path)
        try:
            attrs = await self._sftp.stat(path)
            if attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY:
                raise FileOpError(f"{path} is a directory")
            fh = await self._sftp.open(path, "rb")
        except (asyncssh.SFTPError, OSError) as exc:
            raise _err(exc, path) from exc

        async def chunks() -> AsyncIterator[bytes]:
            try:
                while True:
                    data = await fh.read(CHUNK)
                    if not data:
                        break
                    yield data
            finally:
                await fh.close()

        return Download(name=posixpath.basename(path), size=attrs.size, chunks=chunks())

    async def upload(self, path: str, chunks: AsyncIterator[bytes], overwrite: bool,
                     progress: Transfer | None = None) -> int:
        path = clean_path(path)
        if not overwrite and await self._sftp.exists(path):
            raise FileExists(f"{path}: already exists")
        tmp = f"{path}.part-{secrets.token_hex(4)}"
        written = 0
        try:
            async with self._sftp.open(tmp, "wb") as fh:
                async for chunk in chunks:
                    if chunk:
                        await fh.write(chunk)
                        written += len(chunk)
                        if progress:
                            progress.transferred = written
            await self.rename(tmp, path, overwrite=True)
        except BaseException as exc:
            try:
                await self._sftp.remove(tmp)
            except (asyncssh.SFTPError, OSError):
                pass
            if isinstance(exc, (asyncssh.SFTPError, OSError)):
                raise _err(exc, path) from exc
            raise
        return written

    async def close(self) -> None:
        try:
            self._sftp.exit()
        except Exception:  # noqa: S110 - best effort
            pass
        self._conns.close()
        await self._conns.wait_closed()
