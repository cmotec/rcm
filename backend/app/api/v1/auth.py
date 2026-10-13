"""Authentication endpoints: OIDC, local recovery login, logout, current user, API tokens."""

from __future__ import annotations

import time
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.api.deps import Principal, get_principal, settings_dep
from app.config import Settings
from app.context import current_request
from app.db import get_db
from app.errors import AppError, Forbidden, NotFound, TooManyRequests, Unauthenticated
from app.logging import get_logger
from app.schemas.core import (
    ApiTokenCreate,
    ApiTokenCreated,
    ApiTokenOut,
    AuthConfigOut,
    LocalLogin,
    MeOut,
    PreferencesIn,
)
from app.security.crypto import Signer, aad_for, get_box
from app.security.passwords import hash_password, needs_rehash, verify_password
from app.security.ratelimit import limiter
from app.services import audit
from app.services import auth as auth_svc
from app.services.authz import Perm, build_access_context
from app.services.oidc import STATE_COOKIE, LoginState, get_oidc_client, safe_return_to
from app.services.sessions.manager import get_manager

log = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])
me_router = APIRouter(prefix="/me", tags=["me"])


def _signer() -> Signer:
    return Signer(get_box().keyring.derive("oidc-state-cookie"))


def _login_error_redirect(settings: Settings, code: str) -> RedirectResponse:
    resp = RedirectResponse(f"{settings.APP_URL}/login?error={code}", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(STATE_COOKIE, path="/api/v1/auth/oidc")
    return resp


@router.get("/config", response_model=AuthConfigOut, summary="Public login configuration")
async def auth_config(settings: Settings = Depends(settings_dep)) -> AuthConfigOut:
    return AuthConfigOut(
        oidc_enabled=settings.OIDC_ENABLED,
        oidc_display_name=settings.OIDC_DISPLAY_NAME,
        local_enabled=settings.LOCAL_AUTH_ENABLED,
    )


@router.get("/oidc/login", summary="Start OIDC login (redirects to the identity provider)")
async def oidc_login(
    request: Request, return_to: str | None = Query(None), settings: Settings = Depends(settings_dep)
) -> RedirectResponse:
    if not settings.OIDC_ENABLED:
        raise NotFound("OIDC is not enabled")
    ip = current_request().ip or "unknown"
    retry = limiter.hit("login", ip, settings.RATE_LIMIT_LOGIN_PER_MINUTE * 3)
    if retry is not None:
        raise TooManyRequests(retry)
    url, st = await get_oidc_client(settings).authorization_url(return_to)
    resp = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        STATE_COOKIE, _signer().sign(st.dump()), max_age=600, httponly=True, secure=settings.COOKIE_SECURE,
        samesite="lax", path="/api/v1/auth/oidc",
    )
    return resp


