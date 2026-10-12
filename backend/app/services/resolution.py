"""Resolution of effective connection values (hostname, port, search domain, credentials).

The rules are documented in docs/architecture.md, section 5. Summary:

* hostname: first non-empty value walking up the parent-connection chain.
* port: own value, else protocol default (ports are protocol specific and do
  not inherit).
* search domain: own → ancestor connections → folders (bottom-up) → env.
* credentials, per field: interactive input (at the position of the
  interactive connection in the chain) → the connection's own values for
  its ``auth_mode`` → the parent's effective value if ``inherit_auth``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.errors import NotFound, ValidationFailed
from app.security.crypto import SecretBox, aad_for

MAX_CHAIN = 32
AUTH_FIELDS = ("username", "password", "domain", "private_key", "passphrase")
SECRET_AUTH_FIELDS = ("password", "private_key", "passphrase")
LAUNCHABLE = ("ssh", "sftp", "rdp", "vnc", "ftp", "ftps")


def default_port(protocol: str, settings: dict[str, Any] | None = None) -> int | None:
    settings = settings or {}
    if protocol in ("ssh", "sftp"):
        return 22
    if protocol == "rdp":
        return 3389
    if protocol == "vnc":
        display = settings.get("display") or 0
        return 5900 + int(display)
    if protocol == "ftp":
        return 21
    if protocol == "ftps":
        return 990 if settings.get("tls_mode") == "implicit" else 21
    return None


@dataclass(frozen=True)
class Source:
    kind: str  # connection | credential | interactive | folder | environment | default
    id: str | None = None
    name: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "name": self.name}


@dataclass
class Resolved:
    value: Any
    source: Source
    inherited: bool = False
    present: bool = True

    def public(self, secret: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {"source": self.source.as_dict(), "inherited": self.inherited}
        if secret:
            out["set"] = self.present
        else:
            out["value"] = self.value
        return out


@dataclass
class EffectiveConnection:
    connection: m.Connection
    chain: list[m.Connection]
    hostname: Resolved | None
    port: Resolved | None
    search_domains: Resolved | None
    auth: dict[str, Resolved] = field(default_factory=dict)
    interactive_connection_id: uuid.UUID | None = None
    interactive_supplied: bool = False

    @property
    def protocol(self) -> str:
        return self.connection.protocol

    @property
    def settings(self) -> dict[str, Any]:
        return dict(self.connection.settings or {})

    @property
    def prompt_required(self) -> bool:
        return self.interactive_connection_id is not None and not self.interactive_supplied

    def value(self, name: str) -> Any:
        r = self.auth.get(name)
        return r.value if r else None

    def auth_sources(self) -> dict[str, Any]:
        return {k: v.source.as_dict() for k, v in self.auth.items()}

    def public(self) -> dict[str, Any]:
        domains = self.search_domains.value if self.search_domains else []
        host = self.hostname.value if self.hostname else None
        return {
            "connection_id": str(self.connection.id),
            "protocol": self.protocol,
            "launchable": self.protocol in LAUNCHABLE and host is not None,
            "hostname": self.hostname.public() if self.hostname else None,
            "port": self.port.public() if self.port else None,
            "search_domains": self.search_domains.public() if self.search_domains else None,
            "hostname_candidates": hostname_candidates(host, domains) if host else [],
            "auth_mode": self.connection.auth_mode,
            "auth": {k: v.public(secret=k in SECRET_AUTH_FIELDS) for k, v in self.auth.items()},
            "prompt_required": self.prompt_required,
            "interactive_connection_id": (
                str(self.interactive_connection_id) if self.interactive_connection_id else None
            ),
            "chain": [{"id": str(c.id), "name": c.name, "protocol": c.protocol} for c in self.chain],
        }


# --------------------------------------------------------------------------- hostname helpers


def is_qualified(host: str) -> bool:
    """True for FQDNs, names with a trailing dot, IPv4 and IPv6 literals."""
    import ipaddress

    h = host.strip()
    if h.endswith(".") or "." in h:
        return True
    try:
        ipaddress.ip_address(h.strip("[]"))
        return True
    except ValueError:
        return ":" in h


def hostname_candidates(host: str, domains: list[str]) -> list[str]:
    if is_qualified(host) or not domains:
        return [host]
    return [f"{host}.{d.strip('.')}" for d in domains if d.strip(".")] + [host]


# --------------------------------------------------------------------------- loaders


async def load_connection_chain(db: AsyncSession, connection_id: uuid.UUID) -> list[m.Connection]:
    chain: list[m.Connection] = []
    seen: set[uuid.UUID] = set()
    current: uuid.UUID | None = connection_id
    while current is not None:
        if current in seen or len(chain) >= MAX_CHAIN:
            raise ValidationFailed("connection hierarchy contains a cycle or is too deep")
        seen.add(current)
        conn = await db.get(m.Connection, current)
        if conn is None:
            if not chain:
                raise NotFound("connection not found")
            break
        chain.append(conn)
        current = conn.parent_id
    return chain


async def load_folder_chain(db: AsyncSession, folder_id: uuid.UUID | None) -> list[m.Folder]:
    chain: list[m.Folder] = []
    seen: set[uuid.UUID] = set()
    while folder_id is not None and folder_id not in seen and len(chain) < MAX_CHAIN:
        seen.add(folder_id)
        folder = await db.get(m.Folder, folder_id)
        if folder is None:
            break
        chain.append(folder)
        folder_id = folder.parent_id
    return chain


async def load_credential_chain(db: AsyncSession, credential_id: uuid.UUID | None) -> list[m.Credential]:
    chain: list[m.Credential] = []
    seen: set[uuid.UUID] = set()
    while credential_id is not None and credential_id not in seen and len(chain) < MAX_CHAIN:
        seen.add(credential_id)
        cred = await db.get(m.Credential, credential_id)
        if cred is None:
            break
        chain.append(cred)
        credential_id = cred.parent_id
    return chain


async def would_create_cycle(
    db: AsyncSession, model: type[m.Folder] | type[m.Connection] | type[m.Credential],
    item_id: uuid.UUID, new_parent_id: uuid.UUID | None,
) -> bool:
    current = new_parent_id
    depth = 0
    while current is not None and depth <= MAX_CHAIN:
        if current == item_id:
            return True
        current = (await db.execute(select(model.parent_id).where(model.id == current))).scalar_one_or_none()
        depth += 1
    return depth > MAX_CHAIN


# --------------------------------------------------------------------------- credential values


def credential_values(
    chain: list[m.Credential], box: SecretBox | None, *, include_secrets: bool
) -> dict[str, Resolved]:
    out: dict[str, Resolved] = {}
    for fname in AUTH_FIELDS:
        for idx, cred in enumerate(chain):
            raw = _credential_field(cred, fname, box, include_secrets)
            if raw is None:
                continue
            present, value = raw
            out[fname] = Resolved(
                value=value,
                source=Source("credential", str(cred.id), cred.name),
                inherited=idx > 0,
                present=present,
            )
            break
    return out


def _credential_field(
    cred: m.Credential, fname: str, box: SecretBox | None, include_secrets: bool
) -> tuple[bool, Any] | None:
    if fname in ("username", "domain"):
        v = getattr(cred, fname)
        return (True, v) if v else None
    blob = getattr(cred, f"{fname}_enc")
    if not blob:
        return None
    if not include_secrets or box is None:
        return (True, None)
    return (True, box.decrypt(blob, aad_for("credentials", fname, cred.id)))


def _connection_own(
    conn: m.Connection,
    credentials: dict[uuid.UUID, list[m.Credential]],
    box: SecretBox | None,
    include_secrets: bool,
) -> dict[str, Resolved]:
    src = Source("connection", str(conn.id), conn.name)
    vals: dict[str, Resolved] = {}
    if conn.auth_mode in ("direct", "interactive"):
        if conn.username:
            vals["username"] = Resolved(conn.username, src)
        if conn.domain:
            vals["domain"] = Resolved(conn.domain, src)
        if conn.auth_mode == "direct" and conn.password_enc:
            value = (
                box.decrypt(conn.password_enc, aad_for("connections", "password", conn.id))
                if include_secrets and box
                else None
            )
            vals["password"] = Resolved(value, src)
    elif conn.auth_mode == "credential" and conn.credential_id:
        vals.update(credential_values(credentials.get(conn.credential_id, []), box, include_secrets=include_secrets))
    return vals


# --------------------------------------------------------------------------- main entry


async def resolve_connection(
    db: AsyncSession,
    connection_id: uuid.UUID,
    *,
    box: SecretBox | None,
    include_secrets: bool,
    env_search_domains: list[str],
    interactive: dict[str, str] | None = None,
) -> EffectiveConnection:
    chain = await load_connection_chain(db, connection_id)
    credentials: dict[uuid.UUID, list[m.Credential]] = {}
    for conn in chain:
        if conn.auth_mode == "credential" and conn.credential_id and conn.credential_id not in credentials:
            credentials[conn.credential_id] = await load_credential_chain(db, conn.credential_id)
    top = chain[-1]
    folders = await load_folder_chain(db, top.folder_id)
    return compute_effective(
        chain,
        credentials,
        folders,
        box=box,
        include_secrets=include_secrets,
        env_search_domains=env_search_domains,
        interactive=interactive,
    )


def compute_effective(
    chain: list[m.Connection],
    credentials: dict[uuid.UUID, list[m.Credential]],
    folders: list[m.Folder],
    *,
    box: SecretBox | None,
    include_secrets: bool,
    env_search_domains: list[str],
    interactive: dict[str, str] | None = None,
) -> EffectiveConnection:
    conn = chain[0]

    # hostname -----------------------------------------------------------------
    hostname: Resolved | None = None
    for idx, c in enumerate(chain):
        if c.hostname:
            hostname = Resolved(c.hostname.strip(), Source("connection", str(c.id), c.name), inherited=idx > 0)
            break

    # port -----------------------------------------------------------------------
    port: Resolved | None
    if conn.port:
        port = Resolved(conn.port, Source("connection", str(conn.id), conn.name))
    else:
        dp = default_port(conn.protocol, conn.settings)
        port = Resolved(dp, Source("default", None, f"{conn.protocol} default")) if dp else None

    # search domain ---------------------------------------------------------------
    search: Resolved | None = None
    for idx, c in enumerate(chain):
        if c.search_domain:
            search = Resolved([c.search_domain.strip(".")], Source("connection", str(c.id), c.name), idx > 0)
            break
    if search is None:
        for f in folders:
            if f.search_domain:
                search = Resolved([f.search_domain.strip(".")], Source("folder", str(f.id), f.name), True)
                break
    if search is None and env_search_domains:
        search = Resolved(list(env_search_domains), Source("environment", None, "DNS_SEARCH_DOMAINS"), True)

    # credentials ---------------------------------------------------------------
    auth: dict[str, Resolved] = {}
    interactive_conn: uuid.UUID | None = None
    interactive = {k: v for k, v in (interactive or {}).items() if k in AUTH_FIELDS and v}
    for idx, c in enumerate(chain):
        own = _connection_own(c, credentials, box, include_secrets)
        if c.auth_mode == "interactive" and interactive_conn is None:
            interactive_conn = c.id
            for k, v in interactive.items():
                own[k] = Resolved(v, Source("interactive", str(c.id), "entered at launch"))
        for k, r in own.items():
            if k not in auth:
                r.inherited = r.inherited or idx > 0
                auth[k] = r
        if not c.inherit_auth:
            break

    return EffectiveConnection(
        connection=conn,
        chain=chain,
        hostname=hostname,
        port=port,
        search_domains=search,
        auth=auth,
        interactive_connection_id=interactive_conn,
        interactive_supplied=bool(interactive),
    )
