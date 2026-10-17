"""API tests against a real PostgreSQL: auth, CRUD, inheritance, permissions, credentials, import."""

from __future__ import annotations

import uuid

from sqlalchemy import select, text

from app import models as m
from tests.conftest import ADMIN_PASSWORD, ADMIN_USER, UserClient, run_in_app_loop

ROOT = str(uuid.UUID(int=0))


def test_health_and_auth_config(client) -> None:  # noqa: ANN001
    assert client.get("/healthz").json() == {"status": "ok"}
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["database"] is True
    cfg = client.get("/api/v1/auth/config").json()
    assert cfg["local_enabled"] is True and cfg["oidc_enabled"] is False


def test_unauthenticated_and_bad_login(client) -> None:  # noqa: ANN001
    client.cookies.clear()
    r = client.get("/api/v1/tree")
    assert r.status_code == 401 and r.headers["content-type"].startswith("application/problem+json")
    r = client.post("/api/v1/auth/local/login", json={"username": ADMIN_USER, "password": "wrong"})
    assert r.status_code == 401 and r.json()["code"] == "invalid_credentials"


def test_csrf_and_origin_required_for_cookie_auth(client, admin: UserClient) -> None:  # noqa: ANN001
    admin._prepare()
    r = client.post("/api/v1/folders", json={"name": "no-csrf"})
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"
    r = admin.post("/api/v1/folders", json={"name": "evil"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["code"] == "origin_rejected"


def test_me(admin: UserClient) -> None:
    me = admin.get("/api/v1/auth/me").json()
    assert me["username"] == ADMIN_USER and me["is_admin"] and "admin" in me["roles"]


def test_folder_connection_hierarchy_and_effective_values(admin: UserClient) -> None:
    folder = admin.post("/api/v1/folders", json={"name": "Production", "color": "#ff0000", "icon": "server",
                                                 "search_domain": "corp.example"}).json()
    parent = admin.post("/api/v1/connections", json={
        "name": "Production Server", "protocol": "host", "hostname": "server01", "folder_id": folder["id"],
        "auth_mode": "direct", "username": "ops", "password": "parent-secret"}).json()
    assert parent["password_set"] is True and "password" not in parent
    ssh = admin.post("/api/v1/connections", json={"name": "SSH", "protocol": "ssh", "parent_id": parent["id"]}).json()
    rdp = admin.post("/api/v1/connections", json={"name": "RDP", "protocol": "rdp", "parent_id": parent["id"],
                                                  "port": 3390, "auth_mode": "direct", "username": "Administrator"}).json()
    eff = admin.get(f"/api/v1/connections/{ssh['id']}/effective").json()
    assert eff["hostname"]["value"] == "server01" and eff["hostname"]["inherited"] is True
    assert eff["port"]["value"] == 22 and eff["port"]["source"]["kind"] == "default"
    assert eff["search_domains"]["value"] == ["corp.example"] and eff["search_domains"]["source"]["kind"] == "folder"
    assert eff["hostname_candidates"] == ["server01.corp.example", "server01"]
    assert eff["auth"]["username"]["value"] == "ops" and eff["auth"]["password"] == {
        "source": {"kind": "connection", "id": parent["id"], "name": "Production Server"}, "inherited": True,
        "set": True}
    eff_rdp = admin.get(f"/api/v1/connections/{rdp['id']}/effective").json()
    assert eff_rdp["port"]["value"] == 3390 and eff_rdp["auth"]["username"]["value"] == "Administrator"
    assert eff_rdp["auth"]["password"]["inherited"] is True

    # renaming the parent host propagates
    r = admin.patch(f"/api/v1/connections/{parent['id']}", json={"hostname": "server02", "version": parent["version"]})
    assert r.status_code == 200
    assert admin.get(f"/api/v1/connections/{ssh['id']}/effective").json()["hostname"]["value"] == "server02"
    # stale version is rejected
    r = admin.patch(f"/api/v1/connections/{parent['id']}", json={"hostname": "x", "version": parent["version"]})
    assert r.status_code == 412

    tree = admin.get("/api/v1/tree").json()
    ids = {c["id"] for c in tree["connections"]}
    assert {parent["id"], ssh["id"], rdp["id"]} <= ids
    # deleting a parent with children needs recursive=true
    assert admin.delete(f"/api/v1/connections/{parent['id']}").status_code == 409
    assert admin.delete(f"/api/v1/folders/{folder['id']}").status_code == 409


def test_settings_validation_and_warnings(admin: UserClient) -> None:
    r = admin.post("/api/v1/connections", json={"name": "bad", "protocol": "rdp", "hostname": "h",
                                                "settings": {"no_such_setting": True}})
    assert r.status_code == 422
    vnc = admin.post("/api/v1/connections", json={"name": "vnc", "protocol": "vnc", "hostname": "h"}).json()
    assert [w["code"] for w in vnc["warnings"]] == ["encryption_not_guaranteed"]
    ftp = admin.post("/api/v1/connections", json={"name": "ftp", "protocol": "ftp", "hostname": "h"}).json()
    assert [w["code"] for w in ftp["warnings"]] == ["unencrypted"]
    rdp = admin.post("/api/v1/connections", json={"name": "rdp-ignore", "protocol": "rdp", "hostname": "h",
                                                  "settings": {"cert_policy": "ignore"}}).json()
    assert [w["code"] for w in rdp["warnings"]] == ["cert_ignored"]


def test_move_cycle_rejected(admin: UserClient) -> None:
    a = admin.post("/api/v1/connections", json={"name": "A", "protocol": "host", "hostname": "a"}).json()
    b = admin.post("/api/v1/connections", json={"name": "B", "protocol": "ssh", "parent_id": a["id"]}).json()
    r = admin.post(f"/api/v1/connections/{a['id']}/move", json={"parent_id": b["id"]})
    assert r.status_code == 422
    f1 = admin.post("/api/v1/folders", json={"name": "F1"}).json()
    f2 = admin.post("/api/v1/folders", json={"name": "F2", "parent_id": f1["id"]}).json()
    r = admin.patch(f"/api/v1/folders/{f1['id']}", json={"parent_id": f2["id"]})
    assert r.status_code == 422
    r = admin.post(f"/api/v1/connections/{a['id']}/move", json={"folder_id": f2["id"]})
    assert r.status_code == 200 and r.json()["folder_id"] == f2["id"]


def test_duplicate_with_children(admin: UserClient) -> None:
    a = admin.post("/api/v1/connections", json={"name": "Dup", "protocol": "host", "hostname": "d",
                                                "auth_mode": "direct", "username": "u", "password": "pw"}).json()
    admin.post("/api/v1/connections", json={"name": "Dup SSH", "protocol": "ssh", "parent_id": a["id"]})
    copy = admin.post(f"/api/v1/connections/{a['id']}/duplicate", json={"include_children": True}).json()
    assert copy["name"] == "Dup (copy)" and copy["password_set"] is True
    children = admin.get("/api/v1/connections", params={"parent_id": copy["id"]}).json()
    assert children["total"] == 1


def test_credentials_encrypted_at_rest_and_reveal_is_audited(client, admin: UserClient) -> None:  # noqa: ANN001
    secret = "very-secret-password-xyz"
    cred = admin.post("/api/v1/credentials", json={"name": "svc", "username": "svc", "password": secret}).json()
    assert cred["password_set"] and "password" not in cred
    empty = admin.post("/api/v1/credentials", json={"name": "empty"})
    assert empty.status_code == 201  # every field is optional

    async def raw() -> bytes:
        from app.db import session_scope

        async with session_scope() as db:
            row = (await db.execute(text("SELECT password_enc FROM credentials WHERE id = :i"),
                                    {"i": cred["id"]})).scalar_one()
            return bytes(row)

    blob = run_in_app_loop(client, raw)
    assert secret.encode() not in blob and len(blob) > 30

    listed = admin.get("/api/v1/credentials").json()
    assert secret not in str(listed)
    revealed = admin.post(f"/api/v1/credentials/{cred['id']}/reveal", json={"reason": "test"}).json()
    assert revealed["password"] == secret
    audit = admin.get("/api/v1/audit", params={"action": "credential.reveal"}).json()
    assert audit["total"] >= 1 and secret not in str(audit)


def test_private_key_validation(admin: UserClient) -> None:
    import asyncssh

    key = asyncssh.generate_private_key("ssh-ed25519")
    pem = key.export_private_key("openssh", passphrase="pp").decode()
    ok = admin.post("/api/v1/credentials", json={"name": "key", "private_key": pem, "passphrase": "pp"})
    assert ok.status_code == 201 and ok.json()["private_key_fingerprint"].startswith("SHA256:")
    bad = admin.post("/api/v1/credentials", json={"name": "key2", "private_key": pem, "passphrase": "wrong"})
    assert bad.status_code == 422 and bad.json()["code"] == "private_key_invalid"


def test_idempotency_key(admin: UserClient) -> None:
    body = {"name": "Idem", "protocol": "ssh", "hostname": "idem.example"}
    h = {"Idempotency-Key": "idem-1"}
    r1 = admin.post("/api/v1/connections", json=body, headers=h)
    r2 = admin.post("/api/v1/connections", json=body, headers=h)
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"] and r2.headers.get("idempotent-replayed") == "true"
    r3 = admin.post("/api/v1/connections", json={**body, "name": "other"}, headers=h)
    assert r3.status_code == 409


def test_permissions_and_acl(admin: UserClient, make_user) -> None:  # noqa: ANN001
    ro: UserClient = make_user(["readonly"])
    user: UserClient = make_user(["user"])
    folder = admin.post("/api/v1/folders", json={"name": "Shared"}).json()
    conn = admin.post("/api/v1/connections", json={"name": "Shared SSH", "protocol": "ssh", "hostname": "s",
                                                   "folder_id": folder["id"]}).json()
    secret_cred = admin.post("/api/v1/credentials", json={"name": "admin-only", "password": "x"}).json()

    # nothing granted yet
    assert ro.get(f"/api/v1/connections/{conn['id']}").status_code == 404
    assert conn["id"] not in {c["id"] for c in ro.get("/api/v1/tree").json()["connections"]}

    roles = {r["name"]: r["id"] for r in admin.get("/api/v1/roles").json()}
    r = admin.put(f"/api/v1/acl/folder/{folder['id']}", json=[
        {"principal_type": "role", "principal_id": roles["readonly"], "permission": "manage"},
        {"principal_type": "user", "principal_id": user.id, "permission": "edit"},
    ])
    assert r.status_code == 200 and len(r.json()) == 2

    got = ro.get(f"/api/v1/connections/{conn['id']}").json()
    assert got["permission"] == "connect"  # capped by the readonly role
    assert ro.patch(f"/api/v1/connections/{conn['id']}", json={"name": "x"}).status_code == 403
    assert ro.post("/api/v1/folders", json={"name": "nope"}).status_code == 403
    tree = ro.get("/api/v1/tree").json()
    assert folder["id"] in {f["id"] for f in tree["folders"]}

    # standard user: edit inside the folder, but cannot use a credential they cannot see
    assert user.patch(f"/api/v1/connections/{conn['id']}", json={"description": "edited"}).status_code == 200
    assert user.delete(f"/api/v1/connections/{conn['id']}").status_code == 403  # needs manage
    r = user.patch(f"/api/v1/connections/{conn['id']}",
                   json={"auth_mode": "credential", "credential_id": secret_cred["id"]})
    assert r.status_code in (403, 422)
    own = user.post("/api/v1/connections", json={"name": "Mine", "protocol": "ssh", "hostname": "m"}).json()
    assert own["permission"] == "manage"
    assert admin.get(f"/api/v1/connections/{own['id']}").status_code == 200  # admins see everything

    eff = admin.get("/api/v1/acl/effective", params={"resource_type": "connection", "resource_id": conn["id"],
                                                     "user_id": ro.id}).json()
    assert eff["permission"] == "connect"
    # credential reveal is forbidden without manage on the credential
    assert user.post(f"/api/v1/credentials/{secret_cred['id']}/reveal", json={}).status_code == 404


def test_api_token_auth(admin: UserClient, client) -> None:  # noqa: ANN001
    tok = admin.post("/api/v1/me/tokens", json={"name": "ci", "read_only": True}).json()
    assert tok["token"].startswith("pat_")
    client.cookies.clear()
    h = {"Authorization": f"Bearer {tok['token']}"}
    assert client.get("/api/v1/tree", headers=h).status_code == 200
    r = client.post("/api/v1/folders", json={"name": "x"}, headers=h)
    assert r.status_code == 403
    admin.delete(f"/api/v1/me/tokens/{tok['id']}")
    client.cookies.clear()
    assert client.get("/api/v1/tree", headers=h).status_code == 401


def test_rdm_import_preview_and_apply(admin: UserClient) -> None:
    from tests.test_rdm_import import RDM_XML

    r = admin.post("/api/v1/imports/rdm", files={"file": ("export.rdm", RDM_XML, "application/xml")})
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["preview"]["summary"]["by_kind"]["connection"] == 3
    target = admin.post("/api/v1/folders", json={"name": "Imported"}).json()
    res = admin.post(f"/api/v1/imports/{job['id']}/apply", json={"target_folder_id": target["id"]}).json()
    report = res["result"]
    assert report["created"]["connections"] == 3 and report["created"]["credentials"] == 1
    assert report["created"]["folders"] == 2
    assert any(s["reason"].startswith("unsupported entry type") for s in report["skipped"])
    assert any(mm["name"] == "ACME DC01" for mm in report["manual_migration"])
    assert any(u["name"] == "ACME DC01" for u in report["unsupported_settings"])
    conns = admin.get("/api/v1/connections", params={"q": "dc01"}).json()["items"]
    dc = next(c for c in conns if c["name"] == "ACME DC01")
    assert dc["auth_mode"] == "credential" and dc["settings"]["security"] == "nla"
    child = next(c for c in conns if c["name"] == "DC01 SSH")
    assert child["parent_id"] == dc["id"]
    # applying twice is refused; a re-import detects duplicates
    assert admin.post(f"/api/v1/imports/{job['id']}/apply", json={}).status_code == 409
    again = admin.post("/api/v1/imports/rdm", files={"file": ("export.rdm", RDM_XML, "application/xml")}).json()
    assert again["preview"]["summary"]["duplicates"] == 3


def test_audit_trail_and_settings(admin: UserClient, make_user) -> None:  # noqa: ANN001
    created = admin.get("/api/v1/audit", params={"action": "connection.*"}).json()
    assert created["total"] > 0
    r = admin.patch("/api/v1/settings", json={"rdp_cert_policy_default": "tofu"})
    assert r.status_code == 200 and r.json()["values"]["rdp_cert_policy_default"] == "tofu"
    admin.patch("/api/v1/settings", json={"rdp_cert_policy_default": None})
    u = make_user(["user"])
    assert u.patch("/api/v1/settings", json={"rdp_cert_allow_insecure": False}).status_code == 403
    own = u.get("/api/v1/audit").json()  # users see their own trail only
    assert all(e["actor_id"] in (u.id, None) for e in own["items"])


def test_users_are_provisioned_with_roles(client, admin: UserClient) -> None:  # noqa: ANN001
    async def roles_of_admin() -> list[str]:
        from app.db import session_scope

        async with session_scope() as db:
            user = (await db.execute(select(m.User).where(m.User.username == ADMIN_USER))).scalar_one()
            return list((await db.execute(select(m.Role.name).join(m.UserRole, m.UserRole.role_id == m.Role.id)
                                          .where(m.UserRole.user_id == user.id))).scalars())

    assert run_in_app_loop(client, roles_of_admin) == ["admin"]
    assert ADMIN_PASSWORD  # bootstrap admin exists and can log in (fixture)


def test_bulk_move_and_delete_credentials(admin: UserClient) -> None:
    folder = admin.post("/api/v1/credential-folders", json={"name": "bulk target"}).json()
    parent = admin.post("/api/v1/credentials", json={"name": "bulk parent"}).json()
    child = admin.post("/api/v1/credentials", json={"name": "bulk child", "parent_id": parent["id"]}).json()
    used = admin.post("/api/v1/credentials", json={"name": "bulk used"}).json()
    conn = admin.post("/api/v1/connections", json={"name": "bulk conn", "protocol": "ssh", "hostname": "h",
                                                   "auth_mode": "credential", "credential_id": used["id"]})
    assert conn.status_code == 201, conn.text
    ids = [parent["id"], child["id"], used["id"]]
    missing = str(uuid.uuid4())

    r = admin.post("/api/v1/credentials/bulk/move", json={"ids": [*ids, missing], "folder_id": folder["id"]})
    assert r.status_code == 200, r.text
    assert set(r.json()["done"]) == set(ids) and [f["id"] for f in r.json()["failed"]] == [missing]
    assert all(admin.get(f"/api/v1/credentials/{i}").json()["folder_id"] == folder["id"] for i in ids)
    assert admin.post("/api/v1/credentials/bulk/move", json={"ids": ids, "folder_id": None}).json()["failed"] == []

    # parent and child go together (child first); the credential a connection uses stays
    r = admin.post("/api/v1/credentials/bulk/delete", json={"ids": ids}).json()
    assert set(r["done"]) == {parent["id"], child["id"]}
    assert [(f["id"], f["name"]) for f in r["failed"]] == [(used["id"], "bulk used")]
    assert "referenced" in r["failed"][0]["error"]
    assert admin.get(f"/api/v1/credentials/{parent['id']}").status_code == 404


def test_folder_authentication_is_inherited(admin: UserClient) -> None:
    r = admin.post("/api/v1/folders", json={"name": "auth folder", "auth_mode": "direct", "username": "fadmin",
                                            "domain": "CORP", "password": "folder-secret"})
    assert r.status_code == 201, r.text
    f = r.json()
    assert f["auth_mode"] == "direct" and f["password_set"] is True and "folder-secret" not in r.text
    c = admin.post("/api/v1/connections", json={"name": "in auth folder", "protocol": "rdp", "hostname": "h",
                                                "folder_id": f["id"]}).json()
    e = admin.get(f"/api/v1/connections/{c['id']}/effective").json()
    assert e["auth"]["username"]["value"] == "fadmin" and e["auth"]["username"]["source"]["kind"] == "folder"
    assert e["auth"]["password"]["set"] is True and e["auth"]["password"]["inherited"] is True

    # switching the mode drops what the new mode does not use
    r = admin.patch(f"/api/v1/folders/{f['id']}", json={"auth_mode": "interactive", "version": f["version"]})
    assert r.json()["password_set"] is False and r.json()["username"] == "fadmin"
    assert admin.post("/api/v1/sessions", json={"connection_id": c["id"]}).json()["code"] == "credentials_required"
