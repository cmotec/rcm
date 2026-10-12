"""External secret providers (Infisical, OpenBao) and credential mappings."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.api.deps import Principal, get_access, require, settings_dep
from app.api.v1.credentials import _get as get_credential_checked
from app.api.v1.credentials import local_hash
from app.config import Settings
from app.db import get_db
from app.errors import Forbidden, NotFound, UpstreamError, ValidationFailed
from app.services import audit
from app.services.authz import Perm
from app.services.secrets.base import ProviderError, SecretRef
from app.services.secrets.sync import (
    PROVIDERS,
    build_client,
    provider_secrets,
    resolve_conflict,
    set_provider_secrets,
    sync_provider,
)

router = APIRouter(tags=["secret providers"])


class ProviderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    type: Literal["infisical", "openbao"]
    config: dict[str, Any]
    secrets: dict[str, Any] = Field(default_factory=dict, description="write-only (client secret, token, …)")
    enabled: bool = True
    sync_interval_seconds: int = Field(900, ge=60, le=7 * 86400)


class ProviderPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=1, max_length=255)
    config: dict[str, Any] | None = None
    secrets: dict[str, Any] | None = Field(None, description="merged into the stored secrets; null values remove")
    enabled: bool | None = None
    sync_interval_seconds: int | None = Field(None, ge=60, le=7 * 86400)


class ProviderOut(BaseModel):
    id: uuid.UUID
    name: str
    type: str
    config: dict[str, Any]
    secret_fields_set: list[str]
    enabled: bool
    managed_by_env: bool
    sync_interval_seconds: int
    last_sync_at: datetime | None
    last_sync_status: str
    last_sync_error: str | None
    mapping_count: int = 0
    conflict_count: int = 0


class MappingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: Literal["username", "password", "domain", "private_key", "passphrase"]
    provider_id: uuid.UUID
    external_ref: SecretRef
    conflict_policy: Literal["manual", "prefer_remote", "prefer_local"] = "manual"
    delete_policy: Literal["keep", "clear"] = "keep"


class MappingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    credential_id: uuid.UUID
    provider_id: uuid.UUID
    field: str
    external_ref: dict[str, Any]
    conflict_policy: str
    delete_policy: str
    status: str
    message: str | None
    last_synced_at: datetime | None


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["accept_remote", "keep_local"]


class ImportItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    username: str | None = Field(None, max_length=255, description="literal username (optional)")
    fields: dict[Literal["username", "password", "domain", "private_key", "passphrase"], SecretRef]


class ImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ImportItem] = Field(min_length=1, max_length=500)
    conflict_policy: Literal["manual", "prefer_remote", "prefer_local"] = "manual"


async def _provider_out(db: AsyncSession, p: m.SecretProvider) -> ProviderOut:
    mappings = (await db.execute(select(m.SecretMapping.status).where(m.SecretMapping.provider_id == p.id))).scalars().all()
    return ProviderOut(
        id=p.id, name=p.name, type=p.type, config=p.config or {},
        secret_fields_set=sorted(provider_secrets(p).keys()), enabled=p.enabled, managed_by_env=p.managed_by_env,
        sync_interval_seconds=p.sync_interval_seconds, last_sync_at=p.last_sync_at,
        last_sync_status=p.last_sync_status, last_sync_error=p.last_sync_error,
        mapping_count=len(mappings), conflict_count=sum(1 for s in mappings if s == "conflict"),
    )


async def _get_provider(db: AsyncSession, provider_id: uuid.UUID) -> m.SecretProvider:
    p = await db.get(m.SecretProvider, provider_id)
    if p is None:
        raise NotFound("secret provider not found")
    return p


def _check_enabled(settings: Settings, ptype: str) -> None:
    if (ptype == "infisical" and not settings.INFISICAL_ENABLED) or (ptype == "openbao" and not settings.OPENBAO_ENABLED):
        raise ValidationFailed(f"the {ptype} integration is disabled on this server")


def _validate(ptype: str, config: dict[str, Any], secrets: dict[str, Any]) -> None:
    cls = PROVIDERS[ptype]
    try:
        cls.config_model.model_validate(config)
        cls.secret_model.model_validate(secrets)
    except ValueError as exc:
        raise ValidationFailed(f"invalid provider configuration: {exc}") from exc


@router.get("/secret-providers", response_model=list[ProviderOut], summary="List secret providers")
async def list_providers(principal: Principal = Depends(get_access), db: AsyncSession = Depends(get_db, scope="function")):
    if not (principal.access.has(Perm.SECRETS_MANAGE) or principal.access.has(Perm.CREDENTIALS_CREATE)):
        raise Forbidden()
    rows = (await db.execute(select(m.SecretProvider).order_by(m.SecretProvider.name))).scalars().all()
    return [await _provider_out(db, p) for p in rows]


@router.post("/secret-providers", response_model=ProviderOut, status_code=201, summary="Add a secret provider")
async def create_provider(body: ProviderIn, principal: Principal = Depends(require(Perm.SECRETS_MANAGE)),
                          db: AsyncSession = Depends(get_db, scope="function"), settings: Settings = Depends(settings_dep)):
    _check_enabled(settings, body.type)
    _validate(body.type, body.config, body.secrets)
    p = m.SecretProvider(id=uuid.uuid4(), name=body.name, type=body.type, config=body.config, enabled=body.enabled,
                         sync_interval_seconds=body.sync_interval_seconds)
    set_provider_secrets(p, body.secrets)
    db.add(p)
    await db.flush()
    await audit.record(db, "secret_provider.create", actor_id=principal.id, actor_name=principal.name,
                       resource_type="secret_provider", resource_id=p.id,
                       details={"name": p.name, "type": p.type, "config": body.config})
    await db.refresh(p)
    return await _provider_out(db, p)


@router.patch("/secret-providers/{provider_id}", response_model=ProviderOut, summary="Update a secret provider")
async def update_provider(provider_id: uuid.UUID, body: ProviderPatch,
                          principal: Principal = Depends(require(Perm.SECRETS_MANAGE)),
                          db: AsyncSession = Depends(get_db, scope="function")):
    p = await _get_provider(db, provider_id)
    fields = body.model_fields_set
    if p.managed_by_env and fields - {"enabled", "sync_interval_seconds"}:
        raise Forbidden("this provider is configured through environment variables")
    config = body.config if "config" in fields and body.config is not None else p.config
    secrets = provider_secrets(p)
    if "secrets" in fields and body.secrets is not None:
        for k, v in body.secrets.items():
            if v is None:
                secrets.pop(k, None)
            else:
                secrets[k] = v
    _validate(p.type, config, secrets)
    p.config = config
    set_provider_secrets(p, secrets)
    if body.name:
        p.name = body.name
    if body.enabled is not None:
        p.enabled = body.enabled
    if body.sync_interval_seconds:
        p.sync_interval_seconds = body.sync_interval_seconds
    await audit.record(db, "secret_provider.update", actor_id=principal.id, actor_name=principal.name,
                       resource_type="secret_provider", resource_id=p.id,
                       details={"fields": sorted(fields), "secret_keys_changed": sorted((body.secrets or {}).keys())})
    await db.flush()
    await db.refresh(p)
    return await _provider_out(db, p)


@router.delete("/secret-providers/{provider_id}", status_code=204, summary="Remove a provider and its mappings")
async def delete_provider(provider_id: uuid.UUID, principal: Principal = Depends(require(Perm.SECRETS_MANAGE)),
                          db: AsyncSession = Depends(get_db, scope="function")) -> None:
    p = await _get_provider(db, provider_id)
    if p.managed_by_env:
        raise Forbidden("this provider is configured through environment variables")
    await db.delete(p)
    await audit.record(db, "secret_provider.delete", actor_id=principal.id, actor_name=principal.name,
                       resource_type="secret_provider", resource_id=provider_id, details={"name": p.name})


@router.post("/secret-providers/{provider_id}/test", summary="Test provider connectivity and authentication")
async def test_provider(provider_id: uuid.UUID, principal: Principal = Depends(require(Perm.SECRETS_MANAGE)),
                        db: AsyncSession = Depends(get_db, scope="function"), settings: Settings = Depends(settings_dep)) -> dict:
    p = await _get_provider(db, provider_id)
    try:
        async with build_client(p, settings) as client:
            return await client.test()
    except ProviderError as exc:
        raise UpstreamError(str(exc), code="provider_error") from exc


@router.get("/secret-providers/{provider_id}/browse", summary="Browse secret names (never values)")
async def browse_provider(provider_id: uuid.UUID, path: str = Query("/", max_length=1024),
                          principal: Principal = Depends(require(Perm.SECRETS_MANAGE)),
                          db: AsyncSession = Depends(get_db, scope="function"), settings: Settings = Depends(settings_dep)) -> list[dict]:
    p = await _get_provider(db, provider_id)
    try:
        async with build_client(p, settings) as client:
            items = await client.browse(path)
    except ProviderError as exc:
        raise UpstreamError(str(exc), code="provider_error") from exc
    return [i.__dict__ for i in items]


@router.post("/secret-providers/{provider_id}/sync", summary="Synchronise now")
async def sync_now(provider_id: uuid.UUID, principal: Principal = Depends(get_access),
                   db: AsyncSession = Depends(get_db, scope="function"), settings: Settings = Depends(settings_dep),
                   credential_id: uuid.UUID | None = Query(None, description="only this credential")) -> dict:
    if credential_id is None:
        principal.access.require(Perm.SECRETS_MANAGE)
    else:
        await get_credential_checked(db, principal.access, credential_id, "edit")
    p = await _get_provider(db, provider_id)
    if not settings.SECRET_SYNC_ENABLED:
        raise ValidationFailed("secret synchronisation is disabled (SECRET_SYNC_ENABLED=false)")
    run = await sync_provider(db, p, settings, trigger="manual", actor_id=principal.id, actor_name=principal.name,
                              credential_id=credential_id)
    return {"run_id": str(run.id), "status": run.status, "stats": run.stats, "error": run.error}


@router.get("/secret-providers/{provider_id}/runs", summary="Recent synchronisation runs")
async def list_runs(provider_id: uuid.UUID, limit: int = Query(20, ge=1, le=200),
                    principal: Principal = Depends(require(Perm.SECRETS_MANAGE)),
                    db: AsyncSession = Depends(get_db, scope="function")) -> list[dict]:
    rows = (await db.execute(select(m.SyncRun).where(m.SyncRun.provider_id == provider_id)
                             .order_by(m.SyncRun.started_at.desc()).limit(limit))).scalars().all()
    return [{"id": str(r.id), "trigger": r.trigger, "started_at": r.started_at, "finished_at": r.finished_at,
             "status": r.status, "stats": r.stats, "error": r.error} for r in rows]


@router.post("/secret-providers/{provider_id}/import", summary="Create credential entries from external secrets")
async def import_secrets(provider_id: uuid.UUID, body: ImportIn,
                         principal: Principal = Depends(require(Perm.SECRETS_MANAGE)),
                         db: AsyncSession = Depends(get_db, scope="function"), settings: Settings = Depends(settings_dep)) -> dict:
    principal.access.require(Perm.CREDENTIALS_CREATE)
    p = await _get_provider(db, provider_id)
    created: list[str] = []
    for item in body.items:
        cred = m.Credential(id=uuid.uuid4(), name=item.name, username=item.username, owner_id=principal.id,
                            meta={"imported_from": p.name})
        db.add(cred)
        await db.flush()
        for fname, ref in item.fields.items():
            db.add(m.SecretMapping(credential_id=cred.id, provider_id=p.id, field=fname,
                                   external_ref=ref.model_dump(exclude_none=True),
                                   conflict_policy=body.conflict_policy))
        created.append(str(cred.id))
    await db.flush()
    await audit.record(db, "secret_provider.import", actor_id=principal.id, actor_name=principal.name,
                       resource_type="secret_provider", resource_id=p.id, details={"credentials": len(created)})
    run = await sync_provider(db, p, settings, trigger="import", actor_id=principal.id, actor_name=principal.name)
    return {"created": created, "sync": {"status": run.status, "stats": run.stats, "error": run.error}}


# --------------------------------------------------------------------------- mappings per credential


@router.get("/credentials/{credential_id}/mappings", response_model=list[MappingOut],
            summary="External secret mappings of a credential")
async def get_mappings(credential_id: uuid.UUID, principal: Principal = Depends(get_access),
                       db: AsyncSession = Depends(get_db, scope="function")):
    await get_credential_checked(db, principal.access, credential_id, "view")
    return (await db.execute(select(m.SecretMapping).where(m.SecretMapping.credential_id == credential_id)
                             .order_by(m.SecretMapping.field))).scalars().all()


@router.put("/credentials/{credential_id}/mappings", response_model=list[MappingOut],
            summary="Replace the external secret mappings of a credential")
async def put_mappings(credential_id: uuid.UUID, body: list[MappingIn], principal: Principal = Depends(get_access),
                       db: AsyncSession = Depends(get_db, scope="function")):
    cred = await get_credential_checked(db, principal.access, credential_id, "manage")
    if len({b.field for b in body}) != len(body):
        raise ValidationFailed("each field can be mapped only once")
    existing = {mp.field: mp for mp in (await db.execute(
        select(m.SecretMapping).where(m.SecretMapping.credential_id == credential_id))).scalars()}
    for b in body:
        if await db.get(m.SecretProvider, b.provider_id) is None:
            raise ValidationFailed("unknown provider")
    await db.execute(delete(m.SecretMapping).where(m.SecretMapping.credential_id == credential_id,
                                                   m.SecretMapping.field.notin_([b.field for b in body])))
    for b in body:
        ref = b.external_ref.model_dump(exclude_none=True)
        mp = existing.get(b.field)
        if mp is None or mp.provider_id != b.provider_id or mp.external_ref != ref:
            if mp is not None:
                await db.delete(mp)
                await db.flush()
            db.add(m.SecretMapping(credential_id=credential_id, provider_id=b.provider_id, field=b.field,
                                   external_ref=ref, conflict_policy=b.conflict_policy,
                                   delete_policy=b.delete_policy, local_hash=local_hash(cred, b.field)))
        else:
            mp.conflict_policy, mp.delete_policy = b.conflict_policy, b.delete_policy
    await db.flush()
    await audit.record(db, "credential.mappings", actor_id=principal.id, actor_name=principal.name,
                       resource_type="credential", resource_id=credential_id,
                       details={"mappings": [{"field": b.field, "provider": str(b.provider_id), **b.external_ref.model_dump(exclude_none=True)}
                                             for b in body]})
    return await get_mappings(credential_id, principal, db)


@router.post("/secret-mappings/{mapping_id}/resolve", response_model=MappingOut, summary="Resolve a sync conflict")
async def resolve_mapping(mapping_id: uuid.UUID, body: ResolveIn, principal: Principal = Depends(get_access),
                          db: AsyncSession = Depends(get_db, scope="function")):
    mp = await db.get(m.SecretMapping, mapping_id)
    if mp is None:
        raise NotFound("mapping not found")
    await get_credential_checked(db, principal.access, mp.credential_id, "manage")
    await resolve_conflict(db, mp, body.action)
    await audit.record(db, "secret_sync.resolve", actor_id=principal.id, actor_name=principal.name,
                       resource_type="credential", resource_id=mp.credential_id,
                       details={"field": mp.field, "action": body.action})
    await db.flush()
    await db.refresh(mp)
    return mp
