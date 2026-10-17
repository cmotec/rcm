"""Automatic import: every secret of a provider becomes a credential entry.

Enabled per provider (``auto_import`` in its configuration, OpenBao only). Before each full sync the
provider is walked from ``import_path``:

* every secret (a KV entry, not a folder) is one credential, named after the secret (last path
  segment);
* its keys are matched to credential fields by name (``username``/``user``/``login``,
  ``password``/``pass``/``pwd``, ``domain``, ``private_key``/``ssh_key``, ``passphrase`` and a few
  more; case, ``-``/``_`` and spaces are ignored). A secret with a single key that matches nothing,
  e.g. a key without a name, is taken as the password;
* every other key becomes a credential attribute (``attr:<key>``, encrypted, shown to viewers,
  readable by lookups such as ``{{lookup:{hostname}_root:port}}``); keys whose name looks secret
  (token, secret, key, certificate, …) become write-only secret attributes (``secret:<key>``);
* the matching keys become secret mappings, and the normal sync then fills in the values;
* a secret without a username key: the secret's name becomes the credential's username (replaced by a
  username key added later);
* excluded secrets (provider setting ``exclude``: one pattern per line, ``*`` = any text, matched
  case-insensitively against the path below the mount or the secret's name) are left out completely:
  credentials the import created for them are deleted when nothing uses them, otherwise only their
  link to the provider is removed;
* folders: the secret's path below ``import_path`` is mirrored as credential folders below the
  provider's ``import_folder_id`` (or at the top level).

Already linked secrets only get mappings for keys added since. A credential with the secret's name that
is not linked to this provider yet is linked instead of creating a duplicate; local values that differ
then become sync conflicts (never overwritten silently).

Removed secrets: a credential the import manages (created or linked by it, see ``_managed_by``) whose
secret is no longer in the provider (deleted, or a KV v2 soft delete) is deleted together with its
mappings. It is kept, with its mappings showing ``remote_missing``, while connections or other
credentials still use it or other secrets still feed it. Nothing is deleted after an incomplete walk
(a listing error fails the whole sync; hitting ``MAX_SECRETS`` skips this step). Credentials that are
only mapped by hand are never deleted.
"""

from __future__ import annotations

import re
import uuid
from collections import deque
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.logging import get_logger
from app.services import audit
from app.services.credential_folders import ensure_folder_path
from app.services.lookups import name_matcher
from app.services.secrets.base import SecretProviderClient

log = get_logger(__name__)

MAX_DEPTH = 10
MAX_SECRETS = 2000

FIELD_ALIASES: dict[str, set[str]] = {
    "username": {"username", "user", "login", "loginname", "userid", "account", "benutzer", "benutzername"},
    "password": {"password", "pass", "passwd", "pwd", "kennwort", "passwort", "secret"},
    "domain": {"domain", "domaene", "domäne", "ntdomain", "userdomain"},
    "private_key": {"privatekey", "sshkey", "sshprivatekey", "key", "idrsa", "ided25519"},
    "passphrase": {"passphrase", "keypassphrase", "privatekeypassphrase", "sshpassphrase"},
}
_NORMALISE = re.compile(r"[\s_\-.]+")


def field_for_key(key: str) -> str | None:
    """The credential field a secret key stands for, or None."""
    k = _NORMALISE.sub("", key.strip().lower())
    for field, aliases in FIELD_ALIASES.items():
        if k in aliases:
            return field
    return None


def fields_for_keys(keys: list[str]) -> dict[str, str]:
    """{field: key} for the keys of one secret."""
    out: dict[str, str] = {}
    for key in keys:
        field = field_for_key(key)
        if field and field not in out:
            out[field] = key
    if not out and len(keys) == 1:
        out["password"] = keys[0]  # a lone value (often a key without a name) is the password
    return out


# Key names that probably hold a secret: imported as write-only secret attributes.
_SENSITIVE = re.compile(r"(token|secret|key|pass|pwd|pin|cert|private|credential|auth|otp|seed|salt|hash)",
                        re.IGNORECASE)
_ATTR_NAME = re.compile(r"[^A-Za-z0-9_.\-]+")


def attribute_fields(keys: list[str], used: set[str]) -> dict[str, str]:
    """{"attr:<name>" or "secret:<name>": key} for the keys not used for a credential field."""
    out: dict[str, str] = {}
    for key in keys:
        if key in used or not key.strip():
            continue
        name = _ATTR_NAME.sub("_", key.strip()).strip("_")[:59]
        if not name:
            continue
        field = f"secret:{name}" if _SENSITIVE.search(key) else f"attr:{name}"
        if field not in out:
            out[field] = key
    return out


USERNAME_FROM_NAME = "_username_from_secret_name"


