"""Import workflow (Devolutions Remote Desktop Manager): upload → preview → selective apply → report."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.api.deps import Principal, get_access, require, settings_dep
from app.config import Settings
from app.db import get_db
from app.errors import Conflict, Forbidden, NotFound, ValidationFailed
from app.schemas.protocol_settings import validate_settings
from app.services import audit
from app.services.auth import now
from app.services.authz import Perm
from app.services.importers.rdm import parse
from app.services.resolution import default_port

router = APIRouter(prefix="/imports", tags=["import"])

MANUAL_MIGRATION_HELP = (
    "Remote Desktop Manager encrypts stored passwords with keys that are not part of the export. "
    "They are never decrypted by this importer. To migrate them: (1) create credential entries in the "
    "credential store (or map them to Infisical/OpenBao secrets), (2) open each listed connection and set its "
    "authentication to 'Credential store' with that entry, or (3) use 'Interactive' authentication so users "
    "enter the password when connecting."
)


class ApplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected: list[str] | None = Field(None, description="source IDs to import; null = everything importable")
    target_folder_id: uuid.UUID | None = None
    on_duplicate: Literal["skip", "create"] = "skip"


def _job_out(job: m.ImportJob, include_items: bool = True) -> dict[str, Any]:
    preview = dict(job.preview or {})
    if not include_items:
        preview.pop("items", None)
    return {"id": str(job.id), "status": job.status, "source_format": job.source_format, "filename": job.filename,
            "created_at": job.created_at, "applied_at": job.applied_at, "preview": preview, "result": job.result,
            "manual_migration_help": MANUAL_MIGRATION_HELP}


async def _detect_duplicates(db: AsyncSession, preview: dict[str, Any]) -> None:
    rows = (await db.execute(select(m.Connection.id, m.Connection.name, m.Connection.protocol, m.Connection.hostname,
                                    m.Connection.port, m.Connection.import_source))).all()
    by_source = {(r.import_source or {}).get("source_id"): r for r in rows if r.import_source}
    by_endpoint: dict[tuple[str, str, int | None], Any] = {}
    for r in rows:
        if r.hostname:
            by_endpoint[(r.protocol, r.hostname.lower(), r.port or default_port(r.protocol))] = r
    for item in preview["items"]:
        if item["kind"] != "connection":
            continue
        prev = by_source.get(item["source_id"])
        if prev is not None:
            item["duplicate_of"], item["duplicate_reason"] = str(prev.id), "imported before (same RDM entry ID)"
            continue
        if item.get("host") and item.get("protocol"):
            key = (item["protocol"], item["host"].lower(), item.get("port") or default_port(item["protocol"]))
            hit = by_endpoint.get(key)
            if hit is not None:
                item["duplicate_of"], item["duplicate_reason"] = str(hit.id), f"same endpoint as '{hit.name}'"
    preview["summary"]["duplicates"] = sum(1 for i in preview["items"] if i.get("duplicate_of"))


@router.post("/rdm", status_code=201, summary="Upload an RDM export (.rdm/.xml or .csv) and get a preview")
async def preview_rdm(file: UploadFile = File(...), principal: Principal = Depends(require(Perm.IMPORT_RUN)),
                      db: AsyncSession = Depends(get_db, scope="function"), settings: Settings = Depends(settings_dep)) -> dict:
    if not settings.FEATURE_RDM_IMPORT:
        raise Forbidden("RDM import is disabled")
    limit = settings.IMPORT_MAX_FILE_MB * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise ValidationFailed(f"file exceeds {settings.IMPORT_MAX_FILE_MB} MB")
    parsed = parse(file.filename, data)
    preview = parsed.as_dict()
    await _detect_duplicates(db, preview)
    job = m.ImportJob(user_id=principal.id, source_format=parsed.format, filename=(file.filename or "")[:255],
                      status="previewed" if parsed.items else "failed", preview=preview)
    db.add(job)
    await db.flush()
    await db.refresh(job)
    await audit.record(db, "import.preview", actor_id=principal.id, actor_name=principal.name,
                       resource_type="import", resource_id=job.id,
                       details={"filename": file.filename, "summary": preview["summary"]})
    return _job_out(job)


@router.get("", summary="Import history")
async def list_imports(principal: Principal = Depends(require(Perm.IMPORT_RUN)),
                       db: AsyncSession = Depends(get_db, scope="function")) -> list[dict]:
    stmt = select(m.ImportJob).order_by(m.ImportJob.created_at.desc()).limit(100)
    if not principal.access.is_admin:
        stmt = stmt.where(m.ImportJob.user_id == principal.id)
    return [_job_out(j, include_items=False) for j in (await db.execute(stmt)).scalars()]


@router.get("/{job_id}", summary="Import preview / report")
async def get_import(job_id: uuid.UUID, principal: Principal = Depends(require(Perm.IMPORT_RUN)),
                     db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    job = await db.get(m.ImportJob, job_id)
    if job is None or (job.user_id != principal.id and not principal.access.is_admin):
        raise NotFound("import not found")
    return _job_out(job)


async def _ensure_folder(db: AsyncSession, cache: dict[tuple[uuid.UUID | None, str], uuid.UUID],
                         parent: uuid.UUID | None, name: str, owner: uuid.UUID, created: list[str]) -> uuid.UUID:
    key = (parent, name.lower())
    if key in cache:
        return cache[key]
    existing = (await db.execute(select(m.Folder.id).where(
        m.Folder.parent_id.is_(None) if parent is None else m.Folder.parent_id == parent,
        func.lower(m.Folder.name) == name.lower()))).scalar_one_or_none()
    if existing is None:
        folder = m.Folder(name=name[:255], parent_id=parent, owner_id=owner)
        db.add(folder)
        await db.flush()
        existing = folder.id
        created.append(str(folder.id))
    cache[key] = existing
    return existing


@router.post("/{job_id}/apply", summary="Import selected items and produce a report")
async def apply_import(job_id: uuid.UUID, body: ApplyIn, principal: Principal = Depends(get_access),
                       db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    access = principal.access
    access.require(Perm.IMPORT_RUN)
    job = await db.get(m.ImportJob, job_id)
    if job is None or (job.user_id != principal.id and not access.is_admin):
        raise NotFound("import not found")
    if job.status != "previewed":
        raise Conflict(f"import is {job.status}", code="import_not_pending")
    if body.target_folder_id:
        if await db.get(m.Folder, body.target_folder_id) is None:
            raise ValidationFailed("target folder not found")
    if not access.can_create_in_folder(body.target_folder_id, Perm.CONNECTIONS_CREATE):
        raise Forbidden("not allowed to create connections in the target folder")

    items: list[dict[str, Any]] = job.preview.get("items", [])
    selected = set(body.selected) if body.selected is not None else None
    report: dict[str, Any] = {"created": {"folders": 0, "connections": 0, "credentials": 0},
                              "skipped": [], "errors": [], "manual_migration": [], "unsupported_settings": [],
                              "warnings": []}
    folder_cache: dict[tuple[uuid.UUID | None, str], uuid.UUID] = {}
    created_folders: list[str] = []
    source_to_conn: dict[str, uuid.UUID] = {}
    source_to_cred: dict[str, uuid.UUID] = {}

    def is_selected(it: dict[str, Any]) -> bool:
        return selected is None or it["source_id"] in selected

    async def folder_for(path: list[str]) -> uuid.UUID | None:
        current = body.target_folder_id
        for seg in path:
            current = await _ensure_folder(db, folder_cache, current, seg, principal.id, created_folders)
        return current

    for it in items:
        if not is_selected(it):
            continue
        if it["kind"] == "unsupported":
            report["skipped"].append({"source_id": it["source_id"], "name": it["name"],
                                      "reason": f"unsupported entry type '{it['entry_type']}'"})
        elif it["kind"] == "folder":
            await folder_for(it["folder_path"])
        elif it["kind"] == "credential":
            cred = m.Credential(id=uuid.uuid4(), name=it["name"][:255], username=it.get("username"),
                                domain=it.get("domain"), description=it.get("description"), owner_id=principal.id,
                                meta={"imported_from": "rdm", "source_id": it["source_id"]})
            db.add(cred)
            source_to_cred[it["source_id"]] = cred.id
            report["created"]["credentials"] += 1
            if it.get("credential_status") == "encrypted_not_migrated":
                report["manual_migration"].append({"type": "credential", "id": str(cred.id), "name": it["name"],
                                                   "reason": "password not migrated (encrypted in export)"})
    await db.flush()

    pending = [it for it in items if it["kind"] == "connection" and is_selected(it)]
    by_source = {it["source_id"]: it for it in pending}
    done: set[str] = set()

    async def create(it: dict[str, Any]) -> None:
        if it["source_id"] in done:
            return
        done.add(it["source_id"])
        if it.get("duplicate_of") and body.on_duplicate == "skip":
            report["skipped"].append({"source_id": it["source_id"], "name": it["name"],
                                      "reason": f"duplicate: {it.get('duplicate_reason')}",
                                      "existing_id": it["duplicate_of"]})
            return
        parent_id = None
        psrc = it.get("parent_source_id")
        if psrc:
            if psrc in by_source:
                await create(by_source[psrc])
            parent_id = source_to_conn.get(psrc)
        folder_id = None if parent_id else await folder_for(it["folder_path"])
        protocol = it["protocol"]
        try:
            settings_data = validate_settings(protocol, it.get("settings") or {})
        except ValidationFailed as exc:
            settings_data = validate_settings(protocol, {})
            report["warnings"].append({"source_id": it["source_id"], "name": it["name"],
                                       "message": f"protocol settings dropped: {exc.detail}",
                                       "settings": it.get("settings")})
        cred_ref = source_to_cred.get(it.get("credential_source_id") or "")
        if cred_ref:
            auth_mode = "credential"
        elif it.get("username"):
            auth_mode = "direct"
        else:
            auth_mode = "inherit"
        conn = m.Connection(
            id=uuid.uuid4(), name=it["name"][:255], protocol=protocol, hostname=(it.get("host") or None),
            port=it.get("port"), folder_id=folder_id, parent_id=parent_id, auth_mode=auth_mode,
            username=it.get("username") if auth_mode == "direct" else None,
            domain=it.get("domain") if auth_mode == "direct" else None, credential_id=cred_ref,
            description=it.get("description"), tags=it.get("tags") or [], settings=settings_data,
            owner_id=principal.id,
            import_source={"format": job.source_format, "source_id": it["source_id"], "job_id": str(job.id),
                           "entry_type": it["entry_type"]},
        )
        db.add(conn)
        await db.flush()
        source_to_conn[it["source_id"]] = conn.id
        report["created"]["connections"] += 1
        if it.get("credential_status") == "encrypted_not_migrated":
            report["manual_migration"].append({"type": "connection", "id": str(conn.id), "name": it["name"],
                                               "reason": "password not migrated (encrypted in export)"})
        elif it.get("credential_source_id") and not cred_ref:
            report["manual_migration"].append({"type": "connection", "id": str(conn.id), "name": it["name"],
                                               "reason": "referenced RDM credential entry was not part of this import"})
        if it.get("unsupported"):
            report["unsupported_settings"].append({"connection_id": str(conn.id), "name": it["name"],
                                                   "settings": it["unsupported"]})
        for w in it.get("warnings") or []:
            report["warnings"].append({"source_id": it["source_id"], "name": it["name"], "message": w})
        if not it.get("host") and not parent_id:
            report["warnings"].append({"source_id": it["source_id"], "name": it["name"],
                                       "message": "no hostname in export; set one before connecting"})

    for it in pending:
        try:
            async with db.begin_nested():
                await create(it)
        except Exception as exc:  # noqa: BLE001 - report and continue with the next item
            report["errors"].append({"source_id": it["source_id"], "name": it["name"], "error": str(exc)[:300]})

    report["created"]["folders"] = len(created_folders)
    job.status = "applied"
    job.applied_at = now()
    job.result = report
    await audit.record(db, "import.apply", actor_id=principal.id, actor_name=principal.name, resource_type="import",
                       resource_id=job.id, details={"created": report["created"], "skipped": len(report["skipped"]),
                                                    "errors": len(report["errors"]),
                                                    "manual_migration": len(report["manual_migration"])})
    await db.flush()
    return _job_out(job, include_items=False)
