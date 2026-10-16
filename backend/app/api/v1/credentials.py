"""Credential store."""

from __future__ import annotations

import uuid
from datetime import timedelta

import asyncssh
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.api.common import Paging, check_version, idempotency, page, paging
from app.api.deps import Principal, get_access
from app.db import get_db
from app.errors import Conflict, Forbidden, NotFound, ValidationFailed
from app.schemas.core import (
    CredentialCreate,
    CredentialOut,
    CredentialReveal,
    CredentialSecrets,
    CredentialUpdate,
    Page,
)
from app.security.crypto import aad_for, fingerprint, get_box
from app.services import audit
from app.services.auth import now
from app.services.authz import LEVEL_NAMES, AccessContext, Perm
from app.services.credential_attributes import (
    get_attributes,
    get_secret_attributes,
    secret_attribute_names,
    set_attributes,
    set_secret_attributes,
)
from app.services.resolution import would_create_cycle

router = APIRouter(prefix="/credentials", tags=["credentials"])

SECRET_FIELDS = ("password", "private_key", "passphrase")


def _key_fingerprint(private_key: str | None, passphrase: str | None) -> str | None:
    if not private_key:
        return None
    try:
        key = asyncssh.import_private_key(private_key, passphrase or None)
        return key.convert_to_public().get_fingerprint("sha256")
    except (asyncssh.KeyImportError, ValueError, TypeError):
        return None


async def credential_out(db: AsyncSession, c: m.Credential, access: AccessContext) -> CredentialOut:
    mapped = (await db.execute(select(m.SecretMapping.field).where(m.SecretMapping.credential_id == c.id))).scalars()
    usage = (await db.execute(select(func.count()).select_from(m.Connection)
                              .where(m.Connection.credential_id == c.id))).scalar_one()
    rotation_due = bool(
        c.rotation_interval_days
        and (c.secret_changed_at or c.created_at) + timedelta(days=c.rotation_interval_days) < now()
    )
    return CredentialOut(
        id=c.id, name=c.name, parent_id=c.parent_id, username=c.username, domain=c.domain,
        password_set=c.password_enc is not None, private_key_set=c.private_key_enc is not None,
        passphrase_set=c.passphrase_enc is not None,
        private_key_fingerprint=(c.meta or {}).get("_key_fingerprint"),
        description=c.description, metadata={k: v for k, v in (c.meta or {}).items() if not k.startswith("_")},
        owner_id=c.owner_id, rotation_interval_days=c.rotation_interval_days, secret_changed_at=c.secret_changed_at,
        rotation_due=rotation_due, version=c.version, created_at=c.created_at, updated_at=c.updated_at,
        permission=LEVEL_NAMES.get(access.level_of("credential", c.id)) or None,
        external_fields=sorted(mapped), usage_count=usage,
        folder_id=c.folder_id, attributes=get_attributes(c), secret_attribute_names=secret_attribute_names(c),
    )


async def check_folder_target(db: AsyncSession, access: AccessContext, folder_id: uuid.UUID | None) -> None:
    """Putting a credential into a folder needs 'edit' on the folder; the top level needs the create right."""
    if folder_id is None:
        if not access.has(Perm.CREDENTIALS_CREATE):
            raise Forbidden("not allowed to put credentials at the top level")
        return
    if await db.get(m.CredentialFolder, folder_id) is None or not access.can("cred_folder", folder_id, "view"):
        raise ValidationFailed("credential folder not found")
    access.ensure("cred_folder", folder_id, "edit")


async def _get(db: AsyncSession, access: AccessContext, cid: uuid.UUID, needed: str) -> m.Credential:
    cred = await db.get(m.Credential, cid)
    if cred is None or not access.can("credential", cid, "view"):
        raise NotFound("credential not found")
    access.ensure("credential", cid, needed)
    return cred


def _set_secret(cred: m.Credential, fname: str, value: str | None) -> None:
    setattr(cred, f"{fname}_enc", get_box().encrypt(value, aad_for("credentials", fname, cred.id)) if value else None)


def _refresh_key_fingerprint(cred: m.Credential) -> None:
    box = get_box()
    pk = box.decrypt(cred.private_key_enc, aad_for("credentials", "private_key", cred.id)) if cred.private_key_enc else None
    pp = box.decrypt(cred.passphrase_enc, aad_for("credentials", "passphrase", cred.id)) if cred.passphrase_enc else None
    meta = dict(cred.meta or {})
    fp = _key_fingerprint(pk, pp)
    if fp:
        meta["_key_fingerprint"] = fp
    else:
        meta.pop("_key_fingerprint", None)
    cred.meta = meta


