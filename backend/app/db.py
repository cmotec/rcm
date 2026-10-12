"""Database engine and session management (external PostgreSQL via asyncpg)."""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def build_url(settings: Settings, driver: str = "postgresql+asyncpg") -> URL:
    return URL.create(
        driver,
        username=settings.DB_USER,
        password=settings.DB_PASSWORD or None,
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        database=settings.DB_NAME,
    )


def build_ssl_context(settings: Settings) -> ssl.SSLContext | str | bool:
    """Translate libpq-style sslmode into what asyncpg expects."""
    mode = settings.DB_SSLMODE
    if mode == "disable":
        return False
    if mode in ("allow", "prefer", "require") and not settings.DB_SSLROOTCERT and not settings.DB_SSLCERT:
        return mode
    ctx = ssl.create_default_context(cafile=settings.DB_SSLROOTCERT)
    if mode in ("allow", "prefer", "require"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    elif mode == "verify-ca":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
    if settings.DB_SSLCERT:
        ctx.load_cert_chain(settings.DB_SSLCERT, settings.DB_SSLKEY)
    return ctx


def init_engine(settings: Settings) -> AsyncEngine:
    global _engine, _sessionmaker
    connect_args: dict[str, Any] = {
        "ssl": build_ssl_context(settings),
        "timeout": settings.DB_CONNECT_TIMEOUT_SECONDS,
        "server_settings": {"application_name": "connection-manager-api"},
    }
    _engine = create_async_engine(
        build_url(settings),
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT_SECONDS,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args=connect_args,
    )
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("database engine not initialised")
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("database engine not initialised")
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Unit of work: commit on success, roll back on error."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one transaction per request."""
    async with session_scope() as session:
        yield session


async def ping() -> bool:
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
