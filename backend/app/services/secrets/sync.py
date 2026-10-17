"""Synchronisation of external secrets into credential entries.

Direction: external provider → local credential field (one-way).

Conflict rules (never overwrite silently):
* For each mapping we remember the fingerprint of the remote value and of the
  local value at the last successful sync.
* Remote changed, local unchanged → the remote value is applied.
* Local changed (edited in this application since the last sync), or this is
  the first sync and a *different* local value already exists → **conflict**.
  - ``manual`` (default): nothing is overwritten. The mapping goes to
    ``conflict``, and the new remote value is held encrypted until an
    administrator chooses ``accept_remote`` or ``keep_local``.
  - ``prefer_remote``: the remote value wins (logged as a conflict override).
  - ``prefer_local``: the local value is kept. The remote fingerprint is
    recorded so the same remote value does not raise the conflict again.
* Remote deleted → mapping status ``remote_missing``. With
  ``delete_policy=clear`` the local field is cleared only if it was not edited
  locally since the last sync. Otherwise it is kept.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.config import Settings
from app.errors import ValidationFailed
from app.logging import get_logger
from app.security.crypto import aad_for, fingerprint, get_box
from app.services import audit
from app.services.auth import now
from app.services.credential_attributes import (
    get_attributes,
    get_secret_attributes,
    set_attributes,
    set_secret_attributes,
)
from app.services.secrets.base import ProviderError, SecretProviderClient, SecretRef
from app.services.secrets.discovery import discover
from app.services.secrets.openbao import OpenBaoClient

log = get_logger(__name__)

PROVIDERS: dict[str, type[SecretProviderClient]] = {"openbao": OpenBaoClient}
ADVISORY_LOCK_BASE = 0x5EC0_0000


def provider_secrets(p: m.SecretProvider) -> dict[str, Any]:
    import json

    raw = get_box().decrypt(p.secret_config_enc, aad_for("secret_providers", "secret_config", p.id))
    return json.loads(raw) if raw else {}


def set_provider_secrets(p: m.SecretProvider, data: dict[str, Any]) -> None:
    import json

    clean = {k: v for k, v in data.items() if v not in (None, "")}
    p.secret_config_enc = get_box().encrypt(json.dumps(clean), aad_for("secret_providers", "secret_config", p.id)) \
        if clean else None


def build_client(p: m.SecretProvider, settings: Settings) -> SecretProviderClient:
    cls = PROVIDERS.get(p.type)
    if cls is None:
        raise ValidationFailed(f"unknown provider type {p.type!r}")
    try:
        return cls(p.config or {}, provider_secrets(p), settings)
    except ValueError as exc:  # pydantic validation
        raise ValidationFailed(f"invalid provider configuration: {exc}") from exc


def _attr_target(field: str) -> tuple[str, str] | None:
    """("attributes" | "secret_attributes", name) for attribute mappings."""
    if field.startswith(m.ATTRIBUTE_FIELD_PREFIX):
        return "attributes", field[len(m.ATTRIBUTE_FIELD_PREFIX):]
    if field.startswith(m.SECRET_ATTRIBUTE_FIELD_PREFIX):
        return "secret_attributes", field[len(m.SECRET_ATTRIBUTE_FIELD_PREFIX):]
    return None


def _read_field(cred: m.Credential, field: str) -> str | None:
    target = _attr_target(field)
    if target:
        values = get_attributes(cred) if target[0] == "attributes" else get_secret_attributes(cred)
        v = values.get(target[1])
        return None if v in (None, "") else str(v)
    if field in ("username", "domain"):
        return getattr(cred, field)
    blob = getattr(cred, f"{field}_enc")
    return get_box().decrypt(blob, aad_for("credentials", field, cred.id)) if blob else None


def _write_field(cred: m.Credential, field: str, value: str | None) -> None:
    target = _attr_target(field)
    if target:
        column, name = target
        values = get_attributes(cred) if column == "attributes" else get_secret_attributes(cred)
        if value in (None, ""):
            values.pop(name, None)
        else:
            values[name] = value
        if column == "attributes":
            set_attributes(cred, values)
        else:
            set_secret_attributes(cred, values)
            cred.secret_changed_at = now()
        cred.version += 1
        return
    if field in ("username", "domain"):
        setattr(cred, field, value)
    else:
        setattr(cred, f"{field}_enc",
                get_box().encrypt(value, aad_for("credentials", field, cred.id)) if value else None)
        cred.secret_changed_at = now()
    cred.version += 1


def _hash(v: str | None) -> str | None:
    return fingerprint(v) if v else None


async def apply_remote(db: AsyncSession, mp: m.SecretMapping, cred: m.Credential, value: str | None) -> None:
    _write_field(cred, mp.field, value)
    mp.remote_hash = mp.local_hash = _hash(value)
    mp.pending_remote_enc = None
    mp.status = "ok"
    mp.message = None
    mp.last_synced_at = now()


async def sync_mapping(db: AsyncSession, client: SecretProviderClient, mp: m.SecretMapping,
                       stats: dict[str, int]) -> None:
    cred = await db.get(m.Credential, mp.credential_id)
    if cred is None:
        return
    try:
        ref = SecretRef.model_validate(mp.external_ref)
        value = await client.get(ref)
    except (ProviderError, ValueError) as exc:
        mp.status, mp.message = "error", str(exc)[:500]
        stats["errors"] += 1
        return
    current = _read_field(cred, mp.field)
    cur_h = _hash(current)
    locally_edited = mp.last_synced_at is not None and cur_h != mp.local_hash

    if value is None:
        mp.status = "remote_missing"
        if mp.delete_policy == "clear" and not locally_edited and current is not None:
            _write_field(cred, mp.field, None)
            mp.local_hash = None
            mp.message = "remote secret deleted; local value cleared"
            stats["cleared"] += 1
        else:
            mp.message = "remote secret not found; local value kept"
        stats["missing"] += 1
        return

    rem_h = _hash(value)
    if rem_h == cur_h:
        mp.remote_hash = mp.local_hash = rem_h
        mp.status, mp.message, mp.pending_remote_enc = "ok", None, None
        mp.last_synced_at = now()
        stats["unchanged"] += 1
        return
    if rem_h == mp.remote_hash and not locally_edited:
        stats["unchanged"] += 1
        return
    if rem_h == mp.remote_hash and mp.status == "ok":
        # remote unchanged, local edited and previously accepted (prefer_local) — nothing to do
        stats["unchanged"] += 1
        return

    first_sync_conflict = mp.last_synced_at is None and current is not None
    if not locally_edited and not first_sync_conflict:
        await apply_remote(db, mp, cred, value)
        stats["updated"] += 1
        return

    # conflict
    stats["conflicts"] += 1
    if mp.conflict_policy == "prefer_remote":
        await apply_remote(db, mp, cred, value)
        mp.message = "conflict resolved automatically: remote value applied (prefer_remote)"
    elif mp.conflict_policy == "prefer_local":
        mp.remote_hash = rem_h
        mp.local_hash = cur_h
        mp.status = "ok"
        mp.message = "conflict resolved automatically: local value kept (prefer_local)"
        mp.last_synced_at = now()
    else:
        mp.status = "conflict"
        mp.message = ("local value differs from the external secret; choose 'accept_remote' or 'keep_local'")
        mp.pending_remote_enc = get_box().encrypt(value, aad_for("secret_mappings", "pending", mp.id))


async def sync_provider(db: AsyncSession, provider: m.SecretProvider, settings: Settings, *, trigger: str,
                        actor_id: uuid.UUID | None = None, actor_name: str | None = None,
                        credential_id: uuid.UUID | None = None) -> m.SyncRun:
    run = m.SyncRun(provider_id=provider.id, trigger=trigger, status="running", stats={})
    db.add(run)
    await db.flush()
    stats = {"mappings": 0, "updated": 0, "unchanged": 0, "conflicts": 0, "errors": 0, "missing": 0, "cleared": 0}
    got_lock = (await db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"),
                                 {"k": ADVISORY_LOCK_BASE + (provider.id.int % 0xFFFF)})).scalar_one()
    if not got_lock:
        run.status, run.error, run.finished_at = "skipped", "another sync is running", now()
        return run
    stmt = select(m.SecretMapping).where(m.SecretMapping.provider_id == provider.id)
    if credential_id:
        stmt = stmt.where(m.SecretMapping.credential_id == credential_id)
    try:
        async with build_client(provider, settings) as client:
            # automatic import: every secret becomes a credential (full syncs only)
            if credential_id is None and (provider.config or {}).get("auto_import"):
                found = await discover(db, client, provider, actor_id=actor_id)
                stats.update(found)
                if found["created"] or found["linked"] or found["keys_added"] or found.get("deleted_gone"):
                    await audit.record(db, "secret_provider.auto_import", actor_id=actor_id, actor_name=actor_name,
                                       resource_type="secret_provider", resource_id=provider.id, details=found)
            mappings = (await db.execute(stmt)).scalars().all()
            stats["mappings"] = len(mappings)
            for mp in mappings:
                before = mp.status
                counters = {k: stats[k] for k in ("updated", "conflicts", "cleared", "errors")}
                await sync_mapping(db, client, mp, stats)
                if mp.status != before or any(stats[k] != v for k, v in counters.items()):
                    await audit.record(db, "secret_sync.mapping", actor_id=actor_id, actor_name=actor_name,
                                       resource_type="credential", resource_id=mp.credential_id,
                                       outcome="failure" if mp.status in ("error", "conflict") else "success",
                                       details={"field": mp.field, "provider": provider.name, "status": mp.status,
                                                "message": mp.message})
        status = "ok" if not (stats["errors"] or stats["conflicts"]) else "partial"
        run.error = None
    except (ProviderError, ValidationFailed) as exc:
        status = "error"
        run.error = str(exc)[:1000]
    provider.last_sync_at = now()
    provider.last_sync_status = status
    provider.last_sync_error = run.error
    run.status, run.stats, run.finished_at = status, stats, now()
    await audit.record(db, "secret_sync.run", actor_id=actor_id, actor_name=actor_name,
                       resource_type="secret_provider", resource_id=provider.id,
                       outcome="success" if status != "error" else "failure",
                       details={"trigger": trigger, "status": status, **stats, "error": run.error})
    log.info("secret_sync", provider=provider.name, trigger=trigger, status=status, **stats)
    return run


async def resolve_conflict(db: AsyncSession, mp: m.SecretMapping, action: str) -> None:
    cred = await db.get(m.Credential, mp.credential_id)
    if cred is None:
        raise ValidationFailed("credential no longer exists")
    if mp.status != "conflict":
        raise ValidationFailed("mapping is not in conflict")
    if action == "accept_remote":
        value = get_box().decrypt(mp.pending_remote_enc, aad_for("secret_mappings", "pending", mp.id))
        await apply_remote(db, mp, cred, value)
        mp.message = "conflict resolved: remote value accepted"
    elif action == "keep_local":
        pending = get_box().decrypt(mp.pending_remote_enc, aad_for("secret_mappings", "pending", mp.id))
        mp.remote_hash = _hash(pending)
        mp.local_hash = _hash(_read_field(cred, mp.field))
        mp.pending_remote_enc = None
        mp.status = "ok"
        mp.message = "conflict resolved: local value kept"
        mp.last_synced_at = now()
    else:
        raise ValidationFailed("action must be 'accept_remote' or 'keep_local'")


async def ensure_env_providers(db: AsyncSession, settings: Settings) -> None:
    """Create/update providers configured through environment variables (read-only in the UI)."""
    specs: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
    if settings.OPENBAO_ENABLED and settings.OPENBAO_URL:
        specs.append(("OpenBao (environment)", "openbao",
                      {"url": settings.OPENBAO_URL, "namespace": settings.OPENBAO_NAMESPACE,
                       "mount": settings.OPENBAO_KV_MOUNT, "auth_method": settings.OPENBAO_AUTH_METHOD,
                       "auto_import": settings.OPENBAO_AUTO_IMPORT or None,
                       "import_path": settings.OPENBAO_IMPORT_PATH or None,
                       "exclude": [p.strip() for p in settings.OPENBAO_IMPORT_EXCLUDE.split(",") if p.strip()]
                       or None},
                      {"token": settings.OPENBAO_TOKEN, "role_id": settings.OPENBAO_ROLE_ID,
                       "secret_id": settings.OPENBAO_SECRET_ID}))
    for name, ptype, config, secrets in specs:
        row = (await db.execute(select(m.SecretProvider).where(m.SecretProvider.name == name))).scalar_one_or_none()
        if row is None:
            row = m.SecretProvider(id=uuid.uuid4(), name=name, type=ptype, managed_by_env=True,
                                   sync_interval_seconds=settings.SECRET_SYNC_DEFAULT_INTERVAL_SECONDS)
            db.add(row)
        row.config = {k: v for k, v in config.items() if v is not None}
        set_provider_secrets(row, secrets)
    await db.flush()