async def _username_from_secret_name(cred: m.Credential, secret_name: str, has_username_key: bool,
                                     username_mapped: bool, stats: dict[str, int]) -> None:
    """No username key in the secret: the secret's name is the username. If the secret gets a username
    key later, that key takes over (the name-derived value is cleared, so the first sync applies it
    instead of reporting a conflict)."""
    meta = dict(cred.meta or {})
    if has_username_key or username_mapped:
        if meta.pop(USERNAME_FROM_NAME, None) and not username_mapped and cred.username == secret_name:
            cred.username = None
            cred.meta = meta
        return
    if not cred.username:
        cred.username = secret_name[:255]
        cred.version += 1
        meta[USERNAME_FROM_NAME] = True
        cred.meta = meta
        stats["usernames_from_name"] = stats.get("usernames_from_name", 0) + 1


def exclusion_matcher(patterns: list[str] | str | None):  # noqa: ANN201
    """A predicate for secret paths: True if a pattern matches the path or the secret's name."""
    lines = patterns.splitlines() if isinstance(patterns, str) else list(patterns or [])
    rxs = [name_matcher(p.strip().strip("/")) for p in lines if p.strip() and not p.strip().startswith("#")]

    def excluded(path: str) -> bool:
        name = path.rsplit("/", 1)[-1]
        return any(rx.match(path) or rx.match(name) for rx in rxs)

    return excluded


def _managed_by(cred: m.Credential, provider: m.SecretProvider, path: str) -> bool:
    """Created (or linked) by this provider's import for this secret."""
    meta = cred.meta or {}
    return meta.get("imported_from") == provider.name and _norm(str(meta.get("external_path", ""))) == path


async def _used_by(db: AsyncSession, cred_id: uuid.UUID) -> tuple[int, int]:
    """(connections and folders, credentials) that reference the credential."""
    conns = (await db.execute(select(func.count()).select_from(m.Connection)
                              .where(m.Connection.credential_id == cred_id))).scalar_one() \
        + (await db.execute(select(func.count()).select_from(m.Folder)
                            .where(m.Folder.credential_id == cred_id))).scalar_one()
    children = (await db.execute(select(func.count()).select_from(m.Credential)
                                 .where(m.Credential.parent_id == cred_id))).scalar_one()
    return conns, children


async def _drop_excluded(db: AsyncSession, provider: m.SecretProvider, path: str,
                         mappings: list[m.SecretMapping], stats: dict[str, int]) -> None:
    """An excluded secret: remove its mappings; delete the credential the import created for it if unused."""
    linked = [mp for mp in mappings if _norm(str((mp.external_ref or {}).get("path", ""))) == path]
    for cred_id in {mp.credential_id for mp in linked}:
        for mp in linked:
            if mp.credential_id == cred_id:
                await db.delete(mp)
        await db.flush()
        cred = await db.get(m.Credential, cred_id)
        if cred is None:
            continue
        created_here = _managed_by(cred, provider, path)
        in_use = any(await _used_by(db, cred_id)) \
            or (await db.execute(select(func.count()).select_from(m.SecretMapping)
                                 .where(m.SecretMapping.credential_id == cred_id))).scalar_one()
        if created_here and not in_use:
            await db.delete(cred)
            stats["removed"] = stats.get("removed", 0) + 1
        else:
            stats["unlinked"] = stats.get("unlinked", 0) + 1
    await db.flush()


async def _drop_removed(db: AsyncSession, provider: m.SecretProvider, path: str,
                        mappings: list[m.SecretMapping], stats: dict[str, int], actor_id: uuid.UUID | None) -> None:
    """The secret at ``path`` is gone from the provider: delete the credential the import manages for
    it, unless something still uses it (then its mappings stay and report ``remote_missing``)."""
    linked = [mp for mp in mappings if _norm(str((mp.external_ref or {}).get("path", ""))) == path]
    for cred_id in {mp.credential_id for mp in linked}:
        cred = await db.get(m.Credential, cred_id)
        if cred is None or not _managed_by(cred, provider, path):
            continue  # mapped by hand: only the mapping status shows the secret is missing
        conns, children = await _used_by(db, cred_id)
        other_sources = (await db.execute(select(func.count()).select_from(m.SecretMapping).where(
            m.SecretMapping.credential_id == cred_id,
            m.SecretMapping.id.not_in([mp.id for mp in linked])))).scalar_one()
        if conns or children or other_sources:
            stats["kept_in_use"] = stats.get("kept_in_use", 0) + 1
            log.info("secret_removed_credential_kept", provider=provider.name, path=path, credential_id=str(cred_id),
                     connections=conns, credentials=children, other_mappings=other_sources)
            continue
        for mp in linked:
            if mp.credential_id == cred_id:
                await db.delete(mp)
        await db.flush()
        name = cred.name
        await db.delete(cred)
        await db.flush()
        stats["deleted_gone"] = stats.get("deleted_gone", 0) + 1
        await audit.record(db, "credential.delete", actor_id=actor_id, resource_type="credential", resource_id=cred_id,
                           details={"name": name, "reason": "secret removed from the provider",
                                    "provider": provider.name, "path": path})


def _under(path: str, root: str) -> bool:
    return not root or path == root or path.startswith(root + "/")


def _norm(path: str) -> str:
    return path.strip().strip("/")


