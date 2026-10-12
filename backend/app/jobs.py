"""Background jobs: scheduled secret sync and housekeeping."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import delete, select

from app import models as m
from app.config import Settings
from app.db import session_scope
from app.logging import get_logger
from app.security.ratelimit import limiter
from app.services.auth import now, purge_expired_sessions
from app.services.secrets.sync import sync_provider

log = get_logger(__name__)


class Scheduler:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._loop("housekeeping", 300, self.housekeeping)))
        if self.settings.SECRET_SYNC_ENABLED:
            self._tasks.append(asyncio.create_task(self._loop("secret_sync", 30, self.secret_sync)))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _loop(self, name: str, interval: float, fn) -> None:  # noqa: ANN001
        await asyncio.sleep(5)
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error("job_failed", job=name, error=str(exc)[:300])
            await asyncio.sleep(interval)

    async def secret_sync(self) -> None:
        async with session_scope() as db:
            providers = (await db.execute(select(m.SecretProvider).where(m.SecretProvider.enabled))).scalars().all()
            due = [p for p in providers
                   if p.last_sync_at is None
                   or p.last_sync_at + timedelta(seconds=p.sync_interval_seconds) <= now()]
            ids = [p.id for p in due]
        for pid in ids:
            async with session_scope() as db:
                provider = await db.get(m.SecretProvider, pid)
                if provider is not None:
                    await sync_provider(db, provider, self.settings, trigger="schedule")

    async def housekeeping(self) -> None:
        async with session_scope() as db:
            purged = await purge_expired_sessions(db, self.settings)
            await db.execute(delete(m.IdempotencyKey).where(m.IdempotencyKey.created_at < now() - timedelta(hours=24)))
            if self.settings.AUDIT_RETENTION_DAYS > 0:
                await db.execute(delete(m.AuditEvent).where(
                    m.AuditEvent.ts < now() - timedelta(days=self.settings.AUDIT_RETENTION_DAYS)))
            await db.execute(delete(m.ImportJob).where(m.ImportJob.status == "previewed",
                                                       m.ImportJob.created_at < now() - timedelta(days=7)))
            # Session records left open by a crash of the previous process.
            await db.execute(m.SessionRecord.__table__.update()
                             .where(m.SessionRecord.ended_at.is_(None),
                                    m.SessionRecord.started_at < now() - timedelta(days=2))
                             .values(ended_at=now(), end_reason="stale"))
        limiter.prune()
        if purged:
            log.info("housekeeping", web_sessions_purged=purged)
