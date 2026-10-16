"""OpenBao provider (Vault-compatible API, KV v1/v2, token or AppRole auth)."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.secrets.base import BrowseItem, ProviderError, SecretProviderClient, SecretRef


class OpenBaoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(description="e.g. https://bao.example.com:8200")
    namespace: str | None = None
    mount: str = "secret"
    kv_version: Literal[1, 2] = 2
    auth_method: Literal["token", "approle"] = "token"
    approle_mount: str = "approle"
    verify_tls: bool = True
    # automatic import: every secret below import_path becomes a credential (see secrets/discovery.py)
    auto_import: bool = False
    import_path: str = ""
    # credential folder that mirrors the OpenBao paths of imported secrets (None = top level)
    import_folder_id: str | None = None
    # secrets left out of the import: one pattern per entry, * = any text, path below the mount or name
    exclude: list[str] = []


class OpenBaoSecrets(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str | None = None
    role_id: str | None = None
    secret_id: str | None = None


class OpenBaoClient(SecretProviderClient):
    type = "openbao"
    config_model = OpenBaoConfig
    secret_model = OpenBaoSecrets

    _token: str | None = None
    _token_exp: float = 0.0

    @property
    def base(self) -> str:
        return self.config.url.rstrip("/")  # type: ignore[attr-defined]

    async def _headers(self) -> dict[str, str]:
        cfg: OpenBaoConfig = self.config  # type: ignore[assignment]
        sec: OpenBaoSecrets = self.secrets  # type: ignore[assignment]
        if cfg.auth_method == "token":
            if not sec.token:
                raise ProviderError("openbao: token not configured")
            token = sec.token
        else:
            if self._token is None or time.monotonic() > self._token_exp - 30:
                if not (sec.role_id and sec.secret_id):
                    raise ProviderError("openbao: role_id/secret_id not configured")
                resp = await self._request("POST", f"{self.base}/v1/auth/{cfg.approle_mount}/login",
                                           json={"role_id": sec.role_id, "secret_id": sec.secret_id},
                                           headers=self._ns())
                if resp.status_code != 200:
                    raise ProviderError(f"openbao: AppRole login failed (HTTP {resp.status_code})")
                auth = resp.json().get("auth") or {}
                self._token = auth.get("client_token")
                self._token_exp = time.monotonic() + float(auth.get("lease_duration") or 300)
            token = self._token or ""
        return {"X-Vault-Token": token, **self._ns()}

    def _ns(self) -> dict[str, str]:
        ns = self.config.namespace  # type: ignore[attr-defined]
        return {"X-Vault-Namespace": ns} if ns else {}

    def _data_url(self, path: str) -> str:
        cfg: OpenBaoConfig = self.config  # type: ignore[assignment]
        p = path.strip("/")
        return f"{self.base}/v1/{cfg.mount}/data/{p}" if cfg.kv_version == 2 else f"{self.base}/v1/{cfg.mount}/{p}"

    def _list_url(self, path: str) -> str:
        cfg: OpenBaoConfig = self.config  # type: ignore[assignment]
        p = path.strip("/")
        return f"{self.base}/v1/{cfg.mount}/metadata/{p}" if cfg.kv_version == 2 else f"{self.base}/v1/{cfg.mount}/{p}"

    async def test(self) -> dict[str, Any]:
        resp = await self._request("GET", f"{self.base}/v1/auth/token/lookup-self", headers=await self._headers())
        if resp.status_code != 200:
            raise ProviderError(f"openbao: token lookup failed (HTTP {resp.status_code})")
        data = resp.json().get("data") or {}
        return {"ok": True, "policies": data.get("policies", []), "ttl": data.get("ttl")}

    async def get(self, ref: SecretRef) -> str | None:
        resp = await self._request("GET", self._data_url(ref.path), headers=await self._headers())
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ProviderError(f"openbao: reading '{ref.path}' failed (HTTP {resp.status_code})")
        body = resp.json().get("data") or {}
        data = body.get("data") if self.config.kv_version == 2 else body  # type: ignore[attr-defined]
        if data is None:  # KV v2: secret deleted (latest version soft-deleted)
            return None
        value = data.get(ref.key)
        return None if value is None else str(value)

    async def browse(self, path: str) -> list[BrowseItem]:
        headers = await self._headers()
        resp = await self._request("LIST", self._list_url(path), headers=headers)
        if resp.status_code == 404 and not path.strip("/"):
            # OpenBao answers 404 for an unknown mount and for an empty one alike: say so instead of
            # showing an empty list.
            cfg: OpenBaoConfig = self.config  # type: ignore[assignment]
            raise ProviderError(
                f"openbao: nothing found in the KV mount '{cfg.mount}' (KV version {cfg.kv_version}). Check the "
                f"mount name (the name of the secrets engine, e.g. 'infrastructure'), the KV version, and that "
                f"the token may list '{cfg.mount}/{'metadata/' if cfg.kv_version == 2 else ''}*'."
            )
        if resp.status_code == 404:
            items: list[BrowseItem] = []
        elif resp.status_code != 200:
            raise ProviderError(f"openbao: listing failed (HTTP {resp.status_code})")
        else:
            keys = (resp.json().get("data") or {}).get("keys", [])
            base = path.strip("/")
            items = [
                BrowseItem(path=f"{base}/{k}".strip("/"), key=None, is_folder=k.endswith("/")) for k in keys
            ]
        # keys inside the secret at `path` itself
        if path.strip("/"):
            r = await self._request("GET", self._data_url(path), headers=headers)
            if r.status_code == 200:
                body = r.json().get("data") or {}
                data = body.get("data") if self.config.kv_version == 2 else body  # type: ignore[attr-defined]
                for k in (data or {}):
                    items.append(BrowseItem(path=path.strip("/"), key=k, is_folder=False))
        return items
