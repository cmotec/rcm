"""Folders, connections and the navigation tree."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.api.common import Paging, check_version, idempotency, page, paging
from app.api.deps import Principal, get_access, settings_dep
from app.config import Settings
from app.db import get_db
from app.errors import Conflict, Forbidden, NotFound, ValidationFailed
from app.schemas.core import (
    ConnectionCreate,
    ConnectionDuplicate,
    ConnectionMove,
    ConnectionOut,
    ConnectionUpdate,
    FolderCreate,
    FolderOut,
    FolderUpdate,
    Page,
    SecurityWarning,
    TreeOut,
)
from app.schemas.protocol_settings import security_warnings, validate_settings
from app.security.crypto import aad_for, get_box
from app.services import audit
from app.services.app_settings import RuntimeSettings, get_runtime
from app.services.authz import LEVEL_NAMES, AccessContext, Perm
from app.services.resolution import resolve_connection, would_create_cycle

router = APIRouter(tags=["connections"])

CONN_SIMPLE_FIELDS = ("hostname", "port", "username", "domain", "search_domain", "description", "sort_order")


# --------------------------------------------------------------------------- serialisation


def folder_out(f: m.Folder, access: AccessContext) -> FolderOut:
    out = FolderOut.model_validate(f)
    out.permission = LEVEL_NAMES.get(access.level_of("folder", f.id)) or None
    return out


def connection_out(c: m.Connection, access: AccessContext, runtime: RuntimeSettings,
                   favorites: set[uuid.UUID] | None = None) -> ConnectionOut:
    out = ConnectionOut.model_validate(c)
    out.password_set = c.password_enc is not None
    out.permission = LEVEL_NAMES.get(access.level_of("connection", c.id)) or None
    out.favorite = bool(favorites and c.id in favorites)
    out.warnings = [SecurityWarning(**w) for w in security_warnings(
        c.protocol, c.settings or {}, rdp_default=runtime.rdp_cert_policy_default,
        ftp_verify_default=runtime.ftp_tls_verify_default, ssh_default=runtime.ssh_host_key_policy_default,
    )]
    return out


async def _favorites(db: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    return set((await db.execute(select(m.Favorite.connection_id).where(m.Favorite.user_id == user_id))).scalars())


async def _get_connection(db: AsyncSession, access: AccessContext, cid: uuid.UUID, needed: str) -> m.Connection:
    conn = await db.get(m.Connection, cid)
    if conn is None or not access.can("connection", cid, "view"):
        raise NotFound("connection not found")
    access.ensure("connection", cid, needed)
    return conn


async def _get_folder(db: AsyncSession, access: AccessContext, fid: uuid.UUID, needed: str) -> m.Folder:
    folder = await db.get(m.Folder, fid)
    if folder is None or not access.can("folder", fid, "view"):
        raise NotFound("folder not found")
    access.ensure("folder", fid, needed)
    return folder


# --------------------------------------------------------------------------- tree


@router.get("/tree", response_model=TreeOut, summary="Folders and connections visible to the caller")
async def tree(principal: Principal = Depends(get_access), db: AsyncSession = Depends(get_db, scope="function"),
               settings: Settings = Depends(settings_dep)) -> TreeOut:
    access = principal.access
    runtime = await get_runtime(db, settings)
    visible_c = access.visible_connection_ids()
    visible_f = access.visible_folder_ids(visible_c)
    folders = (await db.execute(select(m.Folder).where(m.Folder.id.in_(visible_f)))).scalars().all() if visible_f else []
    conns = (
        (await db.execute(select(m.Connection).where(m.Connection.id.in_(visible_c)))).scalars().all()
        if visible_c else []
    )
    favs = await _favorites(db, principal.id)
    return TreeOut(
        folders=sorted((folder_out(f, access) for f in folders), key=lambda f: (f.sort_order, f.name.lower())),
        connections=sorted((connection_out(c, access, runtime, favs) for c in conns),
                           key=lambda c: (c.sort_order, c.name.lower())),
    )


# --------------------------------------------------------------------------- folders


@router.get("/folders", response_model=list[FolderOut], summary="List visible folders")
async def list_folders(principal: Principal = Depends(get_access), db: AsyncSession = Depends(get_db, scope="function")):
    access = principal.access
    ids = access.visible_folder_ids(access.visible_connection_ids())
    if not ids:
        return []
    rows = (await db.execute(select(m.Folder).where(m.Folder.id.in_(ids)).order_by(m.Folder.name))).scalars()
    return [folder_out(f, access) for f in rows]


@router.post("/folders", response_model=FolderOut, status_code=201, summary="Create a folder")
async def create_folder(body: FolderCreate, request: Request, principal: Principal = Depends(get_access),
                        db: AsyncSession = Depends(get_db, scope="function")):
    access = principal.access
    if body.parent_id:
        await _get_folder(db, access, body.parent_id, "view")
    if not access.can_create_in_folder(body.parent_id, Perm.FOLDERS_CREATE):
        raise Forbidden("not allowed to create folders here")
    idem = await idempotency(db, request, principal, body)
    if idem.replay:
        return idem.replay
    folder = m.Folder(
        name=body.name, parent_id=body.parent_id, icon=body.icon, color=body.color, description=body.description,
        search_domain=body.search_domain, sort_order=body.sort_order or 0, owner_id=principal.id,
    )
    db.add(folder)
    await db.flush()
    await db.refresh(folder)
    access.invalidate()
    await access.load(db)
    await audit.record(db, "folder.create", actor_id=principal.id, actor_name=principal.name,
                       resource_type="folder", resource_id=folder.id, details={"name": folder.name})
    out = folder_out(folder, access)
    await idem.store(201, out.model_dump(mode="json"))
    return out


@router.get("/folders/{folder_id}", response_model=FolderOut, summary="Get a folder")
async def get_folder(folder_id: uuid.UUID, principal: Principal = Depends(get_access),
                     db: AsyncSession = Depends(get_db, scope="function")):
    return folder_out(await _get_folder(db, principal.access, folder_id, "view"), principal.access)


@router.patch("/folders/{folder_id}", response_model=FolderOut, summary="Update / rename / move a folder")
async def update_folder(folder_id: uuid.UUID, body: FolderUpdate, principal: Principal = Depends(get_access),
                        db: AsyncSession = Depends(get_db, scope="function")):
    access = principal.access
    folder = await _get_folder(db, access, folder_id, "edit")
    check_version(folder.version, body.version)
    fields = body.model_fields_set
    if "parent_id" in fields and body.parent_id != folder.parent_id:
        if body.parent_id is not None:
            await _get_folder(db, access, body.parent_id, "edit")
            if await would_create_cycle(db, m.Folder, folder.id, body.parent_id):
                raise ValidationFailed("a folder cannot be moved into itself or a descendant")
        elif not access.has(Perm.FOLDERS_CREATE):
            raise Forbidden("not allowed to move folders to the root")
        access.ensure("folder", folder.id, "manage")
        folder.parent_id = body.parent_id
    for name in ("name", "icon", "color", "description", "search_domain", "sort_order"):
        if name in fields:
            value = getattr(body, name)
            if name in ("name", "sort_order") and value is None:
                continue
            setattr(folder, name, value)
    folder.version += 1
    await db.flush()
    await db.refresh(folder)
    await audit.record(db, "folder.update", actor_id=principal.id, actor_name=principal.name,
                       resource_type="folder", resource_id=folder.id,
                       details={"fields": sorted(fields - {"version"})})
    access.invalidate()
    await access.load(db)
    return folder_out(folder, access)


@router.delete("/folders/{folder_id}", status_code=204, summary="Delete a folder")
async def delete_folder(folder_id: uuid.UUID, recursive: bool = Query(False),
                        principal: Principal = Depends(get_access), db: AsyncSession = Depends(get_db, scope="function")) -> None:
    access = principal.access
    folder = await _get_folder(db, access, folder_id, "manage")
    sub = (await db.execute(select(m.Folder).where(m.Folder.parent_id == folder.id))).scalars().all()
    conns = (await db.execute(select(m.Connection).where(m.Connection.folder_id == folder.id))).scalars().all()
    if (sub or conns) and not recursive:
        raise Conflict("folder is not empty (use recursive=true)", code="folder_not_empty")
    for c in conns:
        await _delete_connection_tree(db, access, c)
    for f in sub:
        await delete_folder(f.id, True, principal, db)  # type: ignore[arg-type]
    await db.delete(folder)
    await db.flush()
    await audit.record(db, "folder.delete", actor_id=principal.id, actor_name=principal.name,
                       resource_type="folder", resource_id=folder.id, details={"name": folder.name, "recursive": recursive})


# --------------------------------------------------------------------------- connections


@router.get("/connections", response_model=Page[ConnectionOut], summary="List / search connections")
async def list_connections(
    q: str | None = Query(None, max_length=200, description="matches name, hostname, description, tags"),
    protocol: str | None = Query(None),
    folder_id: uuid.UUID | None = Query(None),
    parent_id: uuid.UUID | None = Query(None),
    tag: str | None = Query(None),
    favorite: bool | None = Query(None),
    p: Paging = Depends(paging),
    principal: Principal = Depends(get_access),
    db: AsyncSession = Depends(get_db, scope="function"),
    settings: Settings = Depends(settings_dep),
):
    access = principal.access
    stmt = select(m.Connection)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(
            func.lower(m.Connection.name).like(like),
            func.lower(m.Connection.hostname).like(like),
            func.lower(m.Connection.description).like(like),
            m.Connection.tags.any(q),
        ))
    if protocol:
        stmt = stmt.where(m.Connection.protocol == protocol)
    if folder_id:
        stmt = stmt.where(m.Connection.folder_id == folder_id)
    if parent_id:
        stmt = stmt.where(m.Connection.parent_id == parent_id)
    if tag:
        stmt = stmt.where(m.Connection.tags.any(tag))
    favs = await _favorites(db, principal.id)
    if favorite:
        stmt = stmt.where(m.Connection.id.in_(favs or {uuid.UUID(int=0)}))
    rows = (await db.execute(stmt.order_by(func.lower(m.Connection.name)))).scalars().all()
    visible = [c for c in rows if access.can("connection", c.id, "view")]
    runtime = await get_runtime(db, settings)
    items = [connection_out(c, access, runtime, favs) for c in visible[p.offset : p.offset + p.limit]]
    return page(items, len(visible), p)


async def _check_references(db: AsyncSession, access: AccessContext, settings_data: dict[str, Any],
                            credential_id: uuid.UUID | None) -> None:
    if credential_id is not None:
        cred = await db.get(m.Credential, credential_id)
        if cred is None or not access.can("credential", credential_id, "view"):
            raise ValidationFailed("credential not found")
        if not access.can("credential", credential_id, "connect"):
            raise Forbidden("you may not use this credential ('connect' permission required)")
    for key in ("jump_host_connection_id",):
        ref = settings_data.get(key)
        if ref:
            await _require_ssh_ref(db, access, uuid.UUID(str(ref)))
    tunnel = settings_data.get("ssh_tunnel")
    if tunnel and tunnel.get("via_connection_id"):
        await _require_ssh_ref(db, access, uuid.UUID(str(tunnel["via_connection_id"])))


async def _require_ssh_ref(db: AsyncSession, access: AccessContext, cid: uuid.UUID) -> None:
    ref = await db.get(m.Connection, cid)
    if ref is None or not access.can("connection", cid, "view"):
        raise ValidationFailed("referenced SSH connection not found")
    if ref.protocol != "ssh":
        raise ValidationFailed("referenced connection must use the SSH protocol")
    if not access.can("connection", cid, "connect"):
        raise Forbidden("'connect' permission on the referenced SSH connection is required")


def _apply_password(conn: m.Connection, password: str | None) -> None:
    conn.password_enc = get_box().encrypt(password, aad_for("connections", "password", conn.id)) if password else None


def _normalise_auth(conn: m.Connection) -> None:
    if conn.auth_mode != "direct":
        conn.password_enc = None
    if conn.auth_mode != "credential":
        conn.credential_id = None
    if conn.auth_mode in ("inherit", "credential"):
        conn.username = None
        conn.domain = None


@router.post("/connections", response_model=ConnectionOut, status_code=201, summary="Create a connection")
async def create_connection(body: ConnectionCreate, request: Request, principal: Principal = Depends(get_access),
                            db: AsyncSession = Depends(get_db, scope="function"), settings: Settings = Depends(settings_dep)):
    access = principal.access
    if body.parent_id and body.folder_id:
        raise ValidationFailed("a child connection lives under its parent; do not set folder_id as well")
    if body.parent_id:
        parent = await _get_connection(db, access, body.parent_id, "edit")
        if not access.has(Perm.CONNECTIONS_CREATE):
            raise Forbidden("missing permission 'connections.create'")
        del parent
    else:
        if body.folder_id:
            await _get_folder(db, access, body.folder_id, "view")
        if not access.can_create_in_folder(body.folder_id, Perm.CONNECTIONS_CREATE):
            raise Forbidden("not allowed to create connections here")
    auth_mode = body.auth_mode or "inherit"
    if auth_mode == "credential" and not body.credential_id:
        raise ValidationFailed("auth_mode 'credential' requires credential_id")
    settings_data = validate_settings(body.protocol, body.settings)
    await _check_references(db, access, settings_data, body.credential_id if auth_mode == "credential" else None)

    idem = await idempotency(db, request, principal, body)
    if idem.replay:
        return idem.replay
    conn = m.Connection(
        id=uuid.uuid4(), name=body.name, protocol=body.protocol, hostname=body.hostname, port=body.port,
        folder_id=body.folder_id, parent_id=body.parent_id, auth_mode=auth_mode,
        inherit_auth=True if body.inherit_auth is None else body.inherit_auth,
        username=body.username, domain=body.domain, credential_id=body.credential_id,
        search_domain=body.search_domain, description=body.description, tags=body.tags or [],
        settings=settings_data, sort_order=body.sort_order or 0, owner_id=principal.id,
    )
    _apply_password(conn, body.password)
    _normalise_auth(conn)
    db.add(conn)
    await db.flush()
    await db.refresh(conn)
    access.invalidate()
    await access.load(db)
    await audit.record(db, "connection.create", actor_id=principal.id, actor_name=principal.name,
                       resource_type="connection", resource_id=conn.id,
                       details={"name": conn.name, "protocol": conn.protocol, "auth_mode": conn.auth_mode})
    out = connection_out(conn, access, await get_runtime(db, settings))
    await idem.store(201, out.model_dump(mode="json"))
    return out


@router.get("/connections/{connection_id}", response_model=ConnectionOut, summary="Get a connection")
async def get_connection(connection_id: uuid.UUID, principal: Principal = Depends(get_access),
                         db: AsyncSession = Depends(get_db, scope="function"), settings: Settings = Depends(settings_dep)):
    conn = await _get_connection(db, principal.access, connection_id, "view")
    return connection_out(conn, principal.access, await get_runtime(db, settings), await _favorites(db, principal.id))


@router.get("/connections/{connection_id}/effective", summary="Effective (inherited) values and their sources")
async def effective_connection(connection_id: uuid.UUID, principal: Principal = Depends(get_access),
                               db: AsyncSession = Depends(get_db, scope="function"),
                               settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    await _get_connection(db, principal.access, connection_id, "view")
    eff = await resolve_connection(db, connection_id, box=None, include_secrets=False,
                                   env_search_domains=settings.search_domains)
    return eff.public()


@router.patch("/connections/{connection_id}", response_model=ConnectionOut, summary="Update a connection")
async def update_connection(connection_id: uuid.UUID, body: ConnectionUpdate,
                            principal: Principal = Depends(get_access), db: AsyncSession = Depends(get_db, scope="function"),
                            settings: Settings = Depends(settings_dep)):
    access = principal.access
    conn = await _get_connection(db, access, connection_id, "edit")
    check_version(conn.version, body.version)
    fields = body.model_fields_set - {"version"}
    if "name" in fields and body.name:
        conn.name = body.name
    if "protocol" in fields and body.protocol and body.protocol != conn.protocol:
        conn.protocol = body.protocol
        if "settings" not in fields:
            conn.settings = validate_settings(conn.protocol, {})
    if "settings" in fields:
        conn.settings = validate_settings(conn.protocol, body.settings)
    for name in CONN_SIMPLE_FIELDS:
        if name in fields:
            value = getattr(body, name)
            setattr(conn, name, 0 if name == "sort_order" and value is None else value)
    if "tags" in fields:
        conn.tags = body.tags or []
    if "inherit_auth" in fields and body.inherit_auth is not None:
        conn.inherit_auth = body.inherit_auth
    if "auth_mode" in fields and body.auth_mode:
        conn.auth_mode = body.auth_mode
    if "credential_id" in fields:
        conn.credential_id = body.credential_id
    if "password" in fields:
        _apply_password(conn, body.password)
    if conn.auth_mode == "credential" and not conn.credential_id:
        raise ValidationFailed("auth_mode 'credential' requires credential_id")
    await _check_references(
        db, access, conn.settings or {},
        conn.credential_id if ("credential_id" in fields and conn.auth_mode == "credential") else None,
    )
    _normalise_auth(conn)
    conn.version += 1
    await db.flush()
    await db.refresh(conn)
    await audit.record(db, "connection.update", actor_id=principal.id, actor_name=principal.name,
                       resource_type="connection", resource_id=conn.id,
                       details={"fields": sorted(fields), "password_changed": "password" in fields})
    return connection_out(conn, access, await get_runtime(db, settings), await _favorites(db, principal.id))


async def _delete_connection_tree(db: AsyncSession, access: AccessContext, conn: m.Connection) -> None:
    children = (await db.execute(select(m.Connection).where(m.Connection.parent_id == conn.id))).scalars().all()
    for child in children:
        await _delete_connection_tree(db, access, child)
    access.ensure("connection", conn.id, "manage")
    await db.delete(conn)
    await db.flush()


@router.delete("/connections/{connection_id}", status_code=204, summary="Delete a connection")
async def delete_connection(connection_id: uuid.UUID, recursive: bool = Query(False),
                            principal: Principal = Depends(get_access), db: AsyncSession = Depends(get_db, scope="function")) -> None:
    access = principal.access
    conn = await _get_connection(db, access, connection_id, "manage")
    has_children = (await db.execute(select(func.count()).where(m.Connection.parent_id == conn.id))).scalar_one()
    if has_children and not recursive:
        raise Conflict("connection has child connections (use recursive=true)", code="has_children")
    referenced = (await db.execute(select(func.count()).select_from(m.Connection).where(
        or_(m.Connection.settings["jump_host_connection_id"].astext == str(conn.id),
            m.Connection.settings["ssh_tunnel"]["via_connection_id"].astext == str(conn.id))
    ))).scalar_one()
    if referenced:
        raise Conflict(f"connection is used as jump host / tunnel by {referenced} other connection(s)",
                       code="in_use")
    name = conn.name
    await _delete_connection_tree(db, access, conn)
    await audit.record(db, "connection.delete", actor_id=principal.id, actor_name=principal.name,
                       resource_type="connection", resource_id=connection_id,
                       details={"name": name, "recursive": recursive})


@router.post("/connections/{connection_id}/move", response_model=ConnectionOut,
             summary="Move a connection to a folder, under a parent, or to the root")
async def move_connection(connection_id: uuid.UUID, body: ConnectionMove, principal: Principal = Depends(get_access),
                          db: AsyncSession = Depends(get_db, scope="function"), settings: Settings = Depends(settings_dep)):
    access = principal.access
    conn = await _get_connection(db, access, connection_id, "manage")
    if body.parent_id and body.folder_id:
        raise ValidationFailed("set either folder_id or parent_id, not both")
    if body.parent_id:
        await _get_connection(db, access, body.parent_id, "edit")
        if await would_create_cycle(db, m.Connection, conn.id, body.parent_id):
            raise ValidationFailed("a connection cannot become a child of itself or its descendants")
    elif body.folder_id:
        await _get_folder(db, access, body.folder_id, "edit")
    elif not access.has(Perm.CONNECTIONS_CREATE):
        raise Forbidden("not allowed to move connections to the root")
    before = {"folder_id": str(conn.folder_id) if conn.folder_id else None,
              "parent_id": str(conn.parent_id) if conn.parent_id else None}
    conn.folder_id, conn.parent_id = body.folder_id, body.parent_id
    if body.sort_order is not None:
        conn.sort_order = body.sort_order
    conn.version += 1
    await db.flush()
    await db.refresh(conn)
    access.invalidate()
    await access.load(db)
    await audit.record(db, "connection.move", actor_id=principal.id, actor_name=principal.name,
                       resource_type="connection", resource_id=conn.id,
                       details={"from": before, "to": {"folder_id": str(body.folder_id) if body.folder_id else None,
                                                       "parent_id": str(body.parent_id) if body.parent_id else None}})
    return connection_out(conn, access, await get_runtime(db, settings), await _favorites(db, principal.id))


async def _clone(db: AsyncSession, src: m.Connection, *, name: str, folder_id: uuid.UUID | None,
                 parent_id: uuid.UUID | None, owner_id: uuid.UUID, include_children: bool) -> m.Connection:
    box = get_box()
    new = m.Connection(
        id=uuid.uuid4(), name=name, protocol=src.protocol, hostname=src.hostname, port=src.port,
        folder_id=folder_id, parent_id=parent_id, auth_mode=src.auth_mode, inherit_auth=src.inherit_auth,
        username=src.username, domain=src.domain, credential_id=src.credential_id,
        search_domain=src.search_domain, description=src.description, tags=list(src.tags or []),
        settings=dict(src.settings or {}), sort_order=src.sort_order, owner_id=owner_id,
    )
    if src.password_enc:
        plain = box.decrypt(src.password_enc, aad_for("connections", "password", src.id))
        new.password_enc = box.encrypt(plain, aad_for("connections", "password", new.id))
    db.add(new)
    await db.flush()
    if include_children:
        children = (await db.execute(select(m.Connection).where(m.Connection.parent_id == src.id))).scalars().all()
        for child in children:
            await _clone(db, child, name=child.name, folder_id=None, parent_id=new.id, owner_id=owner_id,
                         include_children=True)
    return new


@router.post("/connections/{connection_id}/duplicate", response_model=ConnectionOut, status_code=201,
             summary="Duplicate a connection (optionally with its children)")
async def duplicate_connection(connection_id: uuid.UUID, body: ConnectionDuplicate,
                               principal: Principal = Depends(get_access), db: AsyncSession = Depends(get_db, scope="function"),
                               settings: Settings = Depends(settings_dep)):
    access = principal.access
    src = await _get_connection(db, access, connection_id, "edit")
    if src.parent_id:
        access.ensure("connection", src.parent_id, "edit")
    elif not access.can_create_in_folder(src.folder_id, Perm.CONNECTIONS_CREATE):
        raise Forbidden("not allowed to create connections here")
    new = await _clone(db, src, name=body.name or f"{src.name} (copy)", folder_id=src.folder_id,
                       parent_id=src.parent_id, owner_id=principal.id, include_children=body.include_children)
    await db.refresh(new)
    access.invalidate()
    await access.load(db)
    await audit.record(db, "connection.duplicate", actor_id=principal.id, actor_name=principal.name,
                       resource_type="connection", resource_id=new.id,
                       details={"source": str(src.id), "include_children": body.include_children})
    return connection_out(new, access, await get_runtime(db, settings))


@router.put("/connections/{connection_id}/favorite", status_code=204, summary="Mark as favorite")
async def add_favorite(connection_id: uuid.UUID, principal: Principal = Depends(get_access),
                       db: AsyncSession = Depends(get_db, scope="function")) -> Response:
    await _get_connection(db, principal.access, connection_id, "view")
    if await db.get(m.Favorite, (principal.id, connection_id)) is None:
        db.add(m.Favorite(user_id=principal.id, connection_id=connection_id))
    return Response(status_code=204)


@router.delete("/connections/{connection_id}/favorite", status_code=204, summary="Remove favorite")
async def remove_favorite(connection_id: uuid.UUID, principal: Principal = Depends(get_access),
                          db: AsyncSession = Depends(get_db, scope="function")) -> Response:
    fav = await db.get(m.Favorite, (principal.id, connection_id))
    if fav is not None:
        await db.delete(fav)
    return Response(status_code=204)
