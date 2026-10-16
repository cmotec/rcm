"""Lookups: connection fields that take their value from the credential store (no database)."""

from __future__ import annotations

import asyncio
import base64
import os
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app import models as m
from app.security.crypto import CryptoError, Keyring, SecretBox, aad_for
from app.services.credential_attributes import (
    get_attributes,
    get_secret_attributes,
    secret_attribute_names,
    set_attributes,
    set_secret_attributes,
)
from app.services.lookups import (
    LookupFailed,
    find_credential,
    name_matcher,
    parse_lookup,
    substitute,
    validate_lookups,
)
from app.services.resolution import compute_effective, credential_lookup_value, resolve_lookups
from app.services.secrets.discovery import attribute_fields

BOX = SecretBox(Keyring.parse("k1:" + base64.b64encode(os.urandom(32)).decode()))


# --------------------------------------------------------------------------- expressions


def test_parse_and_validate() -> None:
    lk = parse_lookup("{{lookup:{hostname}_root:username}}")
    assert lk is not None and lk.pattern == "{hostname}_root" and lk.field == "username"
    assert parse_lookup(" {{ lookup:a:b:port }} ").pattern == "a:b"  # type: ignore[union-attr]
    assert parse_lookup("root") is None and parse_lookup(None) is None
    assert validate_lookups({"username": "{{lookup:{hostname}_root:username}}"}) == {
        "username": "{{lookup:{hostname}_root:username}}"}
    with pytest.raises(ValueError, match="cannot use a lookup"):
        validate_lookups({"tags": "{{lookup:x:username}}"})
    with pytest.raises(ValueError, match="not a lookup"):
        validate_lookups({"username": "root"})
    with pytest.raises(ValueError, match="unknown placeholder"):
        validate_lookups({"username": "{{lookup:{host}_root:username}}"})
    with pytest.raises(ValueError, match="itself"):
        validate_lookups({"hostname": "{{lookup:{hostname}:hostname}}"})


def test_placeholders_and_matching() -> None:
    assert substitute("{hostname}_root", {"hostname": "web01"}) == "web01_root"
    with pytest.raises(LookupFailed, match="no value"):
        substitute("{domain}_x", {"domain": None})
    rx = name_matcher("web*_root")
    assert rx.match("WEB01_root") and rx.match("web_root") and not rx.match("web01_root_old")
    assert name_matcher("a_b").match("a_b") and not name_matcher("a_b").match("aXb")  # _ is literal
    a, b = uuid.uuid4(), uuid.uuid4()
    cands = [(a, "web01_root"), (b, "web02_root")]
    assert find_credential("web01_root", cands) == a
    with pytest.raises(LookupFailed, match="matches 2 credentials"):
        find_credential("web*_root", cands)
    with pytest.raises(LookupFailed, match="no credential"):
        find_credential("db01_root", cands)


# --------------------------------------------------------------------------- resolution


def _conn(name: str, **kw: Any) -> m.Connection:
    return m.Connection(id=uuid.uuid4(), name=name, protocol=kw.pop("protocol", "ssh"),
                        auth_mode=kw.pop("auth_mode", "direct"), inherit_auth=True, settings={}, **kw)


def _cred(name: str, **kw: Any) -> m.Credential:
    c = m.Credential(id=uuid.uuid4(), name=name, username=kw.pop("username", None))
    set_attributes(c, kw.pop("attributes", {}), BOX)
    set_secret_attributes(c, kw.pop("secret_attributes", {}), BOX)
    if "password" in kw:
        c.password_enc = BOX.encrypt(kw.pop("password"), aad_for("credentials", "password", c.id))
    return c


class _FakeDb:
    """Just enough of AsyncSession for resolve_lookups."""

    def __init__(self, creds: list[m.Credential]) -> None:
        self.creds = {c.id: c for c in creds}

    async def execute(self, _stmt: Any) -> Any:
        rows = [SimpleNamespace(id=c.id, name=c.name) for c in self.creds.values()]
        return SimpleNamespace(all=lambda: rows)

    async def get(self, _model: Any, ident: uuid.UUID) -> Any:
        return self.creds.get(ident)


def _resolve(chain: list[m.Connection], creds: list[m.Credential], allowed: set[uuid.UUID] | None = None):  # noqa: ANN202
    lookups, errors = asyncio.run(resolve_lookups(
        _FakeDb(creds), chain, [], [], box=BOX, include_secrets=True,  # type: ignore[arg-type]
        credential_filter=(lambda cid: cid in allowed) if allowed is not None else None))
    return compute_effective(chain, {}, [], box=BOX, include_secrets=True, env_search_domains=[],
                             lookups=lookups, lookup_errors=errors)


