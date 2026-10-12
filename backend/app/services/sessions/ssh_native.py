"""Native SSH (asyncssh): host-key verification, jump hosts, connection setup."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import asyncssh
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.config import Settings
from app.errors import AppError, UpstreamError, ValidationFailed
from app.logging import get_logger
from app.services import audit
from app.services.auth import now
from app.services.sessions.launch import LaunchConfig

log = get_logger(__name__)


class HostKeyIssue(AppError):
    status, code, title = 409, "hostkey_unknown", "SSH host key needs confirmation"


class RemoteAuthFailed(AppError):
    status, code, title = 401, "remote_auth_failed", "Remote authentication failed"


def fingerprint(key: asyncssh.SSHKey) -> str:
    return key.get_fingerprint("sha256")


def public_line(key: asyncssh.SSHKey) -> str:
    return key.export_public_key("openssh").decode().strip()


@dataclass
class HostKeyDecision:
    """Fingerprints the user explicitly accepted for this launch, keyed by 'host:port'."""

    accepted: dict[str, str] = field(default_factory=dict)
    may_replace: bool = False

    def accepts(self, host: str, port: int, fp: str) -> bool:
        return self.accepted.get(f"{host}:{port}") == fp


async def fetch_host_key(
    host: str, port: int, *, timeout: float, tunnel: asyncssh.SSHClientConnection | None = None
) -> asyncssh.SSHKey:
    try:
        key = await asyncio.wait_for(
            asyncssh.get_server_host_key(host, port, tunnel=tunnel or (), config=[]), timeout
        )
    except TimeoutError as exc:
        raise UpstreamError(f"timeout connecting to {host}:{port}", code="connect_timeout") from exc
    except (OSError, asyncssh.Error) as exc:
        raise UpstreamError(f"cannot connect to {host}:{port}: {exc}", code="connect_failed") from exc
    if key is None:
        raise UpstreamError("server did not present a host key", code="hostkey_missing")
    return key


async def verify_host_key(
    db: AsyncSession,
    launch: LaunchConfig,
    decision: HostKeyDecision,
    *,
    user_id: Any,
    user_name: str,
    timeout: float,
    tunnel: asyncssh.SSHClientConnection | None = None,
) -> asyncssh.SSHKey:
    host, port = launch.host, launch.port
    key = await fetch_host_key(host, port, timeout=timeout, tunnel=tunnel)
    fp = fingerprint(key)
    ktype = key.get_algorithm()
    known = (
        await db.execute(select(m.KnownHost).where(m.KnownHost.hostname == host, m.KnownHost.port == port))
    ).scalars().all()
    info = {
        "hostname": host,
        "port": port,
        "key_type": ktype,
        "fingerprint": fp,
        "public_key": public_line(key),
        "connection_id": str(launch.connection_id),
        "connection_name": launch.connection_name,
    }
    for row in known:
        if row.fingerprint == fp:
            row.last_seen_at = now()
            return key
    same_type = [r for r in known if r.key_type == ktype]
    if same_type:
        if decision.accepts(host, port, fp) and decision.may_replace:
            prev = same_type[0].fingerprint
            same_type[0].fingerprint = fp
            same_type[0].public_key = public_line(key)
            same_type[0].added_by = user_id
            same_type[0].last_seen_at = now()
            await audit.record(db, "known_host.replace", actor_id=user_id, actor_name=user_name,
                               resource_type="known_host", resource_id=same_type[0].id,
                               details={**info, "previous_fingerprint": prev})
            return key
        await audit.record_detached("known_host.mismatch", actor_id=user_id, actor_name=user_name,
                                    resource_type="known_host", resource_id=same_type[0].id,
                                    outcome="denied", details={**info, "previous_fingerprint": same_type[0].fingerprint})
        raise HostKeyIssue(
            "The SSH host key has CHANGED. This may indicate a man-in-the-middle attack.",
            code="hostkey_changed",
            extra={"host_key": {**info, "previous_fingerprint": same_type[0].fingerprint,
                                "can_replace": decision.may_replace}},
        )
    policy = launch.host_key_policy
    if policy == "accept-new" or (policy == "prompt" and decision.accepts(host, port, fp)):
        row = m.KnownHost(hostname=host, port=port, key_type=ktype, fingerprint=fp,
                          public_key=public_line(key), added_by=user_id, last_seen_at=now())
        db.add(row)
        await db.flush()
        await audit.record(db, "known_host.add", actor_id=user_id, actor_name=user_name,
                           resource_type="known_host", resource_id=row.id, details={**info, "policy": policy})
        return key
    if policy == "strict":
        raise HostKeyIssue("Unknown SSH host key and the policy is 'strict'; an administrator must add it.",
                           code="hostkey_untrusted", extra={"host_key": info})
    raise HostKeyIssue("Unknown SSH host key — confirm the fingerprint to continue.", code="hostkey_unknown",
                       extra={"host_key": info})


def _client_keys(launch: LaunchConfig) -> list[asyncssh.SSHKey] | None:
    if not launch.private_key:
        return None
    try:
        return [asyncssh.import_private_key(launch.private_key, launch.passphrase or None)]
    except (asyncssh.KeyImportError, ValueError, TypeError) as exc:
        hint = "missing or wrong passphrase" if "passphrase" in str(exc).lower() else str(exc)
        raise ValidationFailed(f"private key could not be loaded: {hint}", code="private_key_invalid") from exc


async def connect(
    launch: LaunchConfig,
    host_keys: list[asyncssh.SSHKey],
    settings: Settings,
    *,
    tunnel: asyncssh.SSHClientConnection | None = None,
) -> asyncssh.SSHClientConnection:
    """Connect, trusting only ``host_keys`` (the server is asked to use one of their algorithms)."""
    if not launch.username:
        raise ValidationFailed("no username configured or entered for this SSH connection", code="username_required")
    s = launch.settings
    timeout = float(getattr(s, "connect_timeout", None) or settings.SSH_CONNECT_TIMEOUT_SECONDS)
    keepalive = getattr(s, "keepalive_interval", None)
    keepalive = settings.SSH_KEEPALIVE_SECONDS if keepalive is None else keepalive
    algs: list[str] = []
    for k in host_keys:
        algs.extend(a.decode() for a in k.sig_algorithms if a.decode() not in algs)
    try:
        return await asyncio.wait_for(
            asyncssh.connect(
                launch.host,
                launch.port,
                tunnel=tunnel or (),
                config=[],
                username=launch.username,
                password=launch.password or None,
                client_keys=_client_keys(launch),
                agent_path=None,
                known_hosts=(list(host_keys), [], []),
                server_host_key_algs=algs,
                x509_trusted_certs=None,
                keepalive_interval=keepalive or 0,
                login_timeout=timeout,
            ),
            timeout + 5,
        )
    except asyncssh.PermissionDenied as exc:
        raise RemoteAuthFailed(f"SSH authentication failed for {launch.username}@{launch.host}") from exc
    except asyncssh.HostKeyNotVerifiable as exc:
        raise HostKeyIssue("host key changed during connection", code="hostkey_changed") from exc
    except TimeoutError as exc:
        raise UpstreamError(f"timeout connecting to {launch.host}:{launch.port}", code="connect_timeout") from exc
    except (OSError, asyncssh.Error) as exc:
        raise UpstreamError(f"SSH connection to {launch.host}:{launch.port} failed: {exc}", code="connect_failed") from exc


@dataclass
class SshConnectionSet:
    """A target connection plus the jump connection it is tunnelled through."""

    conn: asyncssh.SSHClientConnection
    jump: asyncssh.SSHClientConnection | None = None

    def close(self) -> None:
        self.conn.close()
        if self.jump is not None:
            self.jump.close()

    async def wait_closed(self) -> None:
        for c in (self.conn, self.jump):
            if c is not None:
                try:
                    await asyncio.wait_for(c.wait_closed(), 5)
                except (TimeoutError, OSError, asyncssh.Error):
                    pass


async def preflight(
    db: AsyncSession, launch: LaunchConfig, decision: HostKeyDecision, settings: Settings, *, user_id: Any,
    user_name: str,
) -> None:
    """Verify host keys (jump host and target) before a session is created."""
    timeout = settings.SSH_CONNECT_TIMEOUT_SECONDS
    if launch.jump is None:
        await verify_host_key(db, launch, decision, user_id=user_id, user_name=user_name, timeout=timeout)
        return
    jump_key = await verify_host_key(db, launch.jump, decision, user_id=user_id, user_name=user_name,
                                     timeout=timeout)
    jump_conn = await connect(launch.jump, [jump_key], settings)
    try:
        await verify_host_key(db, launch, decision, user_id=user_id, user_name=user_name, timeout=timeout,
                              tunnel=jump_conn)
    finally:
        jump_conn.close()


async def trusted_key(db: AsyncSession, host: str, port: int) -> list[asyncssh.SSHKey]:
    rows = (
        await db.execute(select(m.KnownHost).where(m.KnownHost.hostname == host, m.KnownHost.port == port))
    ).scalars().all()
    keys = []
    for r in rows:
        try:
            keys.append(asyncssh.import_public_key(r.public_key))
        except (asyncssh.KeyImportError, ValueError):
            log.warning("known_host_unparseable", id=str(r.id))
    return keys


async def open_connection_set(
    db: AsyncSession, launch: LaunchConfig, settings: Settings
) -> SshConnectionSet:
    """Connect (via jump host if configured) using only already-trusted host keys."""

    async def _connect_trusted(
        target: LaunchConfig, tunnel: asyncssh.SSHClientConnection | None
    ) -> asyncssh.SSHClientConnection:
        keys = await trusted_key(db, target.host, target.port)
        if not keys:
            raise HostKeyIssue("host key is not trusted yet; reopen the connection to confirm it",
                               code="hostkey_unknown")
        return await connect(target, keys, settings, tunnel=tunnel)

    jump_conn = None
    if launch.jump is not None:
        jump_conn = await _connect_trusted(launch.jump, None)
    try:
        conn = await _connect_trusted(launch, jump_conn)
    except BaseException:
        if jump_conn is not None:
            jump_conn.close()
        raise
    return SshConnectionSet(conn=conn, jump=jump_conn)
