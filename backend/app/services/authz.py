"""Authorisation: role-based global permissions + resource ACLs.

Model
-----
* A user has roles (manual or mapped from IdP groups). Each role grants
  global permissions (``Perm``) and has a ``resource_cap``, the highest
  resource-level permission its members can hold.
* Resource permissions are ordered: ``view < connect < edit < manage``.
* A user's level on a resource is the maximum of
  - ACL grants to the user, any of their groups, or any of their roles, on
    the resource or on any ancestor (folders, parent connections, parent
    credentials), including the virtual root (``ROOT_ID``);
  - ``manage`` if the user owns the resource or an ancestor.
  This maximum is then capped by the user's resource cap.
* Holders of the ``*`` permission (administrators) bypass ACLs.

New permission types can be added as global permission strings or new
resource levels without changing the evaluation structure.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.errors import Forbidden

ROOT_ID = uuid.UUID(int=0)
LEVELS = {"none": 0, "view": 1, "connect": 2, "edit": 3, "manage": 4}
LEVEL_NAMES = {v: k for k, v in LEVELS.items()}
MAX_DEPTH = 64


class Perm(StrEnum):
    ALL = "*"
    API_ACCESS = "api.access"
    USERS_MANAGE = "users.manage"
    ROLES_MANAGE = "roles.manage"
    SETTINGS_MANAGE = "settings.manage"
    AUDIT_READ = "audit.read"
    SESSIONS_MANAGE = "sessions.manage"
    SECRETS_MANAGE = "secrets.manage"
    IMPORT_RUN = "import.run"
    FOLDERS_CREATE = "folders.create"
    CONNECTIONS_CREATE = "connections.create"
    CREDENTIALS_CREATE = "credentials.create"
    CREDENTIALS_REVEAL = "credentials.reveal"
    KNOWN_HOSTS_MANAGE = "known_hosts.manage"
    ACL_MANAGE = "acl.manage"


ALL_PERMISSIONS = [p.value for p in Perm]

BUILTIN_ROLES: dict[str, dict[str, object]] = {
    "admin": {
        "description": "Full access to all resources and administration",
        "permissions": [Perm.ALL.value],
        "resource_cap": "manage",
    },
    "user": {
        "description": "Standard user: manages own and shared resources",
        "permissions": [
            Perm.API_ACCESS.value,
            Perm.FOLDERS_CREATE.value,
            Perm.CONNECTIONS_CREATE.value,
            Perm.CREDENTIALS_CREATE.value,
            Perm.CREDENTIALS_REVEAL.value,
            Perm.IMPORT_RUN.value,
            Perm.ACL_MANAGE.value,
        ],
        "resource_cap": "manage",
    },
    "readonly": {
        "description": "Read-only user: can view and open granted connections, cannot change anything",
        "permissions": [Perm.API_ACCESS.value],
        "resource_cap": "connect",
    },
}


def level(name: str) -> int:
    return LEVELS[name]


@dataclass
class _Node:
    parent_id: uuid.UUID | None
    owner_id: uuid.UUID | None
    folder_id: uuid.UUID | None = None


@dataclass
class AccessContext:
    user_id: uuid.UUID
    username: str
    roles: list[str]
    role_ids: set[uuid.UUID]
    group_ids: set[uuid.UUID]
    permissions: set[str]
    cap: int
    read_only_token: bool = False
    # Signage account: no global permissions, "connect" on exactly this connection, nothing else.
    signage: bool = False
    signage_connection_id: uuid.UUID | None = None
    _acl: dict[tuple[str, uuid.UUID], int] | None = None
    _folders: dict[uuid.UUID, _Node] | None = None
    _connections: dict[uuid.UUID, _Node] | None = None
    _credentials: dict[uuid.UUID, _Node] | None = None
    _memo: dict[tuple[str, uuid.UUID], int] = field(default_factory=dict)

    # ------------------------------------------------------------------ global
    @property
    def is_admin(self) -> bool:
        return not self.signage and Perm.ALL.value in self.permissions

    def has(self, perm: Perm | str) -> bool:
        if self.signage:
            return False
        if self.read_only_token and perm not in (Perm.API_ACCESS, Perm.AUDIT_READ):
            return False
        return self.is_admin or str(perm) in self.permissions

    def require(self, perm: Perm | str) -> None:
        if not self.has(perm):
            raise Forbidden(f"missing permission '{perm}'")

    # ------------------------------------------------------------------ loading
    async def load(self, db: AsyncSession) -> AccessContext:
        """Load ACL grants for this user's principals and the resource trees."""
        if self._acl is not None:
            return self
        principals = [("user", self.user_id)] + [("group", g) for g in self.group_ids] + [
            ("role", r) for r in self.role_ids
        ]
        acl: dict[tuple[str, uuid.UUID], int] = {}
        if not self.is_admin:
            rows = (
                await db.execute(
                    select(
                        m.AclEntry.resource_type,
                        m.AclEntry.resource_id,
                        m.AclEntry.permission,
                        m.AclEntry.principal_type,
                        m.AclEntry.principal_id,
                    ).where(m.AclEntry.principal_id.in_([p[1] for p in principals]))
                )
            ).all()
            wanted = set(principals)
            for rtype, rid, perm, ptype, pid in rows:
                # match the exact (type, id) pair, not just the id
                if (ptype, pid) in wanted:
                    key = (rtype, rid)
                    acl[key] = max(acl.get(key, 0), LEVELS[perm])
        self._acl = acl
        self._folders = {
            r.id: _Node(r.parent_id, r.owner_id)
            for r in (await db.execute(select(m.Folder.id, m.Folder.parent_id, m.Folder.owner_id))).all()
        }
        self._connections = {
            r.id: _Node(r.parent_id, r.owner_id, r.folder_id)
            for r in (
                await db.execute(
                    select(m.Connection.id, m.Connection.parent_id, m.Connection.owner_id, m.Connection.folder_id)
                )
            ).all()
        }
        self._credentials = {
            r.id: _Node(r.parent_id, r.owner_id)
            for r in (
                await db.execute(select(m.Credential.id, m.Credential.parent_id, m.Credential.owner_id))
            ).all()
        }
        self._memo.clear()
        return self

    def invalidate(self) -> None:
        self._acl = None
        self._folders = self._connections = self._credentials = None
        self._memo.clear()

    # ------------------------------------------------------------------ evaluation
    def _own(self, rtype: str, rid: uuid.UUID, node: _Node) -> int:
        assert self._acl is not None
        lvl = self._acl.get((rtype, rid), 0)
        if node.owner_id is not None and node.owner_id == self.user_id:
            lvl = LEVELS["manage"]
        return lvl

    def _folder_raw(self, fid: uuid.UUID | None) -> int:
        assert self._folders is not None and self._acl is not None
        best = self._acl.get(("folder", ROOT_ID), 0)
        seen = 0
        while fid is not None and seen < MAX_DEPTH:
            key = ("folder", fid)
            if key in self._memo:
                return max(best, self._memo[key])
            node = self._folders.get(fid)
            if node is None:
                break
            best = max(best, self._own("folder", fid, node))
            fid = node.parent_id
            seen += 1
        return best

    def _connection_raw(self, cid: uuid.UUID) -> int:
        assert self._connections is not None
        best = 0
        seen = 0
        current: uuid.UUID | None = cid
        folder_id: uuid.UUID | None = None
        while current is not None and seen < MAX_DEPTH:
            node = self._connections.get(current)
            if node is None:
                break
            best = max(best, self._own("connection", current, node))
            folder_id = node.folder_id
            current = node.parent_id
            seen += 1
        return max(best, self._folder_raw(folder_id))

    def _credential_raw(self, crid: uuid.UUID) -> int:
        assert self._credentials is not None and self._acl is not None
        best = self._acl.get(("credential", ROOT_ID), 0)
        current: uuid.UUID | None = crid
        seen = 0
        while current is not None and seen < MAX_DEPTH:
            node = self._credentials.get(current)
            if node is None:
                break
            best = max(best, self._own("credential", current, node))
            current = node.parent_id
            seen += 1
        return best

    def level_of(self, rtype: str, rid: uuid.UUID | None) -> int:
        if self._acl is None:
            raise RuntimeError("AccessContext.load() must be awaited first")
        if self.signage:  # roles and ACLs do not apply: the assignment is the only grant
            hit = rtype == "connection" and rid is not None and rid == self.signage_connection_id
            return LEVELS["connect"] if hit else LEVELS["none"]
        if self.is_admin:
            lvl = LEVELS["manage"]
        elif rid is None or rid == ROOT_ID:
            lvl = self._acl.get((rtype if rtype != "connection" else "folder", ROOT_ID), 0)
        else:
            key = (rtype, rid)
            if key not in self._memo:
                if rtype == "folder":
                    self._memo[key] = self._folder_raw(rid)
                elif rtype == "connection":
                    self._memo[key] = self._connection_raw(rid)
                elif rtype == "credential":
                    self._memo[key] = self._credential_raw(rid)
                else:
                    raise ValueError(rtype)
            lvl = self._memo[key]
        lvl = min(lvl, self.cap)
        if self.read_only_token:
            lvl = min(lvl, LEVELS["connect"])
        return lvl

    def can(self, rtype: str, rid: uuid.UUID | None, needed: str) -> bool:
        return self.level_of(rtype, rid) >= LEVELS[needed]

    def ensure(self, rtype: str, rid: uuid.UUID | None, needed: str) -> None:
        if not self.can(rtype, rid, needed):
            raise Forbidden(f"'{needed}' permission required on {rtype}")

    def can_create_in_folder(self, folder_id: uuid.UUID | None, perm: Perm) -> bool:
        """Creating at root needs the global create permission; inside a folder also edit on it."""
        if not self.has(perm):
            return False
        if folder_id is None:
            return True
        return self.can("folder", folder_id, "edit")

    # ------------------------------------------------------------------ tree helpers
    def visible_connection_ids(self) -> set[uuid.UUID]:
        assert self._connections is not None
        return {cid for cid in self._connections if self.can("connection", cid, "view")}

    def visible_folder_ids(self, visible_connections: set[uuid.UUID]) -> set[uuid.UUID]:
        """Folders the user can view, plus ancestors needed to reach visible items."""
        assert self._folders is not None and self._connections is not None
        out = {fid for fid in self._folders if self.can("folder", fid, "view")}
        needed: set[uuid.UUID] = set()
        for cid in visible_connections:
            node = self._connections[cid]
            # climb to the top-level connection to find its folder
            seen = 0
            while node.parent_id is not None and seen < MAX_DEPTH:
                node = self._connections.get(node.parent_id, node)
                seen += 1
            if node.folder_id:
                needed.add(node.folder_id)
        for fid in list(out) + list(needed):
            seen = 0
            cur: uuid.UUID | None = fid
            while cur is not None and seen < MAX_DEPTH:
                out.add(cur)
                cur = self._folders[cur].parent_id if cur in self._folders else None
                seen += 1
        return out