def test_lookup_fills_login_and_connection_fields() -> None:
    root = _cred("web01_root", username="root", password="s3cret",
                 attributes={"port": "2222", "description": "Web server"})
    c = _conn("Web 01", hostname="web01", lookups={
        "username": "{{lookup:{hostname}_root:username}}",
        "password": "{{lookup:{hostname}_root:password}}",
        "port": "{{lookup:{hostname}_root:port}}",
        "description": "{{lookup:{hostname}_root:description}}",
    })
    e = _resolve([c], [root, _cred("web02_root", username="other")])
    assert e.lookup_errors == {}
    assert e.value("username") == "root" and e.value("password") == "s3cret"
    assert e.port is not None and e.port.value == 2222 and e.port.source.kind == "lookup"
    assert e.description is not None and e.description.value == "Web server"


def test_parent_lookup_uses_the_opened_connections_hostname() -> None:
    parent = _conn("Linux servers", protocol="host", lookups={"username": "{{lookup:{hostname}_root:username}}"})
    child = _conn("db01 ssh", hostname="db01", parent_id=parent.id, auth_mode="inherit")
    e = _resolve([child, parent], [_cred("db01_root", username="dbroot"), _cred("web01_root", username="x")])
    assert e.value("username") == "dbroot"


def test_failures_are_reported_not_guessed() -> None:
    c = _conn("x", hostname="web01", lookups={"username": "{{lookup:web*:username}}",
                                              "port": "{{lookup:{hostname}_root:port}}"})
    e = _resolve([c], [_cred("web01_root", username="a", attributes={"port": "ssh"}), _cred("web02", username="b")])
    assert "matches 2 credentials" in e.lookup_errors["username"]
    assert "not a port number" in e.lookup_errors["port"]
    assert e.value("username") is None


def test_only_permitted_credentials_are_candidates() -> None:
    mine, theirs = _cred("web01_root", username="mine"), _cred("web01_ROOT", username="theirs")
    c = _conn("x", hostname="web01", lookups={"username": "{{lookup:{hostname}_root:username}}"})
    assert _resolve([c], [mine, theirs], allowed={mine.id}).value("username") == "mine"
    assert "no credential" in _resolve([c], [mine, theirs], allowed=set()).lookup_errors["username"]


def test_hostname_lookup_feeds_the_hostname_placeholder() -> None:
    c = _conn("Mail", lookups={"hostname": "{{lookup:{name}:hostname}}",
                               "username": "{{lookup:{hostname}_admin:username}}"})
    e = _resolve([c], [_cred("Mail", attributes={"hostname": "mx01"}), _cred("mx01_admin", username="adm")])
    assert e.hostname is not None and e.hostname.value == "mx01" and e.value("username") == "adm"


# --------------------------------------------------------------------------- OpenBao import


def test_import_sorts_other_keys_into_attributes_and_secret_attributes() -> None:
    keys = ["username", "password", "port", "Description", "api_token", "ssh-key-old", "host name"]
    assert attribute_fields(keys, {"username", "password"}) == {
        "attr:port": "port", "attr:Description": "Description", "attr:host_name": "host name",
        "secret:api_token": "api_token", "secret:ssh-key-old": "ssh-key-old"}


# --------------------------------------------------------------------------- attributes at rest


def test_attributes_are_encrypted_and_bound_to_their_credential() -> None:
    c = _cred("web01", attributes={"port": "2222"}, secret_attributes={"api_token": "t0ps3cret"})
    assert c.attributes_enc and b"2222" not in c.attributes_enc
    assert c.secret_attributes_enc and b"t0ps3cret" not in c.secret_attributes_enc
    assert get_attributes(c, BOX) == {"port": "2222"}
    assert get_secret_attributes(c, BOX) == {"api_token": "t0ps3cret"}
    assert secret_attribute_names(c, BOX) == ["api_token"]
    other = _cred("other")
    other.attributes_enc = c.attributes_enc  # copied ciphertext does not decrypt for another row
    with pytest.raises(CryptoError):
        get_attributes(other, BOX)


def test_secret_attributes_reach_sessions_only() -> None:
    c = _cred("web01_root", secret_attributes={"api_token": "t0ps3cret"})
    assert credential_lookup_value([c], "api_token", BOX, include_secrets=True) == "t0ps3cret"
    assert credential_lookup_value([c], "api_token", BOX, include_secrets=False) is None
    with pytest.raises(LookupFailed):
        credential_lookup_value([c], "missing", BOX, include_secrets=True)
