"""Common interface for remote file systems (SFTP, FTP, FTPS)."""

from __future__ import annotations

import posixpath
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime

from app.errors import AppError, ValidationFailed


class FileOpError(AppError):
    status, code, title = 400, "file_operation_failed", "File operation failed"


class FileNotFound(AppError):
    status, code, title = 404, "file_not_found", "File not found"


class FileExists(AppError):
    status, code, title = 409, "file_exists", "File already exists"


class NotSupported(AppError):
    status, code, title = 501, "not_supported", "Not supported by this protocol"


@dataclass
class Entry:
    name: str
    path: str
    type: str  # file | dir | link | other
    size: int | None = None
    modified: datetime | None = None
    permissions: str | None = None
    mode: int | None = None
    owner: str | None = None
    group: str | None = None
    link_target: str | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Download:
    name: str
    size: int | None
    chunks: AsyncIterator[bytes]


@dataclass
class Transfer:
    id: str
    direction: str  # upload | download
    path: str
    size: int | None
    transferred: int = 0
    state: str = "running"  # running | done | failed | cancelled
    error: str | None = None
    started_at: float = 0.0
    finished_at: float | None = None
    extra: dict = field(default_factory=dict)


def clean_path(path: str) -> str:
    if not path or "\x00" in path or len(path) > 4096:
        raise ValidationFailed("invalid path")
    norm = posixpath.normpath(path)
    if path.startswith("//"):
        norm = "/" + norm.lstrip("/")
    return norm


def join(directory: str, name: str) -> str:
    return clean_path(posixpath.join(directory, name))


class RemoteFS(ABC):
    kind: str = "abstract"
    supports_chmod: bool = False

    @abstractmethod
    async def home(self) -> str: ...

    @abstractmethod
    async def list(self, path: str) -> list[Entry]: ...

    @abstractmethod
    async def stat(self, path: str) -> Entry: ...

    @abstractmethod
    async def mkdir(self, path: str) -> None: ...

    @abstractmethod
    async def rename(self, source: str, target: str, overwrite: bool) -> None: ...

    @abstractmethod
    async def delete(self, path: str, recursive: bool) -> None: ...

    async def chmod(self, path: str, mode: int) -> None:
        raise NotSupported("changing permissions is not supported for this protocol")

    @abstractmethod
    async def download(self, path: str) -> Download: ...

    @abstractmethod
    async def upload(self, path: str, chunks: AsyncIterator[bytes], overwrite: bool,
                     progress: Transfer | None = None) -> int: ...

    @abstractmethod
    async def close(self) -> None: ...

    async def exists(self, path: str) -> bool:
        try:
            await self.stat(path)
            return True
        except FileNotFound:
            return False
