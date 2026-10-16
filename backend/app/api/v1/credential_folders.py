"""Folders of the credential store. Grants on a folder (ACL resource type ``cred_folder``) inherit to
the folders and credentials below it."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.api.common import check_version
from app.api.deps import Principal, get_access
from app.db import get_db
from app.errors import Conflict, Forbidden, NotFound, ValidationFailed
from app.schemas.core import CredentialFolderCreate, CredentialFolderOut, CredentialFolderUpdate
from app.services import audit
from app.services.authz import LEVEL_NAMES, AccessContext, Perm
from app.services.resolution import would_create_cycle

router = APIRouter(prefix="/credential-folders", tags=["credentials"])


def folder_out(f: m.CredentialFolder, access: AccessContext) -> CredentialFolderOut:
    out = CredentialFolderOut.model_validate(f)
    out.permission = LEVEL_NAMES.get(access.level_of("cred_folder", f.id)) or None
    return out


async def _get(db: AsyncSession, access: AccessContext, fid: uuid.UUID, needed: str) -> m.CredentialFolder:
    folder = await db.get(m.CredentialFolder, fid)
    if folder is None or not access.can("cred_folder", fid, "view"):
        raise NotFound("credential folder not found")
    access.ensure("cred_folder", fid, needed)
    return folder


@router.get("", response_model=list[CredentialFolderOut], summary="List visible credential folders")
async def list_folders(principal: Principal = Depends(get_access),
                       db: AsyncSession = Depends(get_db, scope="function")):
    access = principal.access
    ids = access.visible_cred_folder_ids()
    if not ids:
        return []
    rows = (await db.execute(select(m.CredentialFolder).where(m.CredentialFolder.id.in_(ids))
                             .order_by(func.lower(m.CredentialFolder.name)))).scalars()
    return [folder_out(f, access) for f in rows]


@router.post("", response_model=CredentialFolderOut, status_code=201, summary="Create a credential folder")
async def create_folder(body: CredentialFolderCreate, principal: Principal = Depends(get_access),
                        db: AsyncSession = Depends(get_db, scope="function")):
    access = principal.access
    access.require(Perm.CREDENTIALS_CREATE)
    if body.parent_id:
        await _get(db, access, body.parent_id, "edit")
    folder = m.CredentialFolder(id=uuid.uuid4(), name=body.name, parent_id=body.parent_id,
                                description=body.description, owner_id=principal.id)
    db.add(folder)
    await db.flush()
    await db.refresh(folder)
    access.invalidate()
    await access.load(db)
    await audit.record(db, "credential_folder.create", actor_id=principal.id, actor_name=principal.name,
                       resource_type="cred_folder", resource_id=folder.id, details={"name": folder.name})
    return folder_out(folder, access)


@router.patch("/{folder_id}", response_model=CredentialFolderOut, summary="Rename / move a credential folder")
async def update_folder(folder_id: uuid.UUID, body: CredentialFolderUpdate, principal: Principal = Depends(get_access),
                        db: AsyncSession = Depends(get_db, scope="function")):
    access = principal.access
    folder = await _get(db, access, folder_id, "edit")
    check_version(folder.version, body.version)
    fields = body.model_fields_set - {"version"}
    if "parent_id" in fields and body.parent_id != folder.parent_id:
        access.ensure("cred_folder", folder.id, "manage")
        if body.parent_id is not None:
            await _get(db, access, body.parent_id, "edit")
            if await would_create_cycle(db, m.CredentialFolder, folder.id, body.parent_id):
                raise ValidationFailed("a folder cannot be moved into itself or a folder below it")
        elif not access.has(Perm.CREDENTIALS_CREATE):
            raise Forbidden("not allowed to move folders to the top level")
        folder.parent_id = body.parent_id
    if "name" in fields and body.name:
        folder.name = body.name
    if "description" in fields:
        folder.description = body.description
    folder.version += 1
    await db.flush()
    await db.refresh(folder)
    access.invalidate()
    await access.load(db)
    await audit.record(db, "credential_folder.update", actor_id=principal.id, actor_name=principal.name,
                       resource_type="cred_folder", resource_id=folder.id, details={"fields": sorted(fields)})
    return folder_out(folder, access)


@router.delete("/{folder_id}", status_code=204, summary="Delete a credential folder")
async def delete_folder(folder_id: uuid.UUID,
                        move_contents: bool = Query(False, description="move credentials and sub-folders to the "
                                                                       "parent folder instead of refusing"),
                        principal: Principal = Depends(get_access),
                        db: AsyncSession = Depends(get_db, scope="function")) -> None:
    access = principal.access
    folder = await _get(db, access, folder_id, "manage")
    subs = (await db.execute(select(m.CredentialFolder).where(m.CredentialFolder.parent_id == folder.id))).scalars().all()
    creds = (await db.execute(select(m.Credential).where(m.Credential.folder_id == folder.id))).scalars().all()
    if (subs or creds) and not move_contents:
        raise Conflict(f"folder contains {len(creds)} credential(s) and {len(subs)} folder(s)", code="folder_not_empty")
    # credentials are never deleted with their folder: they move up one level
    for sub in subs:
        sub.parent_id = folder.parent_id
    for cred in creds:
        cred.folder_id = folder.parent_id
    await db.flush()
    await db.delete(folder)
    await db.flush()
    await audit.record(db, "credential_folder.delete", actor_id=principal.id, actor_name=principal.name,
                       resource_type="cred_folder", resource_id=folder_id,
                       details={"name": folder.name, "moved_credentials": len(creds), "moved_folders": len(subs)})
