"""Resolution of effective connection values (hostname, port, search domain, credentials).

The rules are documented in docs/architecture.md, section 5. Summary:

* hostname: first non-empty value walking up the parent-connection chain.
* port: own value, else protocol default (ports are protocol specific and do
  not inherit).
* search domain: own → ancestor connections → folders (bottom-up) → env.
* credentials, per field: interactive input (at the position of the
  interactive connection in the chain) → the connection's own values for
  its ``auth_mode`` → the parent's effective value if ``inherit_auth``.
* lookups (``connections.lookups``, see services/lookups.py) take the place of the
  connection's own value for that field; their placeholders refer to the opened
  connection. A failing lookup leaves the field empty and is reported in
  ``lookup_errors`` (launching refuses then).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.errors import NotFound, ValidationFailed
from app.security.crypto import SecretBox, aad_for
from app.services.credential_attributes import get_attributes, get_secret_attributes
from app.services.lookups import LookupFailed, find_credential, parse_lookup, substitute

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
class AuthOverride:
    """"Open with different credentials": replaces the connection's configured authentication for
    one session. Either typed values (``values``, never stored) or a credential-store entry."""

    values: dict[str, str] | None = None
    credential_id: uuid.UUID | None = None


@dataclass(frozen=True)
class Source:
    kind: str  # connection | credential | interactive | folder | environment | default | lookup
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
    # auth_mode "credential" without a pinned credential: the user picks one at connect time
    credential_choice_connection_id: uuid.UUID | None = None
    chosen_credential_id: uuid.UUID | None = None
    # …limited to this credential folder (and below), if the connection says so
    credential_choice_folder_id: uuid.UUID | None = None
    description: Resolved | None = None
    # field -> why its lookup failed (only lookups that decide an effective value)
    lookup_errors: dict[str, str] = field(default_factory=dict)

    @property
    def credential_choice_required(self) -> bool:
        return self.credential_choice_connection_id is not None and self.chosen_credential_id is None

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
            "credential_choice_required": self.credential_choice_connection_id is not None,
            "credential_choice_folder_id": (
                str(self.credential_choice_folder_id) if self.credential_choice_folder_id else None
            ),
            "description": self.description.public() if self.description else None,
            "lookup_errors": dict(self.lookup_errors),
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
    db: AsyncSession,
    model: type[m.Folder] | type[m.Connection] | type[m.Credential] | type[m.CredentialFolder],
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
    chosen_credential_id: uuid.UUID | None = None,
    lookups: dict[tuple[uuid.UUID, str], Resolved] | None = None,
    lookup_errors: dict[tuple[uuid.UUID, str], str] | None = None,
    errors_out: dict[str, str] | None = None,
) -> dict[str, Resolved]:
    src = Source("connection", str(conn.id), conn.name)
    vals: dict[str, Resolved] = {}
    if conn.auth_mode in ("direct", "interactive"):
        # connection-specific values may come from the credential store through lookups
        for fname in AUTH_FIELDS:
            key = (conn.id, fname)
            if lookups and key in lookups:
                vals[fname] = lookups[key]
            elif lookup_errors and key in lookup_errors and errors_out is not None:
                errors_out.setdefault(fname, lookup_errors[key])
        if conn.username and "username" not in vals:
            vals["username"] = Resolved(conn.username, src)
        if conn.domain and "domain" not in vals:
            vals["domain"] = Resolved(conn.domain, src)
        if conn.auth_mode == "direct" and conn.password_enc and "password" not in vals:
            value = (
                box.decrypt(conn.password_enc, aad_for("connections", "password", conn.id))
                if include_secrets and box
                else None
            )
            vals["password"] = Resolved(value, src)
    elif conn.auth_mode == "credential":
        # pinned credential, or (when none is pinned) the one the user chose at connect time
        cred_id = conn.credential_id or chosen_credential_id
        if cred_id:
            vals.update(credential_values(credentials.get(cred_id, []), box, include_secrets=include_secrets))
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
    chosen_credential_id: uuid.UUID | None = None,
    auth_override: AuthOverride | None = None,
    credential_filter: Callable[[uuid.UUID], bool] | None = None,
) -> EffectiveConnection:
    """``credential_filter``: which credentials lookups may use (e.g. those the user may connect with);
    None = all."""
    chain = await load_connection_chain(db, connection_id)
    credentials: dict[uuid.UUID, list[m.Credential]] = {}
    for conn in chain:
        if conn.auth_mode == "credential" and conn.credential_id and conn.credential_id not in credentials:
            credentials[conn.credential_id] = await load_credential_chain(db, conn.credential_id)
    for extra in (chosen_credential_id, auth_override.credential_id if auth_override else None):
        if extra and extra not in credentials:
            credentials[extra] = await load_credential_chain(db, extra)
    top = chain[-1]
    folders = await load_folder_chain(db, top.folder_id)
    lookups, lookup_errors = await resolve_lookups(
        db, chain, folders, env_search_domains, box=box, include_secrets=include_secrets,
        credential_filter=credential_filter,
    )
    return compute_effective(
        chain,
        credentials,
        folders,
        box=box,
        include_secrets=include_secrets,
        env_search_domains=env_search_domains,
        interactive=interactive,
        chosen_credential_id=chosen_credential_id,
        auth_override=auth_override,
        lookups=lookups,
        lookup_errors=lookup_errors,
    )


def _literal_search_domain(chain: list[m.Connection], folders: list[m.Folder], env: list[str]) -> str | None:
    for c in chain:
        if c.search_domain:
            return c.search_domain.strip(".")
    for f in folders:
        if f.search_domain:
            return f.search_domain.strip(".")
    return env[0].strip(".") if env else None


def credential_lookup_value(creds: list[m.Credential], fname: str, box: SecretBox | None,
                            include_secrets: bool) -> Any:
    """``fname`` of the credential (chain): a login field, an attribute, or a secret attribute.
    Secrets (fields and secret attributes) are only decrypted with ``include_secrets`` (sessions);
    otherwise their value is None."""
    name = creds[0].name
    if fname in AUTH_FIELDS:
        r = credential_values(creds, box, include_secrets=include_secrets).get(fname)
        if r is None:
            raise LookupFailed(f"credential '{name}' has no {fname}")
        return r.value
    for cred in creds:
        v = get_attributes(cred, box).get(fname)
        if v not in (None, ""):
            return str(v)
    for cred in creds:
        if cred.secret_attributes_enc is None:
            continue
        if not include_secrets:
            if fname in get_secret_attributes(cred, box):
                return None  # set, but secret: only a session reads it
            continue
        v = get_secret_attributes(cred, box).get(fname)
        if v not in (None, ""):
            return str(v)
    raise LookupFailed(f"credential '{name}' has no attribute '{fname}'")


async def resolve_lookups(
    db: AsyncSession,
    chain: list[m.Connection],
    folders: list[m.Folder],
    env_search_domains: list[str],
    *,
    box: SecretBox | None,
    include_secrets: bool,
    credential_filter: Callable[[uuid.UUID], bool] | None,
) -> tuple[dict[tuple[uuid.UUID, str], Resolved], dict[tuple[uuid.UUID, str], str]]:
    """Every lookup in the chain: {(connection id, field): value} and {(connection id, field): error}."""
    if not any(c.lookups for c in chain):
        return {}, {}
    rows = (await db.execute(select(m.Credential.id, m.Credential.name))).all()
    candidates = [(r.id, r.name) for r in rows if credential_filter is None or credential_filter(r.id)]
    creds_cache: dict[uuid.UUID, list[m.Credential]] = {}
    ctx: dict[str, str | None] = {
        "name": chain[0].name,
        "hostname": None,
        "domain": _literal_search_domain(chain, folders, env_search_domains),
    }
    results: dict[tuple[uuid.UUID, str], Resolved] = {}
    errors: dict[tuple[uuid.UUID, str], str] = {}

    async def one(c: m.Connection, fname: str, expr: str) -> None:
        try:
            lk = parse_lookup(expr)
            if lk is None:
                raise LookupFailed(f"'{expr}' is not a valid lookup")
            cid = find_credential(substitute(lk.pattern, ctx), candidates)
            if not isinstance(cid, uuid.UUID):
                raise LookupFailed("unexpected credential id")
            if cid not in creds_cache:
                creds_cache[cid] = await load_credential_chain(db, cid)
            creds = creds_cache[cid]
            value = credential_lookup_value(creds, lk.field, box, include_secrets)
            results[(c.id, fname)] = Resolved(value, Source("lookup", str(cid), creds[0].name))
        except LookupFailed as exc:
            errors[(c.id, fname)] = str(exc)

    # 1. hostname lookups (they cannot use {hostname}), then the hostname placeholder
    for c in chain:
        expr = (c.lookups or {}).get("hostname")
        if expr:
            await one(c, "hostname", expr)
    for c in chain:
        r = results.get((c.id, "hostname"))
        if r is not None:
            ctx["hostname"] = str(r.value).strip()
            break
        if (c.id, "hostname") in errors:
            break
        if c.hostname:
            ctx["hostname"] = c.hostname.strip()
            break
    # 2. everything else
    for c in chain:
        for fname, expr in (c.lookups or {}).items():
            if fname != "hostname":
                await one(c, fname, expr)
    return results, errors


def compute_effective(
    chain: list[m.Connection],
    credentials: dict[uuid.UUID, list[m.Credential]],
    folders: list[m.Folder],
    *,
    box: SecretBox | None,
    include_secrets: bool,
    env_search_domains: list[str],
    interactive: dict[str, str] | None = None,
    chosen_credential_id: uuid.UUID | None = None,
    auth_override: AuthOverride | None = None,
    lookups: dict[tuple[uuid.UUID, str], Resolved] | None = None,
    lookup_errors: dict[tuple[uuid.UUID, str], str] | None = None,
) -> EffectiveConnection:
    conn = chain[0]
    lookups = lookups or {}
    lookup_errors = lookup_errors or {}
    errors: dict[str, str] = {}

    def from_lookup(c: m.Connection, fname: str) -> Resolved | None | bool:
        """The lookup value of c.fname; False if its lookup failed (error recorded); None without lookup."""
        key = (c.id, fname)
        if key in lookups:
            return lookups[key]
        if key in lookup_errors:
            errors.setdefault(fname, lookup_errors[key])
            return False
        return None

    # hostname -----------------------------------------------------------------
    hostname: Resolved | None = None
    for idx, c in enumerate(chain):
        lk = from_lookup(c, "hostname")
        if lk is False:
            break
        if isinstance(lk, Resolved):
            hostname = Resolved(str(lk.value).strip(), lk.source, inherited=idx > 0)
            break
        if c.hostname:
            hostname = Resolved(c.hostname.strip(), Source("connection", str(c.id), c.name), inherited=idx > 0)
            break

    # port -----------------------------------------------------------------------
    port: Resolved | None = None
    lk_port = from_lookup(conn, "port")
    if isinstance(lk_port, Resolved) and lk_port.value is None:
        port = Resolved(None, lk_port.source)  # from a secret attribute: known only to sessions
    elif isinstance(lk_port, Resolved):
        try:
            port_value = int(str(lk_port.value).strip())
            if not 1 <= port_value <= 65535:
                raise ValueError
            port = Resolved(port_value, lk_port.source)
        except ValueError:
            errors["port"] = f"the lookup returned '{lk_port.value}', which is not a port number"
    elif lk_port is None and conn.port:
        port = Resolved(conn.port, Source("connection", str(conn.id), conn.name))
    if port is None and "port" not in errors:
        dp = default_port(conn.protocol, conn.settings)
        port = Resolved(dp, Source("default", None, f"{conn.protocol} default")) if dp else None

    # description (own value only) -------------------------------------------------
    description: Resolved | None = None
    lk_desc = from_lookup(conn, "description")
    if isinstance(lk_desc, Resolved):
        description = lk_desc
    elif lk_desc is None and conn.description:
        description = Resolved(conn.description, Source("connection", str(conn.id), conn.name))

    # search domain ---------------------------------------------------------------
    search: Resolved | None = None
    search_failed = False
    for idx, c in enumerate(chain):
        lk = from_lookup(c, "search_domain")
        if lk is False:
            search_failed = True
            break
        if isinstance(lk, Resolved):
            search = Resolved([str(lk.value).strip(".")], lk.source, idx > 0)
            break
        if c.search_domain:
            search = Resolved([c.search_domain.strip(".")], Source("connection", str(c.id), c.name), idx > 0)
            break
    if search is None and not search_failed:
        for f in folders:
            if f.search_domain:
                search = Resolved([f.search_domain.strip(".")], Source("folder", str(f.id), f.name), True)
                break
    if search is None and not search_failed and env_search_domains:
        search = Resolved(list(env_search_domains), Source("environment", None, "DNS_SEARCH_DOMAINS"), True)

    # credentials ---------------------------------------------------------------
    auth: dict[str, Resolved] = {}
    interactive_conn: uuid.UUID | None = None
    choice_conn: uuid.UUID | None = None
    choice_folder: uuid.UUID | None = None
    auth_errors: dict[str, str] = {}
    interactive = {k: v for k, v in (interactive or {}).items() if k in AUTH_FIELDS and v}
    for idx, c in enumerate(chain):
        choose_here = c.auth_mode == "credential" and not c.credential_id and choice_conn is None
        if choose_here:
            choice_conn = c.id
            choice_folder = c.credential_folder_id
        own = _connection_own(c, credentials, box, include_secrets,
                              chosen_credential_id if choose_here else None,
                              lookups, lookup_errors, auth_errors)
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

    if auth_override is not None:
        # Different credentials for this session only: nothing from the configured authentication is
        # mixed in (no stored password next to a typed username), and nothing is prompted.
        if auth_override.credential_id:
            auth = credential_values(credentials.get(auth_override.credential_id, []), box,
                                     include_secrets=include_secrets)
        else:
            src = Source("interactive", str(conn.id), "entered at launch")
            auth = {k: Resolved(v, src) for k, v in (auth_override.values or {}).items() if k in AUTH_FIELDS and v}
        interactive_conn = None
        choice_conn = None
        auth_errors = {}
    # a failed auth lookup matters only if nothing else supplied the field
    errors.update({k: v for k, v in auth_errors.items() if k not in auth})

    return EffectiveConnection(
        connection=conn,
        chain=chain,
        hostname=hostname,
        port=port,
        search_domains=search,
        auth=auth,
        interactive_connection_id=interactive_conn,
        interactive_supplied=bool(interactive),
        credential_choice_connection_id=choice_conn,
        chosen_credential_id=chosen_credential_id if choice_conn else None,
        credential_choice_folder_id=choice_folder if choice_conn else None,
        description=description,
        lookup_errors=errors,
    )