async def _walk(client: SecretProviderClient, root: str) -> list[tuple[str, list[str]]]:
    """All secrets below ``root`` with their key names (values are not read)."""
    found: list[tuple[str, list[str]]] = []
    queue: deque[tuple[str, int]] = deque([(_norm(root), 0)])
    seen: set[str] = set()
    while queue and len(found) < MAX_SECRETS:
        path, depth = queue.popleft()
        if path in seen:
            continue
        seen.add(path)
        items = await client.browse(path or "/")
        for item in items:
            if item.key is not None:
                continue
            if item.is_folder:
                if depth < MAX_DEPTH:
                    queue.append((_norm(item.path), depth + 1))
                continue
            secret_path = _norm(item.path)
            keys = [i.key for i in await client.browse(secret_path) if i.key is not None and _norm(i.path) == secret_path]
            if not keys and await client.is_deleted(secret_path):
                continue  # still listed, but deleted (KV v2 keeps the metadata)
            found.append((secret_path, keys))
            if len(found) >= MAX_SECRETS:
                log.warning("secret_discovery_limit", limit=MAX_SECRETS)
                break
    return found


async def discover(db: AsyncSession, client: SecretProviderClient, provider: m.SecretProvider,
                   *, actor_id: uuid.UUID | None) -> dict[str, int]:
    """Create or link credentials for the provider's secrets; returns counters for the sync run."""
    config: dict[str, Any] = provider.config or {}
    stats = {"discovered": 0, "created": 0, "linked": 0, "keys_added": 0, "skipped": 0, "excluded": 0}
    import_path = _norm(str(config.get("import_path") or ""))
    excluded = exclusion_matcher(config.get("exclude"))
    secrets = await _walk(client, import_path)
    target_folder: uuid.UUID | None = None
    if config.get("import_folder_id"):
        try:
            target_folder = uuid.UUID(str(config["import_folder_id"]))
        except ValueError:
            target_folder = None
        if target_folder and await db.get(m.CredentialFolder, target_folder) is None:
            target_folder = None  # folder deleted meanwhile: import at the top level
    stats["discovered"] = len(secrets)

    mappings = (await db.execute(select(m.SecretMapping).where(m.SecretMapping.provider_id == provider.id))).scalars().all()
    by_path: dict[str, uuid.UUID] = {}
    for mp in mappings:
        by_path.setdefault(_norm(str((mp.external_ref or {}).get("path", ""))), mp.credential_id)
    linked_creds = {mp.credential_id for mp in mappings}

    for path, keys in secrets:
        if excluded(path):
            stats["excluded"] += 1
            await _drop_excluded(db, provider, path, list(mappings), stats)
            by_path.pop(path, None)
            continue
        fields = fields_for_keys(keys)
        if not fields:
            stats["skipped"] += 1
            continue
        fields.update(attribute_fields(keys, set(fields.values())))
        cred_id = by_path.get(path)
        name = path.rsplit("/", 1)[-1] or path
        if cred_id is None:
            existing = (await db.execute(
                select(m.Credential).where(m.Credential.name == name).order_by(m.Credential.created_at)
            )).scalars().all()
            match = next((c for c in existing if c.id not in linked_creds), None)
            if match is not None:
                cred_id = match.id
                # from now on the import manages it (also: deleted when the secret is removed)
                match.meta = {**(match.meta or {}), "imported_from": provider.name, "external_path": path,
                              "import_linked": True}
                stats["linked"] += 1
            else:
                rel = path[len(import_path):].strip("/") if import_path and path.startswith(import_path) else path
                folder_id = await ensure_folder_path(db, target_folder, rel.split("/")[:-1], actor_id)
                cred = m.Credential(id=uuid.uuid4(), name=name[:255], owner_id=actor_id, folder_id=folder_id,
                                    description=f"Imported from {provider.name}: {path}",
                                    meta={"imported_from": provider.name, "external_path": path})
                db.add(cred)
                await db.flush()
                cred_id = cred.id
                stats["created"] += 1
            by_path[path] = cred_id
            linked_creds.add(cred_id)
        mapped_fields = set((await db.execute(
            select(m.SecretMapping.field).where(m.SecretMapping.credential_id == cred_id)
        )).scalars())
        cred = await db.get(m.Credential, cred_id)
        if cred is not None:
            await _username_from_secret_name(cred, name, "username" in fields, "username" in mapped_fields, stats)
        for field, key in fields.items():
            if field in mapped_fields:
                continue  # one source per field; an existing mapping (maybe another provider) stays
            db.add(m.SecretMapping(credential_id=cred_id, provider_id=provider.id, field=field,
                                   external_ref={"path": path, "key": key}))
            stats["keys_added"] += 1
    await db.flush()

    # secrets that are gone (below the import path): delete the credentials the import manages for them
    if len(secrets) < MAX_SECRETS:
        present = {path for path, _ in secrets}
        for path in sorted(p for p in set(by_path) - present if _under(p, import_path)):
            await _drop_removed(db, provider, path, list(mappings), stats, actor_id)
    else:
        log.warning("secret_discovery_incomplete_no_removal", provider=provider.name, limit=MAX_SECRETS)
    return stats