async def build_access_context(
    db: AsyncSession, user: m.User, *, read_only_token: bool = False
) -> AccessContext:
    role_rows = (
        await db.execute(
            select(m.Role.id, m.Role.name, m.Role.permissions, m.Role.resource_cap)
            .join(m.UserRole, m.UserRole.role_id == m.Role.id)
            .where(m.UserRole.user_id == user.id)
        )
    ).all()
    group_ids = set(
        (await db.execute(select(m.UserGroup.group_id).where(m.UserGroup.user_id == user.id))).scalars()
    )
    perms: set[str] = set()
    cap = 0
    for r in role_rows:
        perms.update(r.permissions or [])
        cap = max(cap, LEVELS[r.resource_cap])
    return AccessContext(
        user_id=user.id,
        username=user.username,
        roles=sorted(r.name for r in role_rows),
        role_ids={r.id for r in role_rows},
        group_ids=group_ids,
        permissions=perms,
        cap=cap,
        read_only_token=read_only_token,
        signage=bool(user.is_signage),
        signage_connection_id=user.signage_connection_id if user.is_signage else None,
    )


async def ensure_builtin_roles(db: AsyncSession) -> dict[str, m.Role]:
    existing = {r.name: r for r in (await db.execute(select(m.Role))).scalars()}
    for name, spec in BUILTIN_ROLES.items():
        role = existing.get(name)
        if role is None:
            role = m.Role(
                name=name,
                description=str(spec["description"]),
                builtin=True,
                permissions=list(spec["permissions"]),  # type: ignore[arg-type]
                resource_cap=str(spec["resource_cap"]),
            )
            db.add(role)
            existing[name] = role
        elif role.builtin:
            # Built-in roles are kept in sync with code; custom roles are user-managed.
            role.permissions = list(spec["permissions"])  # type: ignore[arg-type]
            role.resource_cap = str(spec["resource_cap"])
    await db.flush()
    return existing
