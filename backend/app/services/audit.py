"""Audit logging.

Successful changes are written in the caller's transaction, so the audit
record and the change commit together. Failures and denials are written in a
separate transaction, so they persist even though the request rolls back.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.context import current_request
from app.db import session_scope
from app.logging import get_logger, redact_mapping

log = get_logger("audit")


def _event(
    action: str,
    *,
    actor_id: uuid.UUID | None,
    actor_name: str | None,
    resource_type: str | None,
    resource_id: Any,
    outcome: str,
    details: dict[str, Any] | None,
) -> m.AuditEvent:
    req = current_request()
    safe = redact_mapping(details or {})
    return m.AuditEvent(
        actor_id=actor_id,
        actor_name=actor_name,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        outcome=outcome,
        ip=req.ip,
        user_agent=(req.user_agent or "")[:512] or None,
        request_id=req.request_id,
        details=safe,
    )


async def record(
    db: AsyncSession,
    action: str,
    *,
    actor_id: uuid.UUID | None = None,
    actor_name: str | None = None,
    resource_type: str | None = None,
    resource_id: Any = None,
    outcome: str = "success",
    details: dict[str, Any] | None = None,
) -> None:
    ev = _event(
        action,
        actor_id=actor_id,
        actor_name=actor_name,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        details=details,
    )
    db.add(ev)
    log.info("audit", action=action, outcome=outcome, actor=actor_name, resource_type=resource_type,
             resource_id=ev.resource_id)


async def record_detached(
    action: str,
    *,
    actor_id: uuid.UUID | None = None,
    actor_name: str | None = None,
    resource_type: str | None = None,
    resource_id: Any = None,
    outcome: str = "failure",
    details: dict[str, Any] | None = None,
) -> None:
    try:
        async with session_scope() as db:
            await record(
                db,
                action,
                actor_id=actor_id,
                actor_name=actor_name,
                resource_type=resource_type,
                resource_id=resource_id,
                outcome=outcome,
                details=details,
            )
    except Exception as exc:  # audit must never break the request path
        log.error("audit_write_failed", action=action, error=str(exc)[:200])
