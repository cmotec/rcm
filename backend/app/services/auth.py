"""Web sessions, API tokens and user provisioning."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.config import Settings
from app.security.crypto import aad_for, get_box, token_hash
from app.services.authz import ensure_builtin_roles

API_TOKEN_PREFIX = "pat_"  # noqa: S105 - identifies personal access tokens for secret scanners


def now() -> datetime:
    return datetime.now(UTC)


@dataclass
class NewSession:
    token: str
    record: m.WebSession


async def create_web_session(
    db: AsyncSession,
    settings: Settings,
    user: m.User,
    *,
    method: str,
    ip: str | None,
    user_agent: str | None,
    id_token: str | None = None,
    refresh_token: str | None = None,
) -> NewSession:
    token = secrets.token_urlsafe(32)
    sid = token_hash(token)
    box = get_box()
    rec = m.WebSession(
        id=sid,
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(32),
        auth_method=method,
        expires_at=now() + (timedelta(days=settings.SIGNAGE_SESSION_MAX_DAYS) if user.is_signage
                            else timedelta(hours=settings.AUTH_SESSION_MAX_HOURS)),
        last_seen_at=now(),
        ip=ip,
        user_agent=(user_agent or "")[:512] or None,
        oidc_id_token_enc=box.encrypt(id_token, aad_for("web_sessions", "id_token", sid)),
        oidc_refresh_token_enc=box.encrypt(refresh_token, aad_for("web_sessions", "refresh_token", sid)),
        oidc_revalidate_at=(
            now() + timedelta(minutes=settings.OIDC_REVALIDATE_MINUTES) if refresh_token else None
        ),
    )
    db.add(rec)
    user.last_login_at = now()
    await db.flush()
    return NewSession(token=token, record=rec)


def set_session_cookies(response: Response, settings: Settings, new: NewSession) -> None:
    max_age = settings.AUTH_SESSION_MAX_HOURS * 3600
    response.set_cookie(
        settings.session_cookie_name,
        new.token,
        max_age=max_age,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        settings.csrf_cookie_name,
        new.record.csrf_token,
        max_age=max_age,
        httponly=False,
        secure=settings.COOKIE_SECURE,
        samesite="strict",
        path="/",
    )


def clear_session_cookies(response: Response, settings: Settings) -> None:
    for name in (settings.session_cookie_name, settings.csrf_cookie_name):
        response.delete_cookie(name, path="/", secure=settings.COOKIE_SECURE, httponly=True)


async def lookup_web_session(
    db: AsyncSession, settings: Settings, token: str
) -> tuple[m.WebSession, m.User] | None:
    sid = token_hash(token)
    rec = await db.get(m.WebSession, sid)
    if rec is None:
        return None
    t = now()
    user = await db.get(m.User, rec.user_id)
    if user is None or not user.is_active:
        await db.delete(rec)
        return None
    # A signage display shows a remote screen for days without calling the API: no idle timeout.
    idle_limit = rec.last_seen_at + timedelta(minutes=settings.AUTH_SESSION_IDLE_MINUTES)
    if rec.expires_at <= t or (idle_limit <= t and not user.is_signage):
        await db.delete(rec)
        return None
    # sliding idle expiry; write at most once a minute
    if (t - rec.last_seen_at).total_seconds() > 60:
        rec.last_seen_at = t
    return rec, user


async def delete_web_session(db: AsyncSession, token: str) -> m.WebSession | None:
    rec = await db.get(m.WebSession, token_hash(token))
    if rec is not None:
        await db.delete(rec)
    return rec


async def purge_expired_sessions(db: AsyncSession, settings: Settings) -> int:
    t = now()
    res = await db.execute(
        delete(m.WebSession).where(
            (m.WebSession.expires_at <= t)
            | (m.WebSession.last_seen_at <= t - timedelta(minutes=settings.AUTH_SESSION_IDLE_MINUTES))
        )
    )
    return res.rowcount or 0


# --------------------------------------------------------------------------- API tokens


def new_api_token() -> tuple[str, str, str]:
    """Return (token, prefix, hash)."""
    raw = API_TOKEN_PREFIX + secrets.token_urlsafe(32)
    return raw, raw[: len(API_TOKEN_PREFIX) + 6], token_hash(raw)


async def lookup_api_token(db: AsyncSession, token: str) -> tuple[m.ApiToken, m.User] | None:
    rec = (
        await db.execute(select(m.ApiToken).where(m.ApiToken.token_hash == token_hash(token)))
    ).scalar_one_or_none()
    t = now()
    if rec is None or rec.revoked_at is not None or (rec.expires_at and rec.expires_at <= t):
        return None
    user = await db.get(m.User, rec.user_id)
    if user is None or not user.is_active:
        return None
    if rec.last_used_at is None or (t - rec.last_used_at).total_seconds() > 60:
        rec.last_used_at = t
    return rec, user


# --------------------------------------------------------------------------- provisioning


async def sync_groups_and_roles(
    db: AsyncSession, settings: Settings, user: m.User, group_names: list[str]
) -> list[str]:
    """Replace the user's IdP groups and IdP-derived roles. Manual roles are kept."""
    roles = await ensure_builtin_roles(db)
    group_names = sorted({g for g in group_names if isinstance(g, str) and g})

    existing_groups = {
        g.name: g
        for g in (await db.execute(select(m.Group).where(m.Group.name.in_(group_names)))).scalars()
    } if group_names else {}
    for name in group_names:
        if name not in existing_groups:
            grp = m.Group(name=name, source="oidc")
            db.add(grp)
            existing_groups[name] = grp
    await db.flush()
    await db.execute(delete(m.UserGroup).where(m.UserGroup.user_id == user.id))
    for grp in existing_groups.values():
        db.add(m.UserGroup(user_id=user.id, group_id=grp.id))

    wanted: set[uuid.UUID] = set()
    for role_name, groups in settings.oidc_group_roles.items():
        if set(groups) & set(group_names) and role_name in roles:
            wanted.add(roles[role_name].id)
    if group_names:
        mapped = await db.execute(
            select(m.GroupRoleMapping.role_id).where(m.GroupRoleMapping.group_name.in_(group_names))
        )
        wanted.update(mapped.scalars())

    await db.execute(
        delete(m.UserRole).where(m.UserRole.user_id == user.id, m.UserRole.source == "oidc")
    )
    manual = set(
        (await db.execute(select(m.UserRole.role_id).where(m.UserRole.user_id == user.id))).scalars()
    )
    if not wanted and not manual and settings.OIDC_DEFAULT_ROLE != "none":
        wanted.add(roles[settings.OIDC_DEFAULT_ROLE].id)
    for rid in wanted - manual:
        db.add(m.UserRole(user_id=user.id, role_id=rid, source="oidc"))
    await db.flush()
    names = (
        await db.execute(
            select(m.Role.name).join(m.UserRole, m.UserRole.role_id == m.Role.id).where(
                m.UserRole.user_id == user.id
            )
        )
    ).scalars()
    return sorted(names)


async def unique_username(db: AsyncSession, wanted: str) -> str:
    from sqlalchemy import func

    base = (wanted or "user").strip()[:200] or "user"
    candidate = base
    n = 1
    while (
        await db.execute(select(m.User.id).where(func.lower(m.User.username) == candidate.lower()))
    ).first():
        n += 1
        candidate = f"{base}-{n}"
    return candidate
