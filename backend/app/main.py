"""Application factory."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request, Response
from sqlalchemy import func, select

from app import models as m
from app.api.v1 import acl, admin, auth, connections, credentials, imports, secrets, sessions, system
from app.config import APP_NAME, Settings, get_settings
from app.context import RequestInfo, request_info
from app.db import dispose_engine, init_engine, session_scope
from app.errors import install_error_handlers
from app.jobs import Scheduler
from app.logging import configure_logging, get_logger
from app.security.crypto import init_crypto
from app.security.passwords import hash_password
from app.services.authz import ensure_builtin_roles
from app.services.oidc import get_oidc_client, set_oidc_client
from app.services.secrets.sync import ensure_env_providers
from app.services.sessions.manager import init_manager

log = get_logger("app")


def run_migrations(settings: Settings) -> None:
    from alembic import command
    from alembic.config import Config

    from app.migrations_path import alembic_config_path

    cfg = Config(alembic_config_path())
    command.upgrade(cfg, "head")


async def bootstrap(settings: Settings) -> None:
    async with session_scope() as db:
        roles = await ensure_builtin_roles(db)
        await ensure_env_providers(db, settings)
        if settings.LOCAL_AUTH_ENABLED and settings.LOCAL_ADMIN_USERNAME and settings.LOCAL_ADMIN_PASSWORD:
            existing = (await db.execute(select(m.User).where(
                func.lower(m.User.username) == settings.LOCAL_ADMIN_USERNAME.lower()))).scalar_one_or_none()
            if existing is None:
                user = m.User(username=settings.LOCAL_ADMIN_USERNAME, is_local=True,
                              password_hash=hash_password(settings.LOCAL_ADMIN_PASSWORD),
                              display_name="Local administrator")
                db.add(user)
                await db.flush()
                db.add(m.UserRole(user_id=user.id, role_id=roles["admin"].id, source="bootstrap"))
                log.warning("local_admin_created", username=user.username)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    init_crypto(settings.ENCRYPTION_KEYS, settings.ENCRYPTION_ACTIVE_KEY_ID)
    init_engine(settings)
    if settings.DB_MIGRATE_ON_START:
        await asyncio.to_thread(run_migrations, settings)
    await bootstrap(settings)
    manager = init_manager(settings)
    manager.start_reaper()
    scheduler = Scheduler(settings)
    scheduler.start()
    log.info("startup_complete", app_url=settings.APP_URL, oidc=settings.OIDC_ENABLED, local=settings.LOCAL_AUTH_ENABLED)
    try:
        yield
    finally:
        await scheduler.stop()
        await manager.shutdown()
        if settings.OIDC_ENABLED:
            await get_oidc_client(settings).close()
            set_oidc_client(None)
        await dispose_engine()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
    docs = settings.API_DOCS_ENABLED
    app = FastAPI(
        title=f"{APP_NAME} API",
        version=system.VERSION,
        description=(
            "REST API of Remote Connections Manager. Authenticate with the browser session "
            "cookie (plus `X-CSRF-Token` header for unsafe methods) or with a personal API token "
            "(`Authorization: Bearer pat_…`). Errors use RFC 9457 problem details. "
            "POST endpoints that create resources honour the `Idempotency-Key` header."
        ),
        lifespan=lifespan,
        docs_url="/api/docs" if docs else None,
        redoc_url="/api/redoc" if docs else None,
        openapi_url="/api/openapi.json" if docs else None,
    )
    app.state.settings = settings
    install_error_handlers(app)

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # noqa: ANN001, ANN202
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_info.set(RequestInfo(
            request_id=rid[:64], ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        ))
        start = time.perf_counter()
        try:
            response: Response = await call_next(request)
        finally:
            request_info.reset(token)
        response.headers["X-Request-ID"] = rid[:64]
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        if not request.url.path.endswith(("/healthz", "/readyz")):
            log.info("request", method=request.method, path=request.url.path, status=response.status_code,
                     duration_ms=round((time.perf_counter() - start) * 1000, 1), request_id=rid[:64])
        return response

    v1 = APIRouter(prefix="/api/v1")
    for r in (auth.router, auth.me_router, connections.router, credentials.router, acl.router, sessions.router,
              admin.router, secrets.router, imports.router, system.router):
        v1.include_router(r)
    app.include_router(v1)
    app.include_router(system.health_router)
    app.include_router(system.health_router, prefix="/api")
    return app
