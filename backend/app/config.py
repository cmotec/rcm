"""Application configuration, read from environment variables.

Every secret setting also accepts ``<NAME>_FILE`` (Docker secrets). The file
contents win over the plain variable.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRET_SETTINGS = (
    "DB_PASSWORD",
    "ENCRYPTION_KEYS",
    "OIDC_CLIENT_SECRET",
    "LOCAL_ADMIN_PASSWORD",
    "INFISICAL_CLIENT_SECRET",
    "OPENBAO_TOKEN",
    "OPENBAO_SECRET_ID",
)


def _load_file_secrets() -> None:
    for name in SECRET_SETTINGS:
        path = os.environ.get(f"{name}_FILE")
        if path:
            with open(path, encoding="utf-8") as fh:
                os.environ[name] = fh.read().strip()


def _csv(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [v.strip() for v in value if v.strip()]
    return [v.strip() for v in value.split(",") if v.strip()]


CertPolicy = Literal["validate", "fingerprints", "tofu", "ignore"]
HostKeyPolicy = Literal["prompt", "strict", "accept-new"]


#: Product name (UI, browser title, API docs).
APP_NAME = "Remote Connections Manager"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, case_sensitive=True, extra="ignore")

    # --- application -------------------------------------------------------
    APP_URL: str = "http://localhost:8080"
    ENVIRONMENT: Literal["production", "development", "test"] = "production"
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "json"
    API_DOCS_ENABLED: bool = True

    # --- database (external PostgreSQL) -----------------------------------
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_NAME: str = "app"
    DB_USER: str = "app"
    DB_PASSWORD: str = ""
    DB_SSLMODE: Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"] = "prefer"
    DB_SSLROOTCERT: str | None = None
    DB_SSLCERT: str | None = None
    DB_SSLKEY: str | None = None
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT_SECONDS: int = 30
    DB_CONNECT_TIMEOUT_SECONDS: int = 10
    DB_MIGRATE_ON_START: bool = True

    # --- encryption at rest ------------------------------------------------
    # "kid1:<base64 32 bytes>,kid2:<base64 32 bytes>"
    ENCRYPTION_KEYS: str = ""
    ENCRYPTION_ACTIVE_KEY_ID: str | None = None

    # --- authentication ----------------------------------------------------
    OIDC_ENABLED: bool = False
    OIDC_ISSUER: str = ""
    OIDC_CLIENT_ID: str = ""
    OIDC_CLIENT_SECRET: str = ""
    OIDC_SCOPES: str = "openid profile email"
    OIDC_REDIRECT_PATH: str = "/api/v1/auth/oidc/callback"
    OIDC_POST_LOGOUT_REDIRECT_URL: str | None = None
    OIDC_USERNAME_CLAIM: str = "preferred_username"
    OIDC_GROUPS_CLAIM: str = "groups"
    OIDC_AUTO_PROVISION: bool = True
    OIDC_ADMIN_GROUPS: str = ""
    OIDC_USER_GROUPS: str = ""
    OIDC_READONLY_GROUPS: str = ""
    OIDC_DEFAULT_ROLE: Literal["user", "readonly", "none"] = "none"
    OIDC_VERIFY_TLS: bool = True
    OIDC_REVALIDATE_MINUTES: int = 15
    OIDC_DISPLAY_NAME: str = "Authentik"

    LOCAL_AUTH_ENABLED: bool = False
    LOCAL_ADMIN_USERNAME: str | None = None
    LOCAL_ADMIN_PASSWORD: str | None = None

    AUTH_SESSION_IDLE_MINUTES: int = 60
    AUTH_SESSION_MAX_HOURS: int = 12
    # Signage accounts (one connection, fullscreen display): login lifetime, no idle timeout.
    SIGNAGE_SESSION_MAX_DAYS: int = 30
    COOKIE_SECURE: bool = True
    API_TOKENS_ENABLED: bool = True

    # --- DNS ---------------------------------------------------------------
    DNS_SEARCH_DOMAINS: str = ""
    DNS_RESOLVE_TIMEOUT_SECONDS: float = 2.0

    # --- guacd ---------------------------------------------------------------
    GUACD_HOST: str = "guacd"
    GUACD_PORT: int = 4822
    GUACD_TLS: bool = False
    GUACD_TLS_VERIFY: bool = True
    GUACD_CA_FILE: str | None = None
    GUACD_CONNECT_TIMEOUT_SECONDS: float = 10.0
    GUACD_HANDSHAKE_TIMEOUT_SECONDS: float = 20.0

    # --- sessions ----------------------------------------------------------
    SESSION_TICKET_TTL_SECONDS: int = 30
    SESSION_IDLE_TIMEOUT_SECONDS: int = 8 * 3600
    SESSION_RECONNECT_GRACE_SECONDS: int = 300
    SESSION_MAX_PER_USER: int = 30

    RDP_CERT_POLICY_DEFAULT: CertPolicy = "validate"
    RDP_CERT_ALLOW_INSECURE: bool = True
    RDP_DRIVE_ENABLED: bool = True
    RDP_DRIVE_ROOT: str = "/drives"
    RECORDING_ENABLED: bool = False
    RECORDING_PATH: str = "/recordings"

    SSH_HOST_KEY_POLICY_DEFAULT: HostKeyPolicy = "prompt"
    SSH_CONNECT_TIMEOUT_SECONDS: float = 15.0
    SSH_KEEPALIVE_SECONDS: int = 30
    SSH_GUACD_ENGINE_ENABLED: bool = True
    SSH_TUNNEL_BIND_HOST: str | None = None

    FTP_TLS_VERIFY_DEFAULT: bool = True
    FTP_TIMEOUT_SECONDS: float = 30.0
    FTP_MAX_SESSIONS: int = 50
    FILE_UPLOAD_MAX_MB: int = 0

    TLS_CA_BUNDLE: str | None = None

    # --- external secret providers ------------------------------------------
    SECRET_SYNC_ENABLED: bool = True
    SECRET_SYNC_DEFAULT_INTERVAL_SECONDS: int = 900
    INFISICAL_ENABLED: bool = True
    INFISICAL_URL: str | None = None
    INFISICAL_CLIENT_ID: str | None = None
    INFISICAL_CLIENT_SECRET: str | None = None
    INFISICAL_PROJECT_ID: str | None = None
    INFISICAL_ENVIRONMENT: str | None = None
    OPENBAO_ENABLED: bool = True
    OPENBAO_URL: str | None = None
    OPENBAO_NAMESPACE: str | None = None
    OPENBAO_AUTH_METHOD: Literal["token", "approle"] = "token"
    OPENBAO_TOKEN: str | None = None
    OPENBAO_ROLE_ID: str | None = None
    OPENBAO_SECRET_ID: str | None = None
    OPENBAO_KV_MOUNT: str = "secret"

    # --- limits & features -----------------------------------------------------
    RATE_LIMIT_LOGIN_PER_MINUTE: int = 10
    RATE_LIMIT_API_PER_MINUTE: int = 600
    AUDIT_RETENTION_DAYS: int = 365
    IMPORT_MAX_FILE_MB: int = 25
    FEATURE_RDM_IMPORT: bool = True

    @field_validator("APP_URL")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @model_validator(mode="after")
    def _check(self) -> Settings:
        if self.OIDC_ENABLED and not (self.OIDC_ISSUER and self.OIDC_CLIENT_ID):
            raise ValueError("OIDC_ENABLED requires OIDC_ISSUER and OIDC_CLIENT_ID")
        if not self.OIDC_ENABLED and not self.LOCAL_AUTH_ENABLED and self.ENVIRONMENT != "test":
            raise ValueError("Enable at least one of OIDC_ENABLED or LOCAL_AUTH_ENABLED")
        return self

    # --- derived ---------------------------------------------------------------
    @property
    def app_origin(self) -> str:
        parts = urlsplit(self.APP_URL)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def search_domains(self) -> list[str]:
        return [d.strip(".") for d in _csv(self.DNS_SEARCH_DOMAINS)]

    @property
    def oidc_redirect_uri(self) -> str:
        return self.APP_URL + self.OIDC_REDIRECT_PATH

    @property
    def oidc_group_roles(self) -> dict[str, list[str]]:
        return {
            "admin": _csv(self.OIDC_ADMIN_GROUPS),
            "user": _csv(self.OIDC_USER_GROUPS),
            "readonly": _csv(self.OIDC_READONLY_GROUPS),
        }

    @property
    def session_cookie_name(self) -> str:
        return "__Host-session" if self.COOKIE_SECURE else "session"

    @property
    def csrf_cookie_name(self) -> str:
        return "__Host-csrf" if self.COOKIE_SECURE else "csrf"

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


@lru_cache
def get_settings() -> Settings:
    _load_file_secrets()
    return Settings()  # type: ignore[call-arg]


def reset_settings_cache() -> None:
    get_settings.cache_clear()


__all__ = ["Settings", "get_settings", "reset_settings_cache"]
