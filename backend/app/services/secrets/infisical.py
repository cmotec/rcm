"""Infisical provider (Universal Auth + raw secrets API)."""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.services.secrets.base import BrowseItem, ProviderError, SecretProviderClient, SecretRef


class InfisicalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(description="e.g. https://app.infisical.com or your self-hosted URL")
    project_id: str = Field(description="workspace / project ID")
    environment: str = Field("prod", description="environment slug")
    verify_tls: bool = True


class InfisicalSecrets(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: str
    client_secret: str


class InfisicalClient(SecretProviderClient):
    type = "infisical"
    config_model = InfisicalConfig
    secret_model = InfisicalSecrets

    _token: str | None = None
    _token_exp: float = 0.0

    @property
    def base(self) -> str:
        return self.config.url.rstrip("/")  # type: ignore[attr-defined]

    async def _auth(self) -> dict[str, str]:
        if self._token is None or time.monotonic() > self._token_exp - 30:
            resp = await self._request(
                "POST", f"{self.base}/api/v1/auth/universal-auth/login",
                json={"clientId": self.secrets.client_id, "clientSecret": self.secrets.client_secret},  # type: ignore[attr-defined]
            )
            if resp.status_code != 200:
                raise ProviderError(f"infisical: authentication failed (HTTP {resp.status_code})")
            data = resp.json()
            self._token = data.get("accessToken")
            self._token_exp = time.monotonic() + float(data.get("expiresIn") or 300)
            if not self._token:
                raise ProviderError("infisical: no access token in response")
        return {"Authorization": f"Bearer {self._token}"}

    def _params(self, ref_path: str, environment: str | None) -> dict[str, Any]:
        return {
            "workspaceId": self.config.project_id,  # type: ignore[attr-defined]
            "environment": environment or self.config.environment,  # type: ignore[attr-defined]
            "secretPath": ref_path or "/",
        }

    async def test(self) -> dict[str, Any]:
        headers = await self._auth()
        resp = await self._request("GET", f"{self.base}/api/v3/secrets/raw", headers=headers,
                                   params=self._params("/", None))
        if resp.status_code != 200:
            raise ProviderError(f"infisical: listing secrets failed (HTTP {resp.status_code})")
        return {"ok": True, "secrets_at_root": len(resp.json().get("secrets", []))}

    async def get(self, ref: SecretRef) -> str | None:
        headers = await self._auth()
        resp = await self._request("GET", f"{self.base}/api/v3/secrets/raw/{ref.key}", headers=headers,
                                   params=self._params(ref.path, ref.environment))
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ProviderError(f"infisical: reading '{ref.key}' failed (HTTP {resp.status_code})")
        secret = resp.json().get("secret") or {}
        value = secret.get("secretValue")
        return value if isinstance(value, str) else None

    async def browse(self, path: str) -> list[BrowseItem]:
        headers = await self._auth()
        resp = await self._request("GET", f"{self.base}/api/v3/secrets/raw", headers=headers,
                                   params=self._params(path, None))
        if resp.status_code != 200:
            raise ProviderError(f"infisical: listing failed (HTTP {resp.status_code})")
        data = resp.json()
        items = [BrowseItem(path=path, key=s.get("secretKey"), is_folder=False) for s in data.get("secrets", [])]
        folders_resp = await self._request(
            "GET", f"{self.base}/api/v1/folders", headers=headers,
            params={"workspaceId": self.config.project_id, "environment": self.config.environment,  # type: ignore[attr-defined]
                    "path": path or "/"},
        )
        if folders_resp.status_code == 200:
            for f in folders_resp.json().get("folders", []):
                sub = (path.rstrip("/") + "/" + f.get("name", "")) if f.get("name") else path
                items.append(BrowseItem(path=sub, key=None, is_folder=True))
        return items