def local_hash(cred: m.Credential, fname: str) -> str | None:
    """Fingerprint of a field's current value, for secret-sync conflict detection."""
    if fname.startswith(m.ATTRIBUTE_FIELD_PREFIX):
        v = get_attributes(cred).get(fname[len(m.ATTRIBUTE_FIELD_PREFIX):])
        return fingerprint(str(v)) if v not in (None, "") else None
    if fname.startswith(m.SECRET_ATTRIBUTE_FIELD_PREFIX):
        v = get_secret_attributes(cred).get(fname[len(m.SECRET_ATTRIBUTE_FIELD_PREFIX):])
        return fingerprint(str(v)) if v not in (None, "") else None
    if fname in ("username", "domain"):
        v = getattr(cred, fname)
    else:
        blob = getattr(cred, f"{fname}_enc")
        v = get_box().decrypt(blob, aad_for("credentials", fname, cred.id)) if blob else None
    return fingerprint(v) if v else None


@router.get("", response_model=Page[CredentialOut], summary="List credentials (metadata only)")
async def list_credentials(q: str | None = Query(None, max_length=200), p: Paging = Depends(paging),
                           folder_id: uuid.UUID | None = Query(None, description="only this credential folder…"),
                           recursive: bool = Query(True, description="…and the folders below it"),
                           principal: Principal = Depends(get_access), db: AsyncSession = Depends(get_db, scope="function")):
    access = principal.access
    stmt = select(m.Credential).order_by(func.lower(m.Credential.name))
    if folder_id is not None:
        ids = access.cred_folder_subtree(folder_id) if recursive else {folder_id}
        stmt = stmt.where(m.Credential.folder_id.in_(ids))
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(func.lower(m.Credential.name).like(like), func.lower(m.Credential.username).like(like),
                              func.lower(m.Credential.description).like(like)))
    rows = [c for c in (await db.execute(stmt)).scalars().all() if access.can("credential", c.id, "view")]
    items = [await credential_out(db, c, access) for c in rows[p.offset : p.offset + p.limit]]
    return page(items, len(rows), p)


@router.post("", response_model=CredentialOut, status_code=201, summary="Create a credential entry")
async def create_credential(body: CredentialCreate, request: Request, principal: Principal = Depends(get_access),
                            db: AsyncSession = Depends(get_db, scope="function")):
    access = principal.access
    access.require(Perm.CREDENTIALS_CREATE)
    await check_folder_target(db, access, body.folder_id)
    if body.parent_id:
        await _get(db, access, body.parent_id, "connect")
    idem = await idempotency(db, request, principal, body.model_dump(exclude=set(SECRET_FIELDS)) | {
        f: bool(getattr(body, f)) for f in SECRET_FIELDS})
    if idem.replay:
        return idem.replay
    cred = m.Credential(id=uuid.uuid4(), name=body.name, parent_id=body.parent_id, username=body.username,
                        domain=body.domain, description=body.description, meta=body.metadata or {},
                        owner_id=principal.id, rotation_interval_days=body.rotation_interval_days,
                        folder_id=body.folder_id)
    set_attributes(cred, body.attributes or {})
    set_secret_attributes(cred, {k: v for k, v in (body.secret_attributes or {}).items() if v})
    for f in SECRET_FIELDS:
        _set_secret(cred, f, getattr(body, f))
    if body.private_key and _key_fingerprint(body.private_key, body.passphrase) is None:
        raise ValidationFailed("private key could not be parsed (wrong passphrase or unsupported format)",
                               code="private_key_invalid")
    if any(getattr(body, f) for f in SECRET_FIELDS) or body.secret_attributes:
        cred.secret_changed_at = now()
    _refresh_key_fingerprint(cred)
    db.add(cred)
    await db.flush()
    await db.refresh(cred)
    access.invalidate()
    await access.load(db)
    await audit.record(db, "credential.create", actor_id=principal.id, actor_name=principal.name,
                       resource_type="credential", resource_id=cred.id,
                       details={"name": cred.name, "fields": [f for f in SECRET_FIELDS if getattr(body, f)]})
    out = await credential_out(db, cred, access)
    await idem.store(201, out.model_dump(mode="json"))
    return out


@router.get("/{credential_id}", response_model=CredentialOut, summary="Credential metadata (no secrets)")
async def get_credential(credential_id: uuid.UUID, principal: Principal = Depends(get_access),
                         db: AsyncSession = Depends(get_db, scope="function")):
    return await credential_out(db, await _get(db, principal.access, credential_id, "view"), principal.access)


