"""FastAPI dependencies: authentication, CSRF, rate limiting, permissions."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, Request, WebSocket
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.config import Settings, get_settings
from app.db import get_db
from app.errors import Forbidden, TooManyRequests, Unauthenticated
from app.logging import get_logger
from app.security.crypto import aad_for, get_box
from app.security.ratelimit import limiter
from app.services import auth as auth_svc
from app.services.authz import AccessContext, Perm, build_access_context

log = get_logger(__name__)

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass
class Principal:
    user: m.User
    access: AccessContext
    web_session: m.WebSession | None = None
    api_token: m.ApiToken | None = None

    @property
    def id(self):  # noqa: ANN201
        return self.user.id

    @property
    def name(self) -> str:
        return self.user.username


def settings_dep() -> Settings:
    return get_settings()


def _bearer(request_headers) -> str | None:  # noqa: ANN001
    value = request_headers.get("authorization")
    if value and value.lower().startswith("bearer "):
        return value[7:].strip()
    return None


async def _maybe_revalidate_oidc(db: AsyncSession, settings: Settings, rec: m.WebSession) -> bool:
    """Use the refresh token to confirm the IdP still accepts the user. False = session revoked."""
    if not settings.OIDC_ENABLED or rec.auth_method != "oidc" or rec.oidc_revalidate_at is None:
        return True
    if rec.oidc_revalidate_at > auth_svc.now() or rec.oidc_refresh_token_enc is None:
        return True
    from app.services.oidc import get_oidc_client

    box = get_box()
    refresh_token = box.decrypt(rec.oidc_refresh_token_enc, aad_for("web_sessions", "refresh_token", rec.id))
    try:
        tokens = await get_oidc_client(settings).refresh(refresh_token or "")
    except Unauthenticated:
        log.info("oidc_refresh_rejected", user_id=str(rec.user_id))
        return False
    except Exception as exc:  # IdP temporarily unreachable: keep the session, retry later
        log.warning("oidc_refresh_failed", error=str(exc)[:200])
        rec.oidc_revalidate_at = auth_svc.now() + timedelta(minutes=1)
        return True
    if tokens.get("refresh_token"):
        rec.oidc_refresh_token_enc = box.encrypt(
            tokens["refresh_token"], aad_for("web_sessions", "refresh_token", rec.id)
        )
    if tokens.get("id_token"):
        rec.oidc_id_token_enc = box.encrypt(tokens["id_token"], aad_for("web_sessions", "id_token", rec.id))
    rec.oidc_revalidate_at = auth_svc.now() + timedelta(minutes=settings.OIDC_REVALIDATE_MINUTES)
    return True


async def authenticate(
    db: AsyncSession, settings: Settings, headers, cookies, method: str  # noqa: ANN001
) -> Principal:
    token = _bearer(headers)
    if token:
        if not settings.API_TOKENS_ENABLED:
            raise Unauthenticated("API tokens are disabled")
        found = await auth_svc.lookup_api_token(db, token)
        if found is None:
            raise Unauthenticated("invalid or expired API token")
        api_token, user = found
        access = await build_access_context(db, user, read_only_token=api_token.read_only)
        if not access.has(Perm.API_ACCESS):
            raise Forbidden("API access is not permitted for this user")
        if api_token.read_only and method in UNSAFE_METHODS:
            raise Forbidden("read-only API token")
        return Principal(user=user, access=access, api_token=api_token)

    cookie = cookies.get(settings.session_cookie_name)
    if not cookie:
        raise Unauthenticated()
    found_s = await auth_svc.lookup_web_session(db, settings, cookie)
    if found_s is None:
        raise Unauthenticated("session expired")
    rec, user = found_s
    if not await _maybe_revalidate_oidc(db, settings, rec):
        await db.delete(rec)
        raise Unauthenticated("identity provider session ended")
    if method in UNSAFE_METHODS:
        sent = headers.get("x-csrf-token") or ""
        if not hmac.compare_digest(sent, rec.csrf_token):
            raise Forbidden("missing or invalid CSRF token", code="csrf_failed")
        origin = headers.get("origin")
        if origin and origin != settings.app_origin:
            raise Forbidden("cross-origin request rejected", code="origin_rejected")
    access = await build_access_context(db, user)
    if not access.roles and not access.signage:
        raise Forbidden("no role assigned to this account", code="no_role")
    return Principal(user=user, access=access, web_session=rec)


async def get_principal(
    request: Request,
    db: AsyncSession = Depends(get_db, scope="function"),
    settings: Settings = Depends(settings_dep),
) -> Principal:
    principal = await authenticate(db, settings, request.headers, request.cookies, request.method)
    retry = limiter.hit("api", str(principal.user.id), settings.RATE_LIMIT_API_PER_MINUTE)
    if retry is not None:
        raise TooManyRequests(retry)
    request.state.principal = principal
    return principal


async def get_access(
    principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db, scope="function")
) -> Principal:
    """Principal with resource ACLs loaded."""
    await principal.access.load(db)
    return principal


def require(perm: Perm) -> Callable[..., Principal]:
    async def _dep(principal: Principal = Depends(get_access)) -> Principal:
        principal.access.require(perm)
        return principal

    return _dep  # type: ignore[return-value]


async def authenticate_websocket(ws: WebSocket, db: AsyncSession, settings: Settings) -> Principal:
    """Cookie or bearer auth for WebSocket upgrades, plus Origin enforcement.

    Browsers always send ``Origin`` on WebSocket upgrades; cookies are attached
    automatically, so a missing/foreign Origin must be rejected to prevent
    cross-site WebSocket hijacking. Non-browser API clients authenticate with
    a bearer token and may omit Origin.
    """
    origin = ws.headers.get("origin")
    has_bearer = _bearer(ws.headers) is not None
    if not has_bearer and origin != settings.app_origin:
        raise Forbidden("WebSocket origin rejected", code="origin_rejected")
    principal = await authenticate(db, settings, ws.headers, ws.cookies, "GET")
    return principal