@router.get("/oidc/callback", summary="OIDC redirect URI")
async def oidc_callback(
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    db: AsyncSession = Depends(get_db, scope="function"),
    settings: Settings = Depends(settings_dep),
) -> Response:
    if not settings.OIDC_ENABLED:
        raise NotFound("OIDC is not enabled")
    req = current_request()
    if error:
        await audit.record_detached("auth.login", outcome="failure", details={"method": "oidc", "error": error[:100]})
        return _login_error_redirect(settings, "idp_error")
    raw = request.cookies.get(STATE_COOKIE)
    payload = _signer().unsign(raw) if raw else None
    if payload is None or not code or not state:
        return _login_error_redirect(settings, "state_missing")
    st = LoginState.load(payload)
    if st.state != state or st.exp < time.time():
        return _login_error_redirect(settings, "state_mismatch")
    try:
        ident = await get_oidc_client(settings).exchange_code(code, st)
    except AppError as exc:
        log.warning("oidc_callback_failed", error=exc.detail)
        await audit.record_detached("auth.login", outcome="failure", details={"method": "oidc", "error": exc.detail[:200]})
        return _login_error_redirect(settings, "token_invalid")

    user = (
        await db.execute(
            select(m.User).where(m.User.oidc_issuer == ident.issuer, m.User.oidc_subject == ident.subject)
        )
    ).scalar_one_or_none()
    if user is None:
        if not settings.OIDC_AUTO_PROVISION:
            await audit.record_detached("auth.login", actor_name=ident.username, outcome="denied",
                                        details={"method": "oidc", "reason": "not_provisioned"})
            return _login_error_redirect(settings, "not_provisioned")
        user = m.User(
            username=await auth_svc.unique_username(db, ident.username),
            email=ident.email,
            display_name=ident.display_name,
            oidc_issuer=ident.issuer,
            oidc_subject=ident.subject,
            is_local=False,
        )
        db.add(user)
        await db.flush()
        await audit.record(db, "user.provision", actor_id=user.id, actor_name=user.username,
                           resource_type="user", resource_id=user.id, details={"email": ident.email})
    else:
        user.email = ident.email or user.email
        user.display_name = ident.display_name or user.display_name
    if not user.is_active:
        await audit.record_detached("auth.login", actor_id=user.id, actor_name=user.username, outcome="denied",
                                    details={"method": "oidc", "reason": "inactive"})
        return _login_error_redirect(settings, "inactive")
    roles = await auth_svc.sync_groups_and_roles(db, settings, user, ident.groups)
    if not roles and not user.is_signage:
        await audit.record(db, "auth.login", actor_id=user.id, actor_name=user.username, outcome="denied",
                           details={"method": "oidc", "reason": "no_role", "groups": ident.groups[:50]})
        await db.commit()
        return _login_error_redirect(settings, "no_role")

    new = await auth_svc.create_web_session(
        db, settings, user, method="oidc", ip=req.ip, user_agent=req.user_agent,
        id_token=ident.id_token, refresh_token=ident.refresh_token,
    )
    await audit.record(db, "auth.login", actor_id=user.id, actor_name=user.username,
                       details={"method": "oidc", "roles": roles})
    resp = RedirectResponse(settings.APP_URL + safe_return_to(st.return_to), status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(STATE_COOKIE, path="/api/v1/auth/oidc")
    auth_svc.set_session_cookies(resp, settings, new)
    return resp


@router.post("/local/login", status_code=204, summary="Local (recovery) account login")
async def local_login(
    body: LocalLogin,
    response: Response,
    db: AsyncSession = Depends(get_db, scope="function"),
    settings: Settings = Depends(settings_dep),
) -> None:
    if not settings.LOCAL_AUTH_ENABLED:
        raise NotFound("local login is disabled")
    req = current_request()
    key = f"{req.ip}|{body.username.lower()}"
    for bucket, k in (("login", req.ip or "unknown"), ("login-user", key)):
        retry = limiter.hit(bucket, k, settings.RATE_LIMIT_LOGIN_PER_MINUTE)
        if retry is not None:
            await audit.record_detached("auth.login", actor_name=body.username, outcome="denied",
                                        details={"method": "local", "reason": "rate_limited"})
            raise TooManyRequests(retry)
    user = (
        await db.execute(select(m.User).where(func.lower(m.User.username) == body.username.lower(), m.User.is_local))
    ).scalar_one_or_none()
    if not verify_password(body.password, user.password_hash if user else None) or user is None or not user.is_active:
        await audit.record_detached("auth.login", actor_name=body.username, outcome="failure",
                                    details={"method": "local"})
        raise Unauthenticated("invalid username or password", code="invalid_credentials")
    if user.password_hash and needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    access = await build_access_context(db, user)
    if not access.roles and not access.signage:
        raise Forbidden("no role assigned to this account", code="no_role")
    limiter.reset("login-user", key)
    new = await auth_svc.create_web_session(db, settings, user, method="local", ip=req.ip, user_agent=req.user_agent)
    await audit.record(db, "auth.login", actor_id=user.id, actor_name=user.username, details={"method": "local"})
    auth_svc.set_session_cookies(response, settings, new)


@router.post("/logout", summary="Log out (returns the IdP logout URL if any)")
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db, scope="function"),
    settings: Settings = Depends(settings_dep),
) -> dict[str, str | None]:
    token = request.cookies.get(settings.session_cookie_name)
    redirect: str | None = None
    if token:
        rec = await auth_svc.delete_web_session(db, token)
        if rec is not None:
            # Note: logging out closes no remote sessions by itself; they belong to the user, not the browser.
            await audit.record(db, "auth.logout", actor_id=rec.user_id, resource_type="user", resource_id=rec.user_id)
            if rec.auth_method == "oidc" and settings.OIDC_ENABLED:
                id_token = None
                if rec.oidc_id_token_enc:
                    id_token = get_box().decrypt(rec.oidc_id_token_enc, aad_for("web_sessions", "id_token", rec.id))
                try:
                    redirect = await get_oidc_client(settings).end_session_url(id_token)
                except AppError:
                    redirect = None
    auth_svc.clear_session_cookies(response, settings)
    return {"redirect": redirect}


