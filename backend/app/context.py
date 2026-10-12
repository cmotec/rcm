"""Per-request context (request id, client address) available to logging and auditing."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class RequestInfo:
    request_id: str | None = None
    ip: str | None = None
    user_agent: str | None = None


request_info: ContextVar[RequestInfo] = ContextVar("request_info", default=RequestInfo())  # noqa: B039


def current_request() -> RequestInfo:
    return request_info.get()