@router.patch("/{credential_id}", response_model=CredentialOut, summary="Update a credential entry")
async def update_credential(credential_id: uuid.UUID, body: CredentialUpdate,
                            principal: Principal = Depends(get_access), db: AsyncSession = Depends(get_db, scope="function")):
    access = principal.access
    cred = await _get(db, access, credential_id, "edit")
    check_version(cred.version, body.version)
    fields = body.model_fields_set - {"version"}
    if "parent_id" in fields and body.parent_id != cred.parent_id:
        if body.parent_id is not None:
            await _get(db, access, body.parent_id, "connect")
            if await would_create_cycle(db, m.Credential, cred.id, body.parent_id):
                raise ValidationFailed("credential inheritance would create a cycle")
        cred.parent_id = body.parent_id
    if "folder_id" in fields and body.folder_id != cred.folder_id:
        await check_folder_target(db, access, body.folder_id)
        cred.folder_id = body.folder_id
    if "attributes" in fields:
        set_attributes(cred, body.attributes or {})
    if "secret_attributes" in fields and body.secret_attributes:
        current = get_secret_attributes(cred)
        for k, v in body.secret_attributes.items():
            if v:
                current[k] = v
            else:
                current.pop(k, None)
        set_secret_attributes(cred, current)
        cred.secret_changed_at = now()
    if "name" in fields and body.name:
        cred.name = body.name
    for f in ("username", "domain", "description", "rotation_interval_days"):
        if f in fields:
            setattr(cred, f, getattr(body, f))
    if "metadata" in fields:
        fp = (cred.meta or {}).get("_key_fingerprint")
        cred.meta = {**(body.metadata or {}), **({"_key_fingerprint": fp} if fp else {})}
    changed = [f for f in SECRET_FIELDS if f in fields]
    for f in changed:
        _set_secret(cred, f, getattr(body, f))
    if changed:
        cred.secret_changed_at = now()
        _refresh_key_fingerprint(cred)
        if cred.private_key_enc and "_key_fingerprint" not in (cred.meta or {}):
            raise ValidationFailed("private key could not be parsed (wrong passphrase or unsupported format)",
                                   code="private_key_invalid")
    managed = set((await db.execute(select(m.SecretMapping.field).where(
        m.SecretMapping.credential_id == cred.id))).scalars())
    touched_managed = sorted(managed & fields)
    cred.version += 1
    await db.flush()
    await db.refresh(cred)
    if "folder_id" in fields:
        access.invalidate()
        await access.load(db)
    await audit.record(db, "credential.update", actor_id=principal.id, actor_name=principal.name,
                       resource_type="credential", resource_id=cred.id,
                       details={"fields": sorted(fields), "secrets_changed": changed,
                                "externally_managed_fields_changed": touched_managed})
    return await credential_out(db, cred, access)


@router.delete("/{credential_id}", status_code=204, summary="Delete a credential entry")
async def delete_credential(credential_id: uuid.UUID, principal: Principal = Depends(get_access),
                            db: AsyncSession = Depends(get_db, scope="function")) -> None:
    cred = await _get(db, principal.access, credential_id, "manage")
    used = (await db.execute(select(func.count()).select_from(m.Connection)
                             .where(m.Connection.credential_id == cred.id))).scalar_one()
    children = (await db.execute(select(func.count()).select_from(m.Credential)
                                 .where(m.Credential.parent_id == cred.id))).scalar_one()
    if used or children:
        raise Conflict(f"credential is referenced by {used} connection(s) and {children} credential(s)",
                       code="in_use")
    await db.delete(cred)
    await audit.record(db, "credential.delete", actor_id=principal.id, actor_name=principal.name,
                       resource_type="credential", resource_id=credential_id, details={"name": cred.name})


@router.post("/{credential_id}/reveal", response_model=CredentialSecrets,
             summary="Reveal secret values (explicitly authorised and audited)")
async def reveal_credential(credential_id: uuid.UUID, body: CredentialReveal,
                            principal: Principal = Depends(get_access), db: AsyncSession = Depends(get_db, scope="function")):
    access = principal.access
    if principal.api_token is not None and principal.api_token.read_only:
        raise Forbidden("read-only tokens cannot reveal secrets")
    try:
        access.require(Perm.CREDENTIALS_REVEAL)
        cred = await _get(db, access, credential_id, "manage")
    except (Forbidden, NotFound):
        await audit.record_detached("credential.reveal", actor_id=principal.id, actor_name=principal.name,
                                    resource_type="credential", resource_id=credential_id, outcome="denied")
        raise
    box = get_box()
    values = {
        f: (box.decrypt(getattr(cred, f"{f}_enc"), aad_for("credentials", f, cred.id)) if f in body.fields else None)
        for f in SECRET_FIELDS
    }
    secret_attrs = get_secret_attributes(cred, box) if "secret_attributes" in body.fields else None
    await audit.record(db, "credential.reveal", actor_id=principal.id, actor_name=principal.name,
                       resource_type="credential", resource_id=cred.id,
                       details={"fields": body.fields, "reason": body.reason})
    return CredentialSecrets(username=cred.username, domain=cred.domain, secret_attributes=secret_attrs, **values)


@router.get("/{credential_id}/usage", summary="Connections and credentials that reference this credential")
async def credential_usage(credential_id: uuid.UUID, principal: Principal = Depends(get_access),
                           db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    access = principal.access
    await _get(db, access, credential_id, "view")
    conns = (await db.execute(select(m.Connection.id, m.Connection.name, m.Connection.protocol)
                              .where(m.Connection.credential_id == credential_id))).all()
    children = (await db.execute(select(m.Credential.id, m.Credential.name)
                                 .where(m.Credential.parent_id == credential_id))).all()
    return {
        "connections": [{"id": str(c.id), "name": c.name, "protocol": c.protocol}
                        for c in conns if access.can("connection", c.id, "view")],
        "hidden_connections": sum(1 for c in conns if not access.can("connection", c.id, "view")),
        "child_credentials": [{"id": str(c.id), "name": c.name} for c in children
                              if access.can("credential", c.id, "view")],
    }
