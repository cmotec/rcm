"""Provider interface for external secret managers.

Adding a provider: subclass ``SecretProviderClient``, implement ``test``,
``get`` and ``browse``, and register it in ``PROVIDERS`` with its config
schema.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, ConfigDict

from app.config import Settings


class ProviderError(Exception):
    """Provider unreachable, authentication failed, or malformed response."""


@dataclass
class BrowseItem:
    path: str
    key: str | None
    is_folder: bool


class SecretRef(BaseModel):
    """Location of one secret value in the provider."""

    model_config = ConfigDict(extra="forbid")
    path: str = "/"
    key: str
    environment: str | None = None  # Infisical only


class SecretProviderClient(ABC):
    type: ClassVar[str]
    config_model: ClassVar[type[BaseModel]]
    secret_model: ClassVar[type[BaseModel]]

    def __init__(self, config: dict[str, Any], secrets: dict[str, Any], settings: Settings) -> None:
        self.config = self.config_model.model_validate(config)
        self.secrets = self.secret_model.model_validate(secrets)
        verify: bool | str = getattr(self.config, "verify_tls", True)
        if verify and settings.TLS_CA_BUNDLE:
            verify = settings.TLS_CA_BUNDLE
        self.http = httpx.AsyncClient(timeout=15.0, verify=verify)

    async def close(self) -> None:
        await self.http.aclose()

    async def __aenter__(self) -> SecretProviderClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @abstractmethod
    async def test(self) -> dict[str, Any]:
        """Authenticate and return non-secret information about the connection."""

    @abstractmethod
    async def get(self, ref: SecretRef) -> str | None:
        """Return the secret value, or ``None`` if it does not exist."""

    @abstractmethod
    async def browse(self, path: str) -> list[BrowseItem]:
        """List secret names (never values) under a path."""

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self.http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.type}: request failed: {exc}") from exc
