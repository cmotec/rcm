"""Helpers shared by the v1 routers."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.api.deps import Principal
from app.errors import Conflict, PreconditionFailed


@dataclass
class Paging:
    limit: int
    offset: int


def paging(
    limit: int = Query(100, ge=1, le=1000, description="page size"),
    offset: int = Query(0, ge=0, description="items to skip"),
) -> Paging:
    return Paging(limit, offset)


def page(items: list[Any], total: int, p: Paging) -> dict[str, Any]:
    return {"items": items, "total": total, "limit": p.limit, "offset": p.offset}


def check_version(current: int, expected: int | None) -> None:
    if expected is not None and expected != current:
        raise PreconditionFailed(f"resource was modified (current version {current}, sent {expected})")


@dataclass
class Idempotency:
    record: m.IdempotencyKey | None
    replay: JSONResponse | None

    async def store(self, status: int, body: Any) -> None:
        if self.record is not None:
            self.record.status_code = status
            self.record.response_body = jsonable_encoder(body)


async def idempotency(db: AsyncSession, request: Request, principal: Principal, body: Any) -> Idempotency:
    """Honour ``Idempotency-Key`` on POST: a retried request returns the original response."""
    key = request.headers.get("idempotency-key")
    if not key:
        return Idempotency(None, None)
    if len(key) > 255:
        raise Conflict("Idempotency-Key too long")
    digest = hashlib.sha256(
        json.dumps(jsonable_encoder(body), sort_keys=True).encode() + request.url.path.encode()
    ).hexdigest()
    existing = (
        await db.execute(
            select(m.IdempotencyKey).where(m.IdempotencyKey.user_id == principal.id, m.IdempotencyKey.key == key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.request_hash != digest or existing.path != request.url.path:
            raise Conflict("Idempotency-Key was already used with a different request", code="idempotency_mismatch")
        if existing.status_code is None:
            raise Conflict("a request with this Idempotency-Key is still in progress", code="idempotency_in_progress")
        resp = JSONResponse(existing.response_body, status_code=existing.status_code)
        resp.headers["Idempotent-Replayed"] = "true"
        return Idempotency(None, resp)
    rec = m.IdempotencyKey(user_id=principal.id, key=key, method=request.method, path=request.url.path,
                           request_hash=digest)
    db.add(rec)
    await db.flush()
    return Idempotency(rec, None)


def uuid_or_none(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None