@router.get("/me", response_model=MeOut, summary="Current user")
async def me(principal: Principal = Depends(get_principal)) -> MeOut:
    a = principal.access
    perms = sorted(a.permissions) if not a.is_admin else sorted({"*", *[p.value for p in Perm]})
    ws = principal.web_session
    return MeOut(
        id=principal.user.id,
        username=principal.user.username,
        email=principal.user.email,
        display_name=principal.user.display_name,
        roles=a.roles,
        permissions=perms,
        is_admin=a.is_admin,
        csrf_token=ws.csrf_token if ws else None,
        auth_method=ws.auth_method if ws else "api_token",
        session_expires_at=ws.expires_at if ws else None,
        signage=a.signage,
        signage_connection_id=a.signage_connection_id,
    )


# --------------------------------------------------------------------------- /me


@me_router.get("/preferences", summary="User preferences (incl. workspace layout)")
async def get_preferences(principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    pref = await db.get(m.UserPreference, principal.id)
    return {"data": pref.data if pref else {}}


@me_router.put("/preferences", summary="Replace user preferences")
async def put_preferences(
    body: PreferencesIn, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db, scope="function")
) -> dict:
    pref = await db.get(m.UserPreference, principal.id)
    if pref is None:
        pref = m.UserPreference(user_id=principal.id, data=body.data)
        db.add(pref)
    else:
        pref.data = body.data
    return {"data": body.data}


@me_router.get("/tokens", response_model=list[ApiTokenOut], summary="List my API tokens")
async def list_tokens(principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db, scope="function")):
    rows = (await db.execute(select(m.ApiToken).where(m.ApiToken.user_id == principal.id)
                             .order_by(m.ApiToken.created_at.desc()))).scalars().all()
    return rows


@me_router.post("/tokens", response_model=ApiTokenCreated, status_code=201, summary="Create an API token")
async def create_token(
    body: ApiTokenCreate,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db, scope="function"),
    settings: Settings = Depends(settings_dep),
):
    if not settings.API_TOKENS_ENABLED:
        raise Forbidden("API tokens are disabled")
    if principal.api_token is not None:
        raise Forbidden("API tokens cannot create further tokens")
    principal.access.require(Perm.API_ACCESS)
    raw, prefix, digest = auth_svc.new_api_token()
    rec = m.ApiToken(
        user_id=principal.id, name=body.name, prefix=prefix, token_hash=digest, read_only=body.read_only,
        expires_at=auth_svc.now() + timedelta(days=body.expires_in_days) if body.expires_in_days else None,
    )
    db.add(rec)
    await db.flush()
    await db.refresh(rec)
    await audit.record(db, "api_token.create", actor_id=principal.id, actor_name=principal.name,
                       resource_type="api_token", resource_id=rec.id, details={"name": body.name, "read_only": body.read_only})
    return ApiTokenCreated.model_validate({**ApiTokenOut.model_validate(rec).model_dump(), "token": raw})


@me_router.delete("/tokens/{token_id}", status_code=204, summary="Revoke an API token")
async def revoke_token(token_id: uuid.UUID, principal: Principal = Depends(get_principal),
                       db: AsyncSession = Depends(get_db, scope="function")) -> None:
    rec = await db.get(m.ApiToken, token_id)
    if rec is None or rec.user_id != principal.id:
        raise NotFound("token not found")
    rec.revoked_at = auth_svc.now()
    await audit.record(db, "api_token.revoke", actor_id=principal.id, actor_name=principal.name,
                       resource_type="api_token", resource_id=rec.id)


@me_router.post("/logout-everywhere", status_code=204, summary="End all my browser sessions and remote sessions")
async def logout_everywhere(principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db, scope="function")) -> None:
    from sqlalchemy import delete

    await db.execute(delete(m.WebSession).where(m.WebSession.user_id == principal.id))
    closed = await get_manager().close_all_for_user(principal.id, "logout_everywhere")
    await audit.record(db, "auth.logout_everywhere", actor_id=principal.id, actor_name=principal.name,
                       details={"remote_sessions_closed": closed})
