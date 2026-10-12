"""Global runtime settings: environment defaults, overridable by administrators (stored in DB)."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.config import Settings

KEYS = (
    "rdp_cert_policy_default",
    "rdp_cert_allow_insecure",
    "ssh_host_key_policy_default",
    "ftp_tls_verify_default",
    "banner_message",
)


@dataclass
class RuntimeSettings:
    rdp_cert_policy_default: str
    rdp_cert_allow_insecure: bool
    ssh_host_key_policy_default: str
    ftp_tls_verify_default: bool
    banner_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def env_defaults(settings: Settings) -> RuntimeSettings:
    return RuntimeSettings(
        rdp_cert_policy_default=settings.RDP_CERT_POLICY_DEFAULT,
        rdp_cert_allow_insecure=settings.RDP_CERT_ALLOW_INSECURE,
        ssh_host_key_policy_default=settings.SSH_HOST_KEY_POLICY_DEFAULT,
        ftp_tls_verify_default=settings.FTP_TLS_VERIFY_DEFAULT,
        banner_message=None,
    )


_cache: tuple[float, RuntimeSettings] | None = None
TTL = 10.0


async def get_runtime(db: AsyncSession, settings: Settings) -> RuntimeSettings:
    global _cache
    if _cache and time.monotonic() - _cache[0] < TTL:
        return _cache[1]
    rs = env_defaults(settings)
    rows = (await db.execute(select(m.AppSetting).where(m.AppSetting.key.in_(KEYS)))).scalars()
    for row in rows:
        setattr(rs, row.key, row.value)
    # An env hard-off for insecure certificate policies cannot be relaxed from the UI.
    if not settings.RDP_CERT_ALLOW_INSECURE:
        rs.rdp_cert_allow_insecure = False
    _cache = (time.monotonic(), rs)
    return rs


def invalidate() -> None:
    global _cache
    _cache = None


async def overrides(db: AsyncSession) -> dict[str, Any]:
    rows = (await db.execute(select(m.AppSetting).where(m.AppSetting.key.in_(KEYS)))).scalars()
    return {r.key: r.value for r in rows}
