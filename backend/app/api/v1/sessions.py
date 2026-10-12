"""Remote sessions: lifecycle, WebSocket tunnels and file-manager operations."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, WebSocket
from fastapi.responses import StreamingResponse

from app.api.deps import Principal, authenticate, authenticate_websocket, settings_dep
from app.config import Settings, get_settings
from app.db import session_scope
from app.errors import AppError, Conflict, Forbidden, NotFound, TooManyRequests, ValidationFailed
from app.logging import get_logger
from app.schemas.core import (
    ChmodIn,
    DeleteIn,
    DirListing,
    FileEntry,
    MkdirIn,
    RenameIn,
    SessionCreate,
    SessionOut,
)
from app.security.crypto import get_box
from app.security.ratelimit import limiter
from app.services import audit
from app.services.app_settings import get_runtime
from app.services.authz import Perm
from app.services.resolution import AuthOverride
from app.services.sessions import guacd_params
from app.services.sessions.files.base import Transfer, clean_path
from app.services.sessions.files.opener import open_fs
from app.services.sessions.guac_tunnel import parse_client_info, run_guac_tunnel
from app.services.sessions.launch import build_launch, user_keyboard_layout
from app.services.sessions.manager import KIND_BY_PROTOCOL, Session, get_manager
from app.services.sessions.ssh_native import HostKeyDecision, preflight
from app.services.sessions.terminal import run_terminal

log = get_logger(__name__)
router = APIRouter(prefix="/sessions", tags=["sessions"])


class CredentialsRequired(AppError):
    status, code, title = 428, "credentials_required", "Credentials required"


class CredentialChoiceRequired(AppError):
    status, code, title = 428, "credential_choice_required", "Choose a credential"


async def principal_detached(request: Request, settings: Settings = Depends(settings_dep)) -> Principal:
    """Authenticate without holding a DB connection for the request (long-running transfers)."""
    async with session_scope() as db:
        principal = await authenticate(db, settings, request.headers, request.cookies, request.method)
        await principal.access.load(db)
    retry = limiter.hit("api", str(principal.user.id), settings.RATE_LIMIT_API_PER_MINUTE)
    if retry is not None:
        raise TooManyRequests(retry)
    return principal


def _owned(session_id: uuid.UUID, principal: Principal) -> Session:
    return get_manager().get_owned(session_id, principal.id)


def _out(s: Session, ticket: tuple[str, object] | None = None) -> SessionOut:
    return SessionOut.model_validate(s.public(ticket=ticket))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- lifecycle


@router.post("", response_model=SessionOut, status_code=201, summary="Start a remote session",
             responses={409: {"description": "SSH host key needs confirmation (see `host_key` in the body)"},
                        428: {"description": "Interactive credentials required"}})
async def create_session(body: SessionCreate, request: Request, principal: Principal = Depends(principal_detached),
                         settings: Settings = Depends(settings_dep)) -> SessionOut:
    manager = get_manager()
    access = principal.access
    if not access.can("connection", body.connection_id, "view"):
        raise NotFound("connection not found")
    if not access.can("connection", body.connection_id, "connect"):
        await audit.record_detached("session.create", actor_id=principal.id, actor_name=principal.name,
                                    resource_type="connection", resource_id=body.connection_id, outcome="denied")
        raise Forbidden("'connect' permission required")
    manager.check_capacity(principal.id)
    override: AuthOverride | None = None
    if body.auth_override is not None:
        o = body.auth_override
        if o.mode == "credential":
            if not o.credential_id:
                raise ValidationFailed("auth_override mode 'credential' requires credential_id")
            override = AuthOverride(credential_id=o.credential_id)
        else:
            values = o.credentials.model_dump(exclude_none=True) if o.credentials else {}
            if not values:
                raise ValidationFailed("auth_override mode 'own' requires credentials")
            override = AuthOverride(values=values)
    for cred_id in (body.credential_id, override.credential_id if override else None):
        if cred_id is None:
            continue
        # the user may only pick credentials they are allowed to use
        if not access.can("credential", cred_id, "view"):
            raise NotFound("credential not found")
        if not access.can("credential", cred_id, "connect"):
            raise Forbidden("you may not use this credential ('connect' permission required)")
    interactive = body.credentials.model_dump(exclude_none=True) if body.credentials else None
    decision = HostKeyDecision(
        accepted={f"{k.hostname}:{k.port}": k.fingerprint for k in body.accept_host_keys},
        may_replace=access.has(Perm.KNOWN_HOSTS_MANAGE) or access.can("connection", body.connection_id, "edit"),
    )
    req_ip = request.client.host if request.client else None

    async with session_scope() as db:
        runtime = await get_runtime(db, settings)
        launch, eff = await build_launch(db, body.connection_id, settings=settings, runtime=runtime,
                                         box=get_box(), interactive=interactive,
                                         chosen_credential_id=body.credential_id, auth_override=override)
        if eff.credential_choice_required:
            raise CredentialChoiceRequired("choose the credential to connect with")
        if launch.protocol == "rdp" and not launch.settings.server_layout:
            layout = await user_keyboard_layout(db, principal.id)
            if layout:  # the user's profile default; a layout set on the connection wins
                launch.settings = launch.settings.model_copy(update={"server_layout": layout})
        if eff.prompt_required:
            raise CredentialsRequired(
                "this connection asks for credentials when it is opened",
                extra={"fields": ["username", "password"] + (["domain"] if launch.protocol == "rdp" else []),
                       "username": launch.username, "domain": launch.domain},
            )
        kind = KIND_BY_PROTOCOL[launch.protocol]
        if launch.protocol == "ssh" and launch.settings.engine == "guacd":
            if not settings.SSH_GUACD_ENGINE_ENABLED:
                raise ValidationFailed("the guacd SSH engine is disabled on this server")
            kind = "guac"
        if kind == "files" and launch.protocol in ("ftp", "ftps"):
            ftp_open = sum(1 for s in manager.all() if s.protocol in ("ftp", "ftps"))
            if ftp_open >= settings.FTP_MAX_SESSIONS:
                raise TooManyRequests(10, "FTP session capacity reached on this server")
        # SSH host-key checks happen before the session exists, so the user can confirm them.
        if launch.protocol in ("ssh", "sftp"):
            await preflight(db, launch, decision, settings, user_id=principal.id, user_name=principal.name)
        if launch.tunnel_via is not None:
            await preflight(db, launch.tunnel_via, decision, settings, user_id=principal.id, user_name=principal.name)
        await db.commit()

        session = Session(
            id=uuid.uuid4(), owner_id=principal.id, owner_name=principal.name, kind=kind,
            protocol=launch.protocol, connection_id=launch.connection_id, connection_name=launch.connection_name,
            launch=launch, client_ip=req_ip, client_settings=guacd_params.client_settings(launch),
        )
        if kind == "files":
            try:
                session.fs = await open_fs(db, launch, settings, runtime)
                session.state = "connected"
            except AppError as exc:
                launch.wipe()
                await audit.record_detached("session.create", actor_id=principal.id, actor_name=principal.name,
                                            resource_type="connection", resource_id=launch.connection_id,
                                            outcome="failure", details={"error": exc.detail[:300]})
                raise
    await manager.register(session)
    if kind == "files":
        await manager.mark_connected(session)
    ticket = manager.issue_ticket(session) if kind != "files" else None
    await audit.record_detached(
        "session.create", actor_id=principal.id, actor_name=principal.name, resource_type="connection",
        resource_id=launch.connection_id, outcome="success",
        details={"session_id": str(session.id), "protocol": launch.protocol, "host": launch.host,
                 "port": launch.port, "auth_sources": {k: v["kind"] for k, v in launch.auth_sources.items()},
                 "chosen_credential_id": str(body.credential_id) if body.credential_id else None,
                 "auth_override": body.auth_override.mode if body.auth_override else None,
                 "override_credential_id": str(override.credential_id) if override and override.credential_id else None,
                 "warnings": [w["code"] for w in launch.warnings]},
    )
    return _out(session, ticket)


@router.get("", response_model=list[SessionOut], summary="List my sessions (or all, for session managers)")
async def list_sessions(scope: str = Query("mine", pattern="^(mine|all)$"),
                        principal: Principal = Depends(principal_detached)) -> list[SessionOut]:
    manager = get_manager()
    if scope == "all":
        principal.access.require(Perm.SESSIONS_MANAGE)
        sessions = manager.all()
    else:
        sessions = manager.for_user(principal.id)
    return [_out(s) for s in sorted(sessions, key=lambda s: s.created_at)]


@router.get("/{session_id}", response_model=SessionOut, summary="Session status")
async def get_session(session_id: uuid.UUID, principal: Principal = Depends(principal_detached)) -> SessionOut:
    return _out(_owned(session_id, principal))


@router.post("/{session_id}/reconnect", response_model=SessionOut, summary="Get a new tunnel ticket / reopen")
async def reconnect_session(session_id: uuid.UUID, principal: Principal = Depends(principal_detached),
                            settings: Settings = Depends(settings_dep)) -> SessionOut:
    manager = get_manager()
    s = _owned(session_id, principal)
    if not principal.access.can("connection", s.connection_id, "connect"):
        await manager.close(s, "permission_revoked")
        raise Forbidden("access to this connection was revoked")
    if s.attached:
        raise Conflict("session is still attached", code="session_attached")
    if s.kind == "files":
        async with s.fs_lock:
            if s.fs is not None:
                await s.fs.close()
            async with session_scope() as db:
                s.fs = await open_fs(db, s.launch, settings, await get_runtime(db, settings))
            s.state = "connected"
        await audit.record_detached("session.reconnect", actor_id=principal.id, actor_name=principal.name,
                                    resource_type="connection", resource_id=s.connection_id,
                                    details={"session_id": str(s.id)})
        return _out(s)
    return _out(s, manager.issue_ticket(s))


@router.delete("/{session_id}", status_code=204, summary="Disconnect and close a session")
async def close_session(session_id: uuid.UUID, principal: Principal = Depends(principal_detached)) -> None:
    manager = get_manager()
    s = manager.sessions.get(session_id)
    if s is None:
        raise NotFound("session not found")
    if s.owner_id != principal.id:
        if not principal.access.has(Perm.SESSIONS_MANAGE):
            raise NotFound("session not found")
    await manager.close(s, "user_closed" if s.owner_id == principal.id else "terminated_by_admin",
                        actor_id=principal.id, actor_name=principal.name)


@router.post("/disconnect-all", summary="Close all my sessions")
async def disconnect_all(principal: Principal = Depends(principal_detached)) -> dict[str, int]:
    return {"closed": await get_manager().close_all_for_user(principal.id, "disconnect_all")}


# --------------------------------------------------------------------------- WebSockets


async def _ws_session(ws: WebSocket, session_id: uuid.UUID, kind: str) -> tuple[Session, Principal] | None:
    settings = get_settings()
    try:
        async with session_scope() as db:
            principal = await authenticate_websocket(ws, db, settings)
            await principal.access.load(db)
    except AppError as exc:
        log.info("ws_auth_failed", reason=exc.detail)
        await ws.close(code=1008)
        return None
    manager = get_manager()
    s = manager.sessions.get(session_id)
    ok = (
        s is not None
        and s.owner_id == principal.id
        and s.kind == kind
        and manager.consume_ticket(ws.query_params.get("ticket"), session_id, principal.id)
        and principal.access.can("connection", s.connection_id, "connect")
    )
    if not ok or s is None:
        await audit.record_detached("session.attach", actor_id=principal.id, actor_name=principal.name,
                                    resource_type="session", resource_id=session_id, outcome="denied")
        await ws.close(code=1008)
        return None
    if s.attached:
        await ws.close(code=1008, reason="already attached")
        return None
    return s, principal


@router.websocket("/{session_id}/tunnel")
async def guac_tunnel(ws: WebSocket, session_id: uuid.UUID) -> None:
    found = await _ws_session(ws, session_id, "guac")
    if found is None:
        return
    s, _ = found
    subprotocols = ws.scope.get("subprotocols") or []
    await ws.accept(subprotocol="guacamole" if "guacamole" in subprotocols else None)
    await run_guac_tunnel(ws, s, get_manager(), get_settings(), parse_client_info(ws.query_params, s.launch.settings))


@router.websocket("/{session_id}/terminal")
async def ssh_terminal(ws: WebSocket, session_id: uuid.UUID) -> None:
    found = await _ws_session(ws, session_id, "terminal")
    if found is None:
        return
    s, _ = found
    await ws.accept()

    def _int(name: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(ws.query_params.get(name) or default)))
        except ValueError:
            return default

    await run_terminal(ws, s, get_manager(), get_settings(), _int("cols", 80, 2, 1000), _int("rows", 24, 1, 500))


# --------------------------------------------------------------------------- file manager


async def _fs_session(session_id: uuid.UUID, principal: Principal) -> Session:
    s = _owned(session_id, principal)
    if s.kind != "files":
        raise ValidationFailed("not a file-transfer session")
    if not principal.access.can("connection", s.connection_id, "connect"):
        await get_manager().close(s, "permission_revoked")
        raise Forbidden("access to this connection was revoked")
    if s.fs is None or s.state != "connected":
        raise Conflict("session is not connected; reconnect first", code="session_disconnected")
    s.touch()
    return s


def _entry(e: object) -> FileEntry:
    return FileEntry.model_validate(e.as_dict())  # type: ignore[attr-defined]


async def _fs_audit(principal: Principal, s: Session, action: str, outcome: str = "success", **details: object) -> None:
    await audit.record_detached(action, actor_id=principal.id, actor_name=principal.name, resource_type="connection",
                                resource_id=s.connection_id, outcome=outcome,
                                details={"session_id": str(s.id), **details})


@router.get("/{session_id}/fs/home", summary="Initial directory")
async def fs_home(session_id: uuid.UUID, principal: Principal = Depends(principal_detached)) -> dict[str, str]:
    s = await _fs_session(session_id, principal)
    return {"path": await s.fs.home()}  # type: ignore[union-attr]


@router.get("/{session_id}/fs/list", response_model=DirListing, summary="List a directory")
async def fs_list(session_id: uuid.UUID, path: str = Query(...), principal: Principal = Depends(principal_detached)):
    s = await _fs_session(session_id, principal)
    path = clean_path(path)
    entries = await s.fs.list(path)  # type: ignore[union-attr]
    show_hidden = s.client_settings.get("show_hidden", False)
    items = [_entry(e) for e in entries if show_hidden or not e.name.startswith(".")]
    items.sort(key=lambda e: (e.type != "dir", e.name.lower()))
    return DirListing(path=path, entries=items)


@router.get("/{session_id}/fs/stat", response_model=FileEntry, summary="File information")
async def fs_stat(session_id: uuid.UUID, path: str = Query(...), principal: Principal = Depends(principal_detached)):
    s = await _fs_session(session_id, principal)
    return _entry(await s.fs.stat(path))  # type: ignore[union-attr]


@router.post("/{session_id}/fs/mkdir", status_code=204, summary="Create a directory")
async def fs_mkdir(session_id: uuid.UUID, body: MkdirIn, principal: Principal = Depends(principal_detached)) -> None:
    s = await _fs_session(session_id, principal)
    await s.fs.mkdir(body.path)  # type: ignore[union-attr]
    await _fs_audit(principal, s, "file.mkdir", path=body.path)


@router.post("/{session_id}/fs/rename", status_code=204, summary="Rename or move a file/directory")
async def fs_rename(session_id: uuid.UUID, body: RenameIn, principal: Principal = Depends(principal_detached)) -> None:
    s = await _fs_session(session_id, principal)
    await s.fs.rename(body.source, body.target, body.overwrite)  # type: ignore[union-attr]
    await _fs_audit(principal, s, "file.rename", source=body.source, target=body.target)


@router.post("/{session_id}/fs/delete", status_code=204, summary="Delete a file or directory")
async def fs_delete(session_id: uuid.UUID, body: DeleteIn, principal: Principal = Depends(principal_detached)) -> None:
    s = await _fs_session(session_id, principal)
    await s.fs.delete(body.path, body.recursive)  # type: ignore[union-attr]
    await _fs_audit(principal, s, "file.delete", path=body.path, recursive=body.recursive)


@router.post("/{session_id}/fs/chmod", status_code=204, summary="Change permissions (SFTP)")
async def fs_chmod(session_id: uuid.UUID, body: ChmodIn, principal: Principal = Depends(principal_detached)) -> None:
    s = await _fs_session(session_id, principal)
    await s.fs.chmod(body.path, body.mode)  # type: ignore[union-attr]
    await _fs_audit(principal, s, "file.chmod", path=body.path, mode=oct(body.mode))


@router.get("/{session_id}/fs/download", summary="Download a file (streamed)")
async def fs_download(session_id: uuid.UUID, path: str = Query(...),
                      principal: Principal = Depends(principal_detached)) -> StreamingResponse:
    s = await _fs_session(session_id, principal)
    dl = await s.fs.download(path)  # type: ignore[union-attr]
    transfer = Transfer(id=uuid.uuid4().hex, direction="download", path=clean_path(path), size=dl.size,
                        started_at=time.time())
    s.transfers[transfer.id] = transfer

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in dl.chunks:
                transfer.transferred += len(chunk)
                s.touch()
                yield chunk
            transfer.state = "done"
        except BaseException as exc:
            transfer.state = "failed" if not isinstance(exc, GeneratorExit) else "cancelled"
            transfer.error = str(exc)[:300] or type(exc).__name__
            raise
        finally:
            transfer.finished_at = time.time()
            await _fs_audit(principal, s, "file.download", "success" if transfer.state == "done" else "failure",
                            path=transfer.path, bytes=transfer.transferred, state=transfer.state)

    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(dl.name)}",
        "X-Transfer-Id": transfer.id,
        "Cache-Control": "no-store",
    }
    if dl.size is not None:
        headers["Content-Length"] = str(dl.size)
    return StreamingResponse(body(), media_type="application/octet-stream", headers=headers)


@router.put("/{session_id}/fs/upload", status_code=201, summary="Upload a file (raw request body, streamed)")
async def fs_upload(session_id: uuid.UUID, request: Request, path: str = Query(...),
                    overwrite: bool = Query(False), principal: Principal = Depends(principal_detached),
                    settings: Settings = Depends(settings_dep)) -> dict[str, object]:
    s = await _fs_session(session_id, principal)
    limit = settings.FILE_UPLOAD_MAX_MB * 1024 * 1024
    declared = request.headers.get("content-length")
    if limit and declared and declared.isdigit() and int(declared) > limit:
        raise ValidationFailed(f"file exceeds the upload limit of {settings.FILE_UPLOAD_MAX_MB} MB",
                               code="too_large")
    transfer = Transfer(id=request.headers.get("x-transfer-id") or uuid.uuid4().hex, direction="upload",
                        path=clean_path(path), size=int(declared) if declared and declared.isdigit() else None,
                        started_at=time.time())
    s.transfers[transfer.id] = transfer

    async def chunks() -> AsyncIterator[bytes]:
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if limit and total > limit:
                raise ValidationFailed("upload limit exceeded", code="too_large")
            s.touch()
            yield chunk

    try:
        written = await s.fs.upload(transfer.path, chunks(), overwrite, transfer)  # type: ignore[union-attr]
        transfer.state = "done"
    except BaseException as exc:
        transfer.state = "failed"
        transfer.error = getattr(exc, "detail", None) or str(exc)[:300] or type(exc).__name__
        await _fs_audit(principal, s, "file.upload", "failure", path=transfer.path, error=transfer.error)
        raise
    finally:
        transfer.finished_at = time.time()
    await _fs_audit(principal, s, "file.upload", path=transfer.path, bytes=written)
    return {"path": transfer.path, "bytes": written, "transfer_id": transfer.id}


@router.get("/{session_id}/transfers", summary="Transfer progress and errors")
async def list_transfers(session_id: uuid.UUID, principal: Principal = Depends(principal_detached)) -> list[dict]:
    s = _owned(session_id, principal)
    cutoff = time.time() - 600
    for tid in [t.id for t in s.transfers.values() if t.finished_at and t.finished_at < cutoff]:
        s.transfers.pop(tid, None)
    return [t.__dict__ for t in s.transfers.values()]
