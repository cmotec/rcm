"""External secret providers: sync/conflict rules (API level) and provider HTTP clients (mocked HTTP)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from app.config import get_settings
from app.services.secrets import sync as sync_mod
from app.services.secrets.base import BrowseItem, SecretProviderClient, SecretRef
from app.services.secrets.openbao import OpenBaoClient, OpenBaoConfig, OpenBaoSecrets
from tests.conftest import UserClient

REMOTE: dict[tuple[str, str], str] = {}


class MemoryProvider(SecretProviderClient):
    type = "openbao"
    config_model = OpenBaoConfig
    secret_model = OpenBaoSecrets

    async def test(self) -> dict[str, Any]:
        return {"ok": True}

    async def get(self, ref: SecretRef) -> str | None:
        return REMOTE.get((ref.path, ref.key))

    async def browse(self, path: str) -> list[BrowseItem]:
        return [BrowseItem(path=p, key=k, is_folder=False) for (p, k) in REMOTE if p == path]


@pytest.fixture
def memory_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sync_mod.PROVIDERS, "openbao", MemoryProvider)
    REMOTE.clear()


def _reveal(admin: UserClient, cid: str) -> str | None:
    return admin.post(f"/api/v1/credentials/{cid}/reveal", json={"fields": ["password"]}).json()["password"]


def _mapping(admin: UserClient, cid: str) -> dict:
    return admin.get(f"/api/v1/credentials/{cid}/mappings").json()[0]


def test_sync_updates_conflicts_and_resolution(admin: UserClient, memory_provider: None) -> None:
    p = admin.post("/api/v1/secret-providers", json={
        "name": "bao-test", "type": "openbao", "config": {"url": "http://bao.invalid"},
        "secrets": {"token": "tok-secret-123"},
    })
    assert p.status_code == 201, p.text
    provider = p.json()
    assert provider["secret_fields_set"] == ["token"] and "tok-secret-123" not in str(provider)
    assert admin.post(f"/api/v1/secret-providers/{provider['id']}/test").json() == {"ok": True}

    cred = admin.post("/api/v1/credentials", json={"name": "db", "username": "app"}).json()
    r = admin.put(f"/api/v1/credentials/{cred['id']}/mappings", json=[{
        "field": "password", "provider_id": provider["id"], "external_ref": {"path": "apps/db", "key": "password"}}])
    assert r.status_code == 200 and r.json()[0]["status"] == "pending"
    sync_url = f"/api/v1/secret-providers/{provider['id']}/sync"

    REMOTE[("apps/db", "password")] = "remote-1"
    res = admin.post(sync_url).json()
    assert res["status"] == "ok" and res["stats"]["updated"] == 1
    assert _reveal(admin, cred["id"]) == "remote-1"
    assert admin.get(f"/api/v1/credentials/{cred['id']}").json()["external_fields"] == ["password"]

    REMOTE[("apps/db", "password")] = "remote-2"  # remote changed, local untouched → applied
    assert admin.post(sync_url).json()["stats"]["updated"] == 1
    assert _reveal(admin, cred["id"]) == "remote-2"
    assert admin.post(sync_url).json()["stats"]["unchanged"] == 1

    # local edit, remote unchanged → local edit stays
    admin.patch(f"/api/v1/credentials/{cred['id']}", json={"password": "local-edit"})
    assert admin.post(sync_url).json()["stats"]["unchanged"] == 1
    assert _reveal(admin, cred["id"]) == "local-edit"

    # both changed → conflict, nothing overwritten
    REMOTE[("apps/db", "password")] = "remote-3"
    res = admin.post(sync_url).json()
    assert res["status"] == "partial" and res["stats"]["conflicts"] == 1
    assert _reveal(admin, cred["id"]) == "local-edit"
    mp = _mapping(admin, cred["id"])
    assert mp["status"] == "conflict"
    providers = admin.get("/api/v1/secret-providers").json()
    assert next(x for x in providers if x["id"] == provider["id"])["conflict_count"] == 1

    r = admin.post(f"/api/v1/secret-mappings/{mp['id']}/resolve", json={"action": "accept_remote"})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert _reveal(admin, cred["id"]) == "remote-3"

    # remote deleted → reported, local value kept (delete_policy=keep)
    del REMOTE[("apps/db", "password")]
    res = admin.post(sync_url).json()
    assert res["stats"]["missing"] == 1
    assert _mapping(admin, cred["id"])["status"] == "remote_missing"
    assert _reveal(admin, cred["id"]) == "remote-3"

    audit = admin.get("/api/v1/audit", params={"action": "secret_sync.*"}).json()
    assert audit["total"] >= 5
    assert "remote-3" not in str(audit) and "local-edit" not in str(audit)


def test_first_sync_with_different_local_value_is_a_conflict(admin: UserClient, memory_provider: None) -> None:
    provider = admin.post("/api/v1/secret-providers", json={
        "name": "bao-first", "type": "openbao", "config": {"url": "http://bao.invalid"}, "secrets": {"token": "t"},
    }).json()
    cred = admin.post("/api/v1/credentials", json={"name": "pre", "password": "existing-local"}).json()
    admin.put(f"/api/v1/credentials/{cred['id']}/mappings", json=[{
        "field": "password", "provider_id": provider["id"], "external_ref": {"path": "x", "key": "pw"},
        "conflict_policy": "manual"}])
    REMOTE[("x", "pw")] = "remote-value"
    res = admin.post(f"/api/v1/secret-providers/{provider['id']}/sync").json()
    assert res["stats"]["conflicts"] == 1
    assert _reveal(admin, cred["id"]) == "existing-local"
    mp = _mapping(admin, cred["id"])
    admin.post(f"/api/v1/secret-mappings/{mp['id']}/resolve", json={"action": "keep_local"})
    assert admin.post(f"/api/v1/secret-providers/{provider['id']}/sync").json()["stats"]["conflicts"] == 0
    assert _reveal(admin, cred["id"]) == "existing-local"


def test_prefer_remote_policy(admin: UserClient, memory_provider: None) -> None:
    provider = admin.post("/api/v1/secret-providers", json={
        "name": "bao-prefer", "type": "openbao", "config": {"url": "http://bao.invalid"}, "secrets": {"token": "t"},
    }).json()
    cred = admin.post("/api/v1/credentials", json={"name": "pref", "password": "local"}).json()
    admin.put(f"/api/v1/credentials/{cred['id']}/mappings", json=[{
        "field": "password", "provider_id": provider["id"], "external_ref": {"path": "y", "key": "pw"},
        "conflict_policy": "prefer_remote"}])
    REMOTE[("y", "pw")] = "remote"
    admin.post(f"/api/v1/secret-providers/{provider['id']}/sync")
    assert _reveal(admin, cred["id"]) == "remote"


def test_secret_management_requires_permission(make_user) -> None:  # noqa: ANN001
    ro = make_user(["readonly"])
    assert ro.get("/api/v1/secret-providers").status_code == 403
    assert ro.post("/api/v1/secret-providers", json={"name": "x", "type": "openbao", "config": {"url": "u"}}).status_code == 403


# --------------------------------------------------------------------------- provider HTTP clients


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_openbao_client_kv2_and_approle() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/auth/approle/login":
            return httpx.Response(200, json={"auth": {"client_token": "btok", "lease_duration": 300}})
        assert req.headers["x-vault-token"] == "btok" and req.headers["x-vault-namespace"] == "team"
        if req.url.path == "/v1/kv/data/apps/db":
            return httpx.Response(200, json={"data": {"data": {"password": "pw1", "user": "u"}}})
        if req.method == "LIST" and req.url.path == "/v1/kv/metadata/apps":
            return httpx.Response(200, json={"data": {"keys": ["db", "sub/"]}})
        return httpx.Response(404, json={"errors": []})

    async def go() -> None:
        c = OpenBaoClient({"url": "https://bao.example", "namespace": "team", "mount": "kv",
                           "auth_method": "approle"}, {"role_id": "r", "secret_id": "s"}, get_settings())
        c.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        assert await c.get(SecretRef(path="apps/db", key="password")) == "pw1"
        assert await c.get(SecretRef(path="apps/db", key="nokey")) is None
        assert await c.get(SecretRef(path="apps/none", key="password")) is None
        items = await c.browse("apps")
        assert {(i.path, i.is_folder) for i in items} == {("apps/db", False), ("apps/sub", True)}
        await c.close()

    _run(go())
