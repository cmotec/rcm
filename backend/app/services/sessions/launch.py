"""Launch configuration: everything needed to open a remote session.

Built from the effective (inherited) connection values plus global settings.
Contains plaintext secrets: it only ever lives in memory for the lifetime
of a session.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import ValidationFailed
from app.schemas.protocol_settings import parse_settings, security_warnings
from app.security.crypto import SecretBox
from app.services.app_settings import RuntimeSettings
from app.services.dns import apply_search_domains
from app.services.resolution import LAUNCHABLE, EffectiveConnection, resolve_connection


@dataclass
class LaunchConfig:
    connection_id: uuid.UUID
    connection_name: str
    protocol: str
    host: str
    port: int
    original_host: str
    host_note: str | None
    username: str | None
    password: str | None
    domain: str | None
    private_key: str | None
    passphrase: str | None
    settings: Any  # protocol settings model instance
    auth_sources: dict[str, Any]
    warnings: list[dict[str, str]]
    host_key_policy: str = "prompt"
    jump: LaunchConfig | None = None
    tunnel_via: LaunchConfig | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def wipe(self) -> None:
        self.password = self.private_key = self.passphrase = None
        if self.jump:
            self.jump.wipe()
        if self.tunnel_via:
            self.tunnel_via.wipe()

    def __repr__(self) -> str:  # never print secrets
        return f"LaunchConfig({self.protocol}://{self.host}:{self.port}, connection={self.connection_id})"


async def build_launch(
    db: AsyncSession,
    connection_id: uuid.UUID,
    *,
    settings: Settings,
    runtime: RuntimeSettings,
    box: SecretBox,
    interactive: dict[str, str] | None = None,
    chosen_credential_id: uuid.UUID | None = None,
    _depth: int = 0,
) -> tuple[LaunchConfig, EffectiveConnection]:
    if _depth > 3:
        raise ValidationFailed("jump host / tunnel chain is too deep")
    eff = await resolve_connection(
        db,
        connection_id,
        box=box,
        include_secrets=True,
        env_search_domains=settings.search_domains,
        interactive=interactive,
        chosen_credential_id=chosen_credential_id,
    )
    conn = eff.connection
    if conn.protocol not in LAUNCHABLE:
        raise ValidationFailed("this entry is a host container and cannot be opened")
    if eff.hostname is None:
        raise ValidationFailed("no hostname configured (neither on the connection nor on a parent)")
    if eff.port is None:
        raise ValidationFailed("no port configured")
    domains = eff.search_domains.value if eff.search_domains else []
    resolution = await apply_search_domains(eff.hostname.value, domains, settings.DNS_RESOLVE_TIMEOUT_SECONDS)

    proto_settings = parse_settings(conn.protocol, conn.settings)
    warnings = security_warnings(
        conn.protocol,
        conn.settings or {},
        rdp_default=runtime.rdp_cert_policy_default,
        ftp_verify_default=runtime.ftp_tls_verify_default,
        ssh_default=runtime.ssh_host_key_policy_default,
    )
    host_key_policy = getattr(proto_settings, "host_key_policy", None) or runtime.ssh_host_key_policy_default

    launch = LaunchConfig(
        connection_id=conn.id,
        connection_name=conn.name,
        protocol=conn.protocol,
        host=resolution.effective,
        port=int(eff.port.value),
        original_host=eff.hostname.value,
        host_note=resolution.note if not resolution.resolved else None,
        username=eff.value("username"),
        password=eff.value("password"),
        domain=eff.value("domain"),
        private_key=eff.value("private_key"),
        passphrase=eff.value("passphrase"),
        settings=proto_settings,
        auth_sources=eff.auth_sources(),
        warnings=warnings,
        host_key_policy=host_key_policy,
    )

    jump_id = getattr(proto_settings, "jump_host_connection_id", None)
    if jump_id:
        if jump_id == conn.id:
            raise ValidationFailed("a connection cannot be its own jump host")
        jump, _ = await build_launch(
            db, jump_id, settings=settings, runtime=runtime, box=box, _depth=_depth + 1
        )
        if jump.protocol != "ssh":
            raise ValidationFailed("jump host must be an SSH connection")
        launch.jump = jump

    tunnel = getattr(proto_settings, "ssh_tunnel", None)
    if tunnel is not None:
        via, _ = await build_launch(
            db, tunnel.via_connection_id, settings=settings, runtime=runtime, box=box, _depth=_depth + 1
        )
        if via.protocol != "ssh":
            raise ValidationFailed("SSH tunnel endpoint must be an SSH connection")
        launch.tunnel_via = via
        launch.warnings = [w for w in launch.warnings if w["code"] != "encryption_not_guaranteed"]

    if conn.protocol == "rdp":
        policy = proto_settings.cert_policy or runtime.rdp_cert_policy_default
        if policy in ("tofu", "ignore") and not runtime.rdp_cert_allow_insecure:
            raise ValidationFailed(
                "insecure RDP certificate policies are disabled globally (RDP_CERT_ALLOW_INSECURE=false)"
            )
        launch.extra["cert_policy"] = policy
    return launch, eff
