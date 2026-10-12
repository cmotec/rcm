"""Resource access control lists."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.api.deps import Principal, get_access
from app.db import get_db
from app.errors import Forbidden, NotFound, ValidationFailed
from app.schemas.core import AclEntryIn, AclEntryOut, EffectiveAccessOut
from app.services import audit
from app.services.authz import LEVEL_NAMES, ROOT_ID, Perm, build_access_context

router = APIRouter(prefix="/acl", tags=["permissions"])

ResourceType = Literal["folder", "connection", "credential"]
MODELS = {"folder": m.Folder, "connection": m.Connection, "credential": m.Credential}


async def _check_resource(db: AsyncSession, principal: Principal, rtype: str, rid: uuid.UUID, needed: str) -> None:
    access = principal.access
    if rid == ROOT_ID:
        if rtype == "connection":
            raise ValidationFailed("root grants use resource type 'folder' or 'credential'")
        if not access.is_admin:
            raise Forbidden("only administrators can manage root-level grants")
        return
    if await db.get(MODELS[rtype], rid) is None or not access.can(rtype, rid, "view"):
        raise NotFound(f"{rtype} not found")
    access.ensure(rtype, rid, needed)


async def _principal_names(db: AsyncSession, entries: list[m.AclEntry]) -> dict[tuple[str, uuid.UUID], str]:
    names: dict[tuple[str, uuid.UUID], str] = {}
    for ptype, model, col in (("user", m.User, m.User.username), ("group", m.Group, m.Group.name),
                              ("role", m.Role, m.Role.name)):
        ids = [e.principal_id for e in entries if e.principal_type == ptype]
        if ids:
            for pid, name in (await db.execute(select(model.id, col).where(model.id.in_(ids)))).all():
                names[(ptype, pid)] = name
    return names


@router.get("/{resource_type}/{resource_id}", response_model=list[AclEntryOut],
            summary="List grants on a resource (use the all-zero UUID for root grants)")
async def get_acl(resource_type: ResourceType, resource_id: uuid.UUID, principal: Principal = Depends(get_access),
                  db: AsyncSession = Depends(get_db, scope="function")):
    await _check_resource(db, principal, resource_type, resource_id, "view")
    rows = (await db.execute(select(m.AclEntry).where(m.AclEntry.resource_type == resource_type,
                                                      m.AclEntry.resource_id == resource_id))).scalars().all()
    names = await _principal_names(db, list(rows))
    out = []
    for r in rows:
        item = AclEntryOut.model_validate(r)
        item.principal_name = names.get((r.principal_type, r.principal_id))
        out.append(item)
    return out


@router.put("/{resource_type}/{resource_id}", response_model=list[AclEntryOut],
            summary="Replace all grants on a resource")
async def put_acl(resource_type: ResourceType, resource_id: uuid.UUID, body: list[AclEntryIn],
                  principal: Principal = Depends(get_access), db: AsyncSession = Depends(get_db, scope="function")):
    principal.access.require(Perm.ACL_MANAGE)
    await _check_resource(db, principal, resource_type, resource_id, "manage")
    seen: set[tuple[str, uuid.UUID]] = set()
    for e in body:
        key = (e.principal_type, e.principal_id)
        if key in seen:
            raise ValidationFailed("duplicate principal in ACL")
        seen.add(key)
        model = {"user": m.User, "group": m.Group, "role": m.Role}[e.principal_type]
        if await db.get(model, e.principal_id) is None:
            raise ValidationFailed(f"{e.principal_type} {e.principal_id} not found")
    before = (await db.execute(select(m.AclEntry).where(m.AclEntry.resource_type == resource_type,
                                                        m.AclEntry.resource_id == resource_id))).scalars().all()
    await db.execute(delete(m.AclEntry).where(m.AclEntry.resource_type == resource_type,
                                              m.AclEntry.resource_id == resource_id))
    for e in body:
        db.add(m.AclEntry(resource_type=resource_type, resource_id=resource_id, principal_type=e.principal_type,
                          principal_id=e.principal_id, permission=e.permission, created_by=principal.id))
    await db.flush()
    await audit.record(db, "acl.update", actor_id=principal.id, actor_name=principal.name,
                       resource_type=resource_type, resource_id=resource_id,
                       details={"before": [{"type": b.principal_type, "id": str(b.principal_id), "perm": b.permission}
                                           for b in before],
                                "after": [{"type": e.principal_type, "id": str(e.principal_id), "perm": e.permission}
                                          for e in body]})
    return await get_acl(resource_type, resource_id, principal, db)  # type: ignore[arg-type]


@router.get("/effective", response_model=EffectiveAccessOut, summary="Effective permission of a user on a resource")
async def effective_access(
    resource_type: ResourceType = Query(...),
    resource_id: uuid.UUID | None = Query(None),
    user_id: uuid.UUID | None = Query(None, description="defaults to the caller; others need users.manage"),
    principal: Principal = Depends(get_access),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    target = principal.user
    access = principal.access
    if user_id and user_id != principal.id:
        principal.access.require(Perm.USERS_MANAGE)
        target_user = await db.get(m.User, user_id)
        if target_user is None:
            raise NotFound("user not found")
        target = target_user
        access = await (await build_access_context(db, target)).load(db)
    level = access.level_of(resource_type, resource_id)
    return EffectiveAccessOut(resource_type=resource_type, resource_id=resource_id, user_id=target.id,
                              permission=LEVEL_NAMES.get(level) if level else None,
                              global_permissions=sorted(access.permissions))
