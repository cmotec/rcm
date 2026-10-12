"""Administration: users, roles, groups, IdP group mappings, known hosts, audit log, settings."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import asyncssh
from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.api.common import Paging, page, paging
from app.api.deps import Principal, get_access, require, settings_dep
from app.config import Settings
from app.db import get_db
from app.errors import Conflict, Forbidden, NotFound, ValidationFailed
from app.schemas.core import (
    AppSettingsIn,
    AppSettingsOut,
    AuditEventOut,
    GroupMappingCreate,
    GroupMappingOut,
    KnownHostCreate,
    KnownHostOut,
    Page,
    RoleCreate,
    RoleOut,
    RoleUpdate,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.security.passwords import hash_password
from app.services import app_settings, audit
from app.services.authz import ALL_PERMISSIONS, Perm
from app.services.sessions.manager import get_manager

router = APIRouter(tags=["administration"])


# --------------------------------------------------------------------------- users


async def _user_out(db: AsyncSession, u: m.User) -> UserOut:
    out = UserOut.model_validate(u)
    out.roles = sorted((await db.execute(select(m.Role.name).join(m.UserRole, m.UserRole.role_id == m.Role.id)
                                         .where(m.UserRole.user_id == u.id))).scalars())
    out.groups = sorted((await db.execute(select(m.Group.name).join(m.UserGroup, m.UserGroup.group_id == m.Group.id)
                                          .where(m.UserGroup.user_id == u.id))).scalars())
    return out


async def _set_manual_roles(db: AsyncSession, user: m.User, names: list[str]) -> None:
    roles = {r.name: r for r in (await db.execute(select(m.Role).where(m.Role.name.in_(names)))).scalars()} if names else {}
    missing = set(names) - set(roles)
    if missing:
        raise ValidationFailed(f"unknown role(s): {', '.join(sorted(missing))}")
    await db.execute(delete(m.UserRole).where(m.UserRole.user_id == user.id, m.UserRole.source != "oidc"))
    oidc = set((await db.execute(select(m.UserRole.role_id).where(m.UserRole.user_id == user.id))).scalars())
    for r in roles.values():
        if r.id not in oidc:
            db.add(m.UserRole(user_id=user.id, role_id=r.id, source="manual"))
    await db.flush()


SIGNAGE_PROTOCOLS = ("rdp", "vnc")


async def _set_signage(db: AsyncSession, principal: Principal, user: m.User, enabled: bool,
                       connection_id: uuid.UUID | None) -> bool:
    """Apply the signage assignment; returns True if it changed."""
    if enabled:
        if user.id == principal.id:
            raise ValidationFailed("you cannot turn your own account into a signage account")
        if connection_id is not None:
            conn = await db.get(m.Connection, connection_id)
            if conn is None:
                raise ValidationFailed("signage connection not found")
            if conn.protocol not in SIGNAGE_PROTOCOLS:
                raise ValidationFailed("a signage connection must be an RDP or VNC connection")
    new_conn = connection_id if enabled else None
    changed = user.is_signage != enabled or user.signage_connection_id != new_conn
    user.is_signage, user.signage_connection_id = enabled, new_conn
    return changed


@router.get("/users", response_model=Page[UserOut], summary="List users")
async def list_users(q: str | None = Query(None, max_length=200), p: Paging = Depends(paging),
                     principal: Principal = Depends(require(Perm.USERS_MANAGE)), db: AsyncSession = Depends(get_db, scope="function")):
    stmt = select(m.User)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(func.lower(m.User.username).like(like), func.lower(m.User.email).like(like),
                              func.lower(m.User.display_name).like(like)))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(func.lower(m.User.username)).offset(p.offset).limit(p.limit))).scalars()
    return page([await _user_out(db, u) for u in rows], total, p)


@router.get("/users/lookup", summary="Minimal user directory for ACL editing")
async def lookup_users(q: str = Query(..., min_length=1, max_length=100), principal: Principal = Depends(get_access),
                       db: AsyncSession = Depends(get_db, scope="function")) -> list[dict[str, Any]]:
    principal.access.require(Perm.ACL_MANAGE)
    like = f"%{q.lower()}%"
    rows = (await db.execute(select(m.User.id, m.User.username, m.User.display_name)
                             .where(m.User.is_active, or_(func.lower(m.User.username).like(like),
                                                          func.lower(m.User.display_name).like(like))).limit(20))).all()
    return [{"id": str(r.id), "username": r.username, "display_name": r.display_name} for r in rows]


@router.post("/users", response_model=UserOut, status_code=201, summary="Create a user (local or pre-provisioned)")
async def create_user(body: UserCreate, principal: Principal = Depends(require(Perm.USERS_MANAGE)),
                      db: AsyncSession = Depends(get_db, scope="function"), settings: Settings = Depends(settings_dep)):
    exists = (await db.execute(select(m.User.id).where(func.lower(m.User.username) == body.username.lower()))).first()
    if exists:
        raise Conflict("username already exists")
    if body.password and not settings.LOCAL_AUTH_ENABLED:
        raise ValidationFailed("local accounts require LOCAL_AUTH_ENABLED=true")
    user = m.User(username=body.username, email=body.email, display_name=body.display_name,
                  is_local=bool(body.password), password_hash=hash_password(body.password) if body.password else None)
    db.add(user)
    await db.flush()
    await _set_manual_roles(db, user, body.roles)
    await _set_signage(db, principal, user, body.is_signage, body.signage_connection_id)
    await audit.record(db, "user.create", actor_id=principal.id, actor_name=principal.name, resource_type="user",
                       resource_id=user.id, details={"username": user.username, "roles": body.roles,
                                                     "local": user.is_local, "signage": user.is_signage,
                                                     "signage_connection_id": str(user.signage_connection_id)
                                                     if user.signage_connection_id else None})
    await db.refresh(user)
    return await _user_out(db, user)


@router.get("/users/{user_id}", response_model=UserOut, summary="Get a user")
async def get_user(user_id: uuid.UUID, principal: Principal = Depends(require(Perm.USERS_MANAGE)),
                   db: AsyncSession = Depends(get_db, scope="function")):
    user = await db.get(m.User, user_id)
    if user is None:
        raise NotFound("user not found")
    return await _user_out(db, user)


@router.patch("/users/{user_id}", response_model=UserOut, summary="Update a user")
async def update_user(user_id: uuid.UUID, body: UserUpdate, principal: Principal = Depends(require(Perm.USERS_MANAGE)),
                      db: AsyncSession = Depends(get_db, scope="function")):
    user = await db.get(m.User, user_id)
    if user is None:
        raise NotFound("user not found")
    fields = body.model_fields_set
    if user.id == principal.id and ("is_active" in fields and body.is_active is False):
        raise ValidationFailed("you cannot deactivate yourself")
    for f in ("email", "display_name"):
        if f in fields:
            setattr(user, f, getattr(body, f))
    if "is_active" in fields and body.is_active is not None:
        user.is_active = body.is_active
        if not body.is_active:
            await db.execute(delete(m.WebSession).where(m.WebSession.user_id == user.id))
            await get_manager().close_all_for_user(user.id, "user_deactivated")
    if "password" in fields and body.password:
        if not user.is_local:
            raise ValidationFailed("only local accounts have passwords")
        user.password_hash = hash_password(body.password)
        await db.execute(delete(m.WebSession).where(m.WebSession.user_id == user.id))
    if "roles" in fields and body.roles is not None:
        if user.id == principal.id and "admin" not in body.roles and principal.access.is_admin:
            raise ValidationFailed("you cannot remove your own administrator role")
        await _set_manual_roles(db, user, body.roles)
    if fields & {"is_signage", "signage_connection_id"}:
        enabled = body.is_signage if "is_signage" in fields and body.is_signage is not None else user.is_signage
        conn_id = body.signage_connection_id if "signage_connection_id" in fields else user.signage_connection_id
        if await _set_signage(db, principal, user, enabled, conn_id):
            # live sessions were opened under the previous permissions
            await get_manager().close_all_for_user(user.id, "permissions_changed")
    await audit.record(db, "user.update", actor_id=principal.id, actor_name=principal.name, resource_type="user",
                       resource_id=user.id, details={"fields": sorted(fields - {"password"}),
                                                     "password_changed": "password" in fields})
    await db.flush()
    await db.refresh(user)
    return await _user_out(db, user)


@router.delete("/users/{user_id}", status_code=204, summary="Delete a user")
async def delete_user(user_id: uuid.UUID, principal: Principal = Depends(require(Perm.USERS_MANAGE)),
                      db: AsyncSession = Depends(get_db, scope="function")) -> None:
    if user_id == principal.id:
        raise ValidationFailed("you cannot delete yourself")
    user = await db.get(m.User, user_id)
    if user is None:
        raise NotFound("user not found")
    await get_manager().close_all_for_user(user.id, "user_deleted")
    await db.execute(delete(m.AclEntry).where(m.AclEntry.principal_type == "user", m.AclEntry.principal_id == user.id))
    await db.delete(user)
    await audit.record(db, "user.delete", actor_id=principal.id, actor_name=principal.name, resource_type="user",
                       resource_id=user_id, details={"username": user.username})


# --------------------------------------------------------------------------- roles & groups


@router.get("/permissions", summary="All global permission names")
async def list_permissions(principal: Principal = Depends(get_access)) -> list[str]:
    return ALL_PERMISSIONS


@router.get("/roles", response_model=list[RoleOut], summary="List roles")
async def list_roles(principal: Principal = Depends(get_access), db: AsyncSession = Depends(get_db, scope="function")):
    return (await db.execute(select(m.Role).order_by(m.Role.name))).scalars().all()


@router.post("/roles", response_model=RoleOut, status_code=201, summary="Create a custom role")
async def create_role(body: RoleCreate, principal: Principal = Depends(require(Perm.ROLES_MANAGE)),
                      db: AsyncSession = Depends(get_db, scope="function")):
    unknown = set(body.permissions) - set(ALL_PERMISSIONS)
    if unknown:
        raise ValidationFailed(f"unknown permission(s): {', '.join(sorted(unknown))}")
    if "*" in body.permissions:
        raise ValidationFailed("'*' is reserved for the built-in admin role")
    role = m.Role(name=body.name, description=body.description, permissions=body.permissions,
                  resource_cap=body.resource_cap, builtin=False)
    db.add(role)
    await db.flush()
    await audit.record(db, "role.create", actor_id=principal.id, actor_name=principal.name, resource_type="role",
                       resource_id=role.id, details=body.model_dump())
    await db.refresh(role)
    return role


@router.patch("/roles/{role_id}", response_model=RoleOut, summary="Update a custom role")
async def update_role(role_id: uuid.UUID, body: RoleUpdate, principal: Principal = Depends(require(Perm.ROLES_MANAGE)),
                      db: AsyncSession = Depends(get_db, scope="function")):
    role = await db.get(m.Role, role_id)
    if role is None:
        raise NotFound("role not found")
    if role.builtin:
        raise Forbidden("built-in roles are defined by the application")
    fields = body.model_fields_set
    if "permissions" in fields and body.permissions is not None:
        unknown = set(body.permissions) - set(ALL_PERMISSIONS)
        if unknown or "*" in body.permissions:
            raise ValidationFailed("invalid permission list")
        role.permissions = body.permissions
    if "description" in fields:
        role.description = body.description
    if "resource_cap" in fields and body.resource_cap:
        role.resource_cap = body.resource_cap
    await audit.record(db, "role.update", actor_id=principal.id, actor_name=principal.name, resource_type="role",
                       resource_id=role.id, details=body.model_dump(exclude_unset=True))
    await db.flush()
    await db.refresh(role)
    return role


@router.delete("/roles/{role_id}", status_code=204, summary="Delete a custom role")
async def delete_role(role_id: uuid.UUID, principal: Principal = Depends(require(Perm.ROLES_MANAGE)),
                      db: AsyncSession = Depends(get_db, scope="function")) -> None:
    role = await db.get(m.Role, role_id)
    if role is None:
        raise NotFound("role not found")
    if role.builtin:
        raise Forbidden("built-in roles cannot be deleted")
    await db.execute(delete(m.AclEntry).where(m.AclEntry.principal_type == "role", m.AclEntry.principal_id == role.id))
    await db.delete(role)
    await audit.record(db, "role.delete", actor_id=principal.id, actor_name=principal.name, resource_type="role",
                       resource_id=role_id, details={"name": role.name})


@router.get("/groups", summary="Known groups (synchronised from the identity provider)")
async def list_groups(principal: Principal = Depends(get_access), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict]:
    if not (principal.access.has(Perm.USERS_MANAGE) or principal.access.has(Perm.ACL_MANAGE)):
        raise Forbidden()
    rows = (await db.execute(select(m.Group.id, m.Group.name, m.Group.source,
                                    func.count(m.UserGroup.user_id).label("members"))
                             .outerjoin(m.UserGroup, m.UserGroup.group_id == m.Group.id)
                             .group_by(m.Group.id).order_by(m.Group.name))).all()
    return [{"id": str(r.id), "name": r.name, "source": r.source, "members": r.members} for r in rows]


@router.get("/group-mappings", response_model=list[GroupMappingOut], summary="IdP group → role mappings")
async def list_group_mappings(principal: Principal = Depends(require(Perm.ROLES_MANAGE)),
                              db: AsyncSession = Depends(get_db, scope="function"), settings: Settings = Depends(settings_dep)):
    return (await db.execute(select(m.GroupRoleMapping).order_by(m.GroupRoleMapping.group_name))).scalars().all()


@router.get("/group-mappings/env", summary="Group mappings configured through environment variables (read-only)")
async def env_group_mappings(principal: Principal = Depends(require(Perm.ROLES_MANAGE)),
                             settings: Settings = Depends(settings_dep)) -> dict[str, list[str]]:
    return settings.oidc_group_roles


@router.post("/group-mappings", response_model=GroupMappingOut, status_code=201, summary="Add a group mapping")
async def add_group_mapping(body: GroupMappingCreate, principal: Principal = Depends(require(Perm.ROLES_MANAGE)),
                            db: AsyncSession = Depends(get_db, scope="function")):
    role = (await db.execute(select(m.Role).where(m.Role.name == body.role))).scalar_one_or_none()
    if role is None:
        raise ValidationFailed("unknown role")
    mapping = m.GroupRoleMapping(group_name=body.group_name, role_id=role.id)
    db.add(mapping)
    await db.flush()
    await audit.record(db, "group_mapping.create", actor_id=principal.id, actor_name=principal.name,
                       resource_type="group_mapping", resource_id=mapping.id, details=body.model_dump())
    await db.refresh(mapping)
    return mapping


@router.delete("/group-mappings/{mapping_id}", status_code=204, summary="Remove a group mapping")
async def delete_group_mapping(mapping_id: uuid.UUID, principal: Principal = Depends(require(Perm.ROLES_MANAGE)),
                               db: AsyncSession = Depends(get_db, scope="function")) -> None:
    mapping = await db.get(m.GroupRoleMapping, mapping_id)
    if mapping is None:
        raise NotFound("mapping not found")
    await db.delete(mapping)
    await audit.record(db, "group_mapping.delete", actor_id=principal.id, actor_name=principal.name,
                       resource_type="group_mapping", resource_id=mapping_id, details={"group": mapping.group_name})


# --------------------------------------------------------------------------- known hosts


@router.get("/known-hosts", response_model=Page[KnownHostOut], summary="Trusted SSH host keys")
async def list_known_hosts(q: str | None = Query(None, max_length=255), p: Paging = Depends(paging),
                           principal: Principal = Depends(get_access), db: AsyncSession = Depends(get_db, scope="function")):
    stmt = select(m.KnownHost)
    if q:
        stmt = stmt.where(func.lower(m.KnownHost.hostname).like(f"%{q.lower()}%"))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(m.KnownHost.hostname, m.KnownHost.port).offset(p.offset).limit(p.limit))).scalars().all()
    return page(list(rows), total, p)


@router.post("/known-hosts", response_model=KnownHostOut, status_code=201, summary="Pin an SSH host key")
async def add_known_host(body: KnownHostCreate, principal: Principal = Depends(require(Perm.KNOWN_HOSTS_MANAGE)),
                         db: AsyncSession = Depends(get_db, scope="function")):
    try:
        key = asyncssh.import_public_key(body.public_key)
    except (asyncssh.KeyImportError, ValueError) as exc:
        raise ValidationFailed(f"invalid public key: {exc}") from exc
    row = m.KnownHost(hostname=body.hostname, port=body.port, key_type=key.get_algorithm(),
                      fingerprint=key.get_fingerprint("sha256"),
                      public_key=key.export_public_key("openssh").decode().strip(), added_by=principal.id)
    db.add(row)
    await db.flush()
    await audit.record(db, "known_host.add", actor_id=principal.id, actor_name=principal.name,
                       resource_type="known_host", resource_id=row.id,
                       details={"hostname": row.hostname, "port": row.port, "fingerprint": row.fingerprint})
    await db.refresh(row)
    return row


@router.delete("/known-hosts/{host_id}", status_code=204, summary="Remove a trusted host key")
async def delete_known_host(host_id: uuid.UUID, principal: Principal = Depends(require(Perm.KNOWN_HOSTS_MANAGE)),
                            db: AsyncSession = Depends(get_db, scope="function")) -> None:
    row = await db.get(m.KnownHost, host_id)
    if row is None:
        raise NotFound("known host not found")
    await db.delete(row)
    await audit.record(db, "known_host.delete", actor_id=principal.id, actor_name=principal.name,
                       resource_type="known_host", resource_id=host_id,
                       details={"hostname": row.hostname, "port": row.port, "fingerprint": row.fingerprint})


# --------------------------------------------------------------------------- audit


@router.get("/audit", response_model=Page[AuditEventOut], summary="Query the audit log")
async def query_audit(
    action: str | None = Query(None, description="exact action or prefix ending in '*' (e.g. 'session.*')"),
    actor_id: uuid.UUID | None = Query(None),
    resource_type: str | None = Query(None),
    resource_id: str | None = Query(None),
    outcome: str | None = Query(None, pattern="^(success|failure|denied)$"),
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    p: Paging = Depends(paging),
    principal: Principal = Depends(get_access),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    stmt = select(m.AuditEvent)
    if not principal.access.has(Perm.AUDIT_READ):
        stmt = stmt.where(m.AuditEvent.actor_id == principal.id)  # everyone may read their own trail
    if action:
        stmt = stmt.where(m.AuditEvent.action.like(action[:-1] + "%") if action.endswith("*")
                          else m.AuditEvent.action == action)
    if actor_id:
        stmt = stmt.where(m.AuditEvent.actor_id == actor_id)
    if resource_type:
        stmt = stmt.where(m.AuditEvent.resource_type == resource_type)
    if resource_id:
        stmt = stmt.where(m.AuditEvent.resource_id == resource_id)
    if outcome:
        stmt = stmt.where(m.AuditEvent.outcome == outcome)
    if since:
        stmt = stmt.where(m.AuditEvent.ts >= since)
    if until:
        stmt = stmt.where(m.AuditEvent.ts < until)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(m.AuditEvent.ts.desc(), m.AuditEvent.id.desc())
                             .offset(p.offset).limit(p.limit))).scalars().all()
    return page(list(rows), total, p)


# --------------------------------------------------------------------------- settings


@router.get("/settings", response_model=AppSettingsOut, summary="Global runtime settings")
async def get_app_settings(principal: Principal = Depends(get_access), db: AsyncSession = Depends(get_db, scope="function"),
                           settings: Settings = Depends(settings_dep)) -> AppSettingsOut:
    app_settings.invalidate()
    runtime = await app_settings.get_runtime(db, settings)
    return AppSettingsOut(values=runtime.as_dict(), defaults=app_settings.env_defaults(settings).as_dict())


@router.patch("/settings", response_model=AppSettingsOut, summary="Change global runtime settings")
async def patch_app_settings(body: AppSettingsIn, principal: Principal = Depends(require(Perm.SETTINGS_MANAGE)),
                             db: AsyncSession = Depends(get_db, scope="function"), settings: Settings = Depends(settings_dep)):
    changes = body.model_dump(exclude_unset=True)
    if changes.get("rdp_cert_allow_insecure") and not settings.RDP_CERT_ALLOW_INSECURE:
        raise ValidationFailed("insecure RDP certificate policies are disabled by the server configuration")
    for key, value in changes.items():
        row = await db.get(m.AppSetting, key)
        if value is None:
            if row is not None:
                await db.delete(row)
            continue
        if row is None:
            db.add(m.AppSetting(key=key, value=value, updated_by=principal.id))
        else:
            row.value = value
            row.updated_by = principal.id
    await db.flush()
    await audit.record(db, "settings.update", actor_id=principal.id, actor_name=principal.name,
                       resource_type="settings", details=changes)
    app_settings.invalidate()
    return await get_app_settings(principal, db, settings)
