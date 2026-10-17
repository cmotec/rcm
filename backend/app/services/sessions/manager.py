"""In-memory registry of live remote sessions.

Sessions own live sockets, so they live in the API process (see
docs/limitations-and-risks.md, A1). Metadata is mirrored to
``session_records`` for auditing and history.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update

from app import models as m
from app.config import Settings
from app.db import session_scope
from app.errors import Conflict, NotFound, TooManyRequests
from app.logging import get_logger
from app.services import audit
from app.services.sessions.files.base import RemoteFS, Transfer
from app.services.sessions.launch import LaunchConfig

log = get_logger(__name__)

KIND_BY_PROTOCOL = {"rdp": "guac", "vnc": "guac", "ssh": "terminal", "sftp": "files", "ftp": "files", "ftps": "files"}


class Stopped(Exception):  # noqa: N818
    """The session was closed (cancelled) while it was still connecting."""


async def unless_stopped[T](work: Awaitable[T], stop: asyncio.Event) -> T:
    """Await ``work`` (e.g. connecting to a host that may be down), but give up as soon as ``stop`` is
    set: ``work`` is cancelled and ``Stopped`` is raised, so a cancelled session stops connecting."""
    task = asyncio.ensure_future(work)
    stopper = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopper.cancel()
    if task.done():
        return task.result()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    raise Stopped


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Session:
    id: uuid.UUID
    owner_id: uuid.UUID
    owner_name: str
    kind: str
    protocol: str
    connection_id: uuid.UUID
    connection_name: str
    launch: LaunchConfig
    client_ip: str | None
    client_settings: dict[str, Any]
    state: str = "pending"  # pending | connecting | connected | disconnected | closed | error
    created_at: datetime = field(default_factory=utcnow)
    connected_at: datetime | None = None
    last_activity_at: datetime = field(default_factory=utcnow)
    disconnected_at: float | None = None
    error: str | None = None
    attached: bool = False
    attach_count: int = 0
    fs: RemoteFS | None = None
    fs_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    transfers: dict[str, Transfer] = field(default_factory=dict)
    _detach: Callable[[], Awaitable[None]] | None = None
    _last_touch: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        t = time.monotonic()
        if t - self._last_touch > 1:
            self._last_touch = t
            self.last_activity_at = utcnow()

    @property
    def idle_seconds(self) -> float:
        return (utcnow() - self.last_activity_at).total_seconds()

    def public(self, *, ticket: tuple[str, datetime] | None = None) -> dict[str, Any]:
        path = None
        if self.kind == "guac":
            path = f"/api/v1/sessions/{self.id}/tunnel"
        elif self.kind == "terminal":
            path = f"/api/v1/sessions/{self.id}/terminal"
        return {
            "id": self.id,
            "connection_id": self.connection_id,
            "connection_name": self.connection_name,
            "protocol": self.protocol,
            "kind": self.kind,
            "state": self.state,
            "created_at": self.created_at,
            "connected_at": self.connected_at,
            "last_activity_at": self.last_activity_at,
            "effective_host": self.launch.host,
            "effective_port": self.launch.port,
            "host_resolution_note": self.launch.host_note,
            "warnings": self.launch.warnings,
            "auth_sources": self.launch.auth_sources,
            "owner_id": self.owner_id,
            "owner_name": self.owner_name,
            "error": self.error,
            "ticket": ticket[0] if ticket else None,
            "ticket_expires_at": ticket[1] if ticket else None,
            "tunnel_path": path,
            "client_settings": self.client_settings,
        }


@dataclass
class _Ticket:
    session_id: uuid.UUID
    user_id: uuid.UUID
    expires: float


class SessionManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sessions: dict[uuid.UUID, Session] = {}
        self._tickets: dict[str, _Ticket] = {}
        self._reaper: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ registry
    def for_user(self, user_id: uuid.UUID) -> list[Session]:
        return [s for s in self.sessions.values() if s.owner_id == user_id]

    def all(self) -> list[Session]:
        return list(self.sessions.values())

    def get_owned(self, session_id: uuid.UUID, user_id: uuid.UUID) -> Session:
        s = self.sessions.get(session_id)
        if s is None or s.owner_id != user_id:
            # same answer for "missing" and "not yours": no session enumeration
            raise NotFound("session not found")
        return s

    def check_capacity(self, user_id: uuid.UUID) -> None:
        active = [s for s in self.for_user(user_id) if s.state not in ("closed", "error")]
        if len(active) >= self.settings.SESSION_MAX_PER_USER:
            raise TooManyRequests(5, f"session limit reached ({self.settings.SESSION_MAX_PER_USER})")

    async def register(self, session: Session) -> None:
        self.check_capacity(session.owner_id)
        self.sessions[session.id] = session
        try:
            async with session_scope() as db:
                db.add(
                    m.SessionRecord(
                        id=session.id,
                        user_id=session.owner_id,
                        connection_id=session.connection_id,
                        connection_name=session.connection_name,
                        protocol=session.protocol,
                        kind=session.kind,
                        effective_host=session.launch.host,
                        effective_port=session.launch.port,
                        auth_sources=session.launch.auth_sources,
                        client_ip=session.client_ip,
                    )
                )
        except Exception as exc:
            log.error("session_record_failed", error=str(exc)[:200])

    # ------------------------------------------------------------------ tickets
    def issue_ticket(self, session: Session) -> tuple[str, datetime]:
        self._prune_tickets()
        token = secrets.token_urlsafe(32)
        ttl = self.settings.SESSION_TICKET_TTL_SECONDS
        self._tickets[token] = _Ticket(session.id, session.owner_id, time.monotonic() + ttl)
        return token, utcnow() + timedelta(seconds=ttl)

    def consume_ticket(self, token: str | None, session_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        if not token:
            return False
        t = self._tickets.pop(token, None)
        return bool(
            t and t.session_id == session_id and t.user_id == user_id and t.expires > time.monotonic()
        )

    def _prune_tickets(self) -> None:
        now = time.monotonic()
        for k in [k for k, t in self._tickets.items() if t.expires <= now]:
            del self._tickets[k]

    # ------------------------------------------------------------------ lifecycle
    def begin_attach(self, session: Session, detach: Callable[[], Awaitable[None]]) -> None:
        if session.state in ("closed",):
            raise Conflict("session is closed", code="session_closed")
        if session.attached:
            raise Conflict("session is already attached in another window", code="session_attached")
        session.attached = True
        session.attach_count += 1
        session.state = "connecting"
        session.error = None
        session._detach = detach
        session.touch()

    async def mark_connected(self, session: Session) -> None:
        first = session.connected_at is None
        session.state = "connected"
        session.connected_at = session.connected_at or utcnow()
        session.disconnected_at = None
        await audit.record_detached(
            "session.connect", actor_id=session.owner_id, actor_name=session.owner_name,
            resource_type="connection", resource_id=session.connection_id, outcome="success",
            details={"session_id": str(session.id), "protocol": session.protocol,
                     "host": session.launch.host, "port": session.launch.port, "attach": session.attach_count},
        )
        if first:
            await self._update_record(session.id, first_connected_at=session.connected_at)

    def detach_now(self, session: Session, *, error: str | None = None) -> None:
        """Synchronous state change; safe to call from a ``finally`` block even while being cancelled."""
        session.attached = False
        session._detach = None
        if session.state != "closed":
            session.state = "error" if error else "disconnected"
            session.error = error
            session.disconnected_at = time.monotonic()

    async def end_attach(self, session: Session, *, error: str | None = None) -> None:
        self.detach_now(session, error=error)
        await self._update_record(session.id, attach_count=session.attach_count)

    async def close(self, session: Session, reason: str, *, actor_id: uuid.UUID | None = None,
                    actor_name: str | None = None) -> None:
        if session.state == "closed" and session.id not in self.sessions:
            return
        session.state = "closed"
        detach = session._detach
        if detach is not None:
            try:
                await detach()
            except Exception as exc:  # noqa: BLE001
                log.warning("session_detach_failed", error=str(exc)[:200])
        if session.fs is not None:
            try:
                await session.fs.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("fs_close_failed", error=str(exc)[:200])
            session.fs = None
        session.launch.wipe()
        self.sessions.pop(session.id, None)
        for k in [k for k, t in self._tickets.items() if t.session_id == session.id]:
            del self._tickets[k]
        await self._update_record(session.id, ended_at=utcnow(), end_reason=reason[:64])
        await audit.record_detached(
            "session.close", actor_id=actor_id or session.owner_id, actor_name=actor_name or session.owner_name,
            resource_type="connection", resource_id=session.connection_id, outcome="success",
            details={"session_id": str(session.id), "reason": reason, "owner": session.owner_name},
        )

    async def close_all_for_user(self, user_id: uuid.UUID, reason: str) -> int:
        sessions = self.for_user(user_id)
        for s in sessions:
            await self.close(s, reason)
        return len(sessions)

    async def shutdown(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        for s in list(self.sessions.values()):
            await self.close(s, "server_shutdown")

    # ------------------------------------------------------------------ reaper
    def start_reaper(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            try:
                await self.reap()
            except Exception as exc:  # noqa: BLE001
                log.error("reaper_failed", error=str(exc)[:200])

    async def reap(self) -> None:
        now = time.monotonic()
        grace = self.settings.SESSION_RECONNECT_GRACE_SECONDS
        idle_limit = self.settings.SESSION_IDLE_TIMEOUT_SECONDS
        self._prune_tickets()
        for s in list(self.sessions.values()):
            if s.state in ("disconnected", "error") and not s.attached and s.disconnected_at is not None:
                if now - s.disconnected_at > grace:
                    await self.close(s, "reconnect_grace_expired")
                    continue
            if s.state == "pending" and s.kind != "files" and (utcnow() - s.created_at).total_seconds() > max(
                grace, 60
            ):
                await self.close(s, "never_attached")
                continue
            if idle_limit and s.idle_seconds > idle_limit and not any(
                t.state == "running" for t in s.transfers.values()
            ):
                await self.close(s, "idle_timeout")

    async def _update_record(self, session_id: uuid.UUID, **values: Any) -> None:
        try:
            async with session_scope() as db:
                await db.execute(update(m.SessionRecord).where(m.SessionRecord.id == session_id).values(**values))
        except Exception as exc:  # noqa: BLE001
            log.error("session_record_update_failed", error=str(exc)[:200])


_manager: SessionManager | None = None


def init_manager(settings: Settings) -> SessionManager:
    global _manager
    _manager = SessionManager(settings)
    return _manager


def get_manager() -> SessionManager:
    if _manager is None:
        raise RuntimeError("session manager not initialised")
    return _manager
