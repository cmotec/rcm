"""Inheritance and resolution rules (pure, on model objects)."""

from __future__ import annotations

import asyncio
import base64
import os
import uuid

from app import models as m
from app.security.crypto import Keyring, SecretBox, aad_for
from app.services import dns
from app.services.resolution import compute_effective, hostname_candidates, is_qualified

BOX = SecretBox(Keyring.parse("k1:" + base64.b64encode(os.urandom(32)).decode()))


def conn(name: str, protocol: str = "ssh", **kw: object) -> m.Connection:
    return m.Connection(id=uuid.uuid4(), name=name, protocol=protocol, auth_mode=kw.pop("auth_mode", "inherit"),
                        inherit_auth=kw.pop("inherit_auth", True), settings=kw.pop("settings", {}), **kw)


def with_password(c: m.Connection, pw: str) -> m.Connection:
    c.password_enc = BOX.encrypt(pw, aad_for("connections", "password", c.id))
    return c


def cred(name: str, **kw: object) -> m.Credential:
    c = m.Credential(id=uuid.uuid4(), name=name, parent_id=kw.pop("parent_id", None),
                     username=kw.pop("username", None), domain=kw.pop("domain", None))
    for f in ("password", "private_key", "passphrase"):
        if f in kw:
            setattr(c, f"{f}_enc", BOX.encrypt(str(kw[f]), aad_for("credentials", f, c.id)))
    return c


def eff(chain: list[m.Connection], creds: dict | None = None, folders: list | None = None, env: list[str] | None = None,
        interactive: dict | None = None):  # noqa: ANN201
    return compute_effective(chain, creds or {}, folders or [], box=BOX, include_secrets=True,
                             env_search_domains=env or [], interactive=interactive)


def test_child_inherits_hostname_and_follows_parent_changes() -> None:
    parent = conn("Production Server", "host", hostname="server01.example.com")
    child = conn("SSH", parent_id=parent.id)
    e = eff([child, parent])
    assert e.hostname.value == "server01.example.com" and e.hostname.inherited
    parent.hostname = "server02.example.com"  # nothing is copied: the change propagates
    assert eff([child, parent]).hostname.value == "server02.example.com"
    child.hostname = "override.example.com"
    e = eff([child, parent])
    assert e.hostname.value == "override.example.com" and not e.hostname.inherited


def test_ports_are_protocol_specific() -> None:
    parent = conn("host", "host", hostname="h")
    assert eff([conn("s", "ssh"), parent]).port.value == 22
    assert eff([conn("r", "rdp"), parent]).port.value == 3389
    assert eff([conn("v", "vnc", settings={"display": 2}), parent]).port.value == 5902
    assert eff([conn("f", "ftps", settings={"tls_mode": "implicit"}), parent]).port.value == 990
    assert eff([conn("f", "ftps", settings={"tls_mode": "explicit"}), parent]).port.value == 21
    assert eff([conn("s", "ssh", port=2222), parent]).port.value == 2222


def test_search_domain_precedence() -> None:
    folder_top = m.Folder(id=uuid.uuid4(), name="top", search_domain="folder.example")
    parent = conn("p", "host", hostname="srv")
    child = conn("c", parent_id=parent.id)
    assert eff([child, parent], env=["env.example"]).search_domains.value == ["env.example"]
    assert eff([child, parent], folders=[folder_top], env=["env.example"]).search_domains.value == ["folder.example"]
    parent.search_domain = "parent.example"
    assert eff([child, parent], folders=[folder_top]).search_domains.value == ["parent.example"]
    child.search_domain = "child.example"
    assert eff([child, parent], folders=[folder_top]).search_domains.value == ["child.example"]


def test_hostname_candidates_never_touch_qualified_names() -> None:
    assert hostname_candidates("srv", ["a.example", "b.example"]) == ["srv.a.example", "srv.b.example", "srv"]
    for q in ("srv.corp.example", "srv.", "10.0.0.1", "fe80::1", "[::1]"):
        assert is_qualified(q)
        assert hostname_candidates(q, ["a.example"]) == [q]


def test_search_domain_picks_first_resolving_candidate(monkeypatch) -> None:  # noqa: ANN001
    async def fake(name: str, timeout: float) -> bool:
        return name == "srv.b.example"

    monkeypatch.setattr(dns, "_resolves", fake)
    res = asyncio.run(dns.apply_search_domains("srv", ["a.example", "b.example"]))
    assert res.effective == "srv.b.example" and res.resolved
    res = asyncio.run(dns.apply_search_domains("other", ["a.example"]))
    assert res.effective == "other.a.example" and not res.resolved
    res = asyncio.run(dns.apply_search_domains("srv.fq.example", ["a.example"]))
    assert res.effective == "srv.fq.example"


def test_field_level_credential_inheritance() -> None:
    base = cred("base", username="svc", password="base-pw", domain="CORP")
    parent = with_password(conn("p", "host", hostname="h", auth_mode="direct", username="parentuser"), "parent-pw")
    child = conn("c", parent_id=parent.id, auth_mode="credential", credential_id=base.id)
    e = eff([child, parent], {base.id: [base]})
    assert e.value("username") == "svc" and e.auth["username"].source.kind == "credential"
    assert e.value("password") == "base-pw" and e.value("domain") == "CORP"

    child2 = conn("c2", parent_id=parent.id, auth_mode="direct", username="childuser")
    e = eff([child2, parent])
    assert e.value("username") == "childuser" and not e.auth["username"].inherited
    assert e.value("password") == "parent-pw" and e.auth["password"].inherited  # missing field → parent

    child3 = conn("c3", parent_id=parent.id, auth_mode="direct", username="solo", inherit_auth=False)
    e = eff([child3, parent])
    assert e.value("username") == "solo" and e.value("password") is None  # independent


def test_credential_parent_chain() -> None:
    root = cred("root", password="root-pw", domain="ROOT")
    leaf = cred("leaf", parent_id=root.id, username="leafuser")
    c = conn("c", "rdp", hostname="h", auth_mode="credential", credential_id=leaf.id)
    e = eff([c], {leaf.id: [leaf, root]})
    assert e.value("username") == "leafuser" and not e.auth["username"].inherited
    assert e.value("password") == "root-pw" and e.auth["password"].inherited


def test_interactive_credentials() -> None:
    c = conn("c", "rdp", hostname="h", auth_mode="interactive", username="prefill")
    e = eff([c])
    assert e.prompt_required and e.value("username") == "prefill"
    e = eff([c], interactive={"password": "typed", "username": "typed-user"})
    assert not e.prompt_required
    assert e.value("password") == "typed" and e.auth["password"].source.kind == "interactive"
    assert e.value("username") == "typed-user"

    # interactive on the parent applies to children that inherit
    parent = conn("p", "host", hostname="h", auth_mode="interactive")
    child = conn("ch", parent_id=parent.id)
    e = eff([child, parent])
    assert e.prompt_required and e.interactive_connection_id == parent.id
    e = eff([child, parent], interactive={"username": "u", "password": "p"})
    assert e.value("password") == "p" and not e.prompt_required


def test_secrets_are_not_resolved_without_include_secrets() -> None:
    c = with_password(conn("c", "ssh", hostname="h", auth_mode="direct", username="u"), "pw")
    e = compute_effective([c], {}, [], box=BOX, include_secrets=False, env_search_domains=[])
    assert e.auth["password"].value is None and e.auth["password"].present
    pub = e.public()
    assert pub["auth"]["password"] == {"source": {"kind": "connection", "id": str(c.id), "name": "c"},
                                       "inherited": False, "set": True}
    assert "pw" not in str(pub)
