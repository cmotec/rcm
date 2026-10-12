"""Alembic environment (async, configured from the application settings)."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db import build_ssl_context, build_url
from app.models import Base

target_metadata = Base.metadata
MIGRATION_LOCK = 0x4D49_4752  # serialises concurrent migrations from several containers


def run_migrations_offline() -> None:
    settings = get_settings()
    context.configure(url=build_url(settings).render_as_string(hide_password=True), target_metadata=target_metadata,
                      literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:  # noqa: ANN001
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    settings = get_settings()
    engine = create_async_engine(
        build_url(settings),
        connect_args={"ssl": build_ssl_context(settings), "timeout": settings.DB_CONNECT_TIMEOUT_SECONDS},
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK})
            await conn.commit()
            try:
                await conn.run_sync(_do_run)
                await conn.commit()
            except BaseException:
                # A failed migration leaves the transaction aborted; roll back so the real error is
                # reported instead of "current transaction is aborted" from the unlock below.
                await conn.rollback()
                raise
            finally:
                try:
                    await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK})
                    await conn.commit()
                except Exception:  # noqa: S110 - the lock is released with the connection anyway
                    pass
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
