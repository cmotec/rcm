"""Structured logging with secret redaction."""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

SENSITIVE_KEY = re.compile(
    r"(pass(word|phrase)?|secret|token|private[_-]?key|authorization|cookie|ticket|credential|api[_-]?key)",
    re.IGNORECASE,
)
REDACTED = "[REDACTED]"
# Values that look like secrets even under harmless keys: (pattern, replacement).
SENSITIVE_VALUE = [
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
        REDACTED,
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer " + REDACTED),
    (re.compile(r"(?i)([?&](?:ticket|token|code|state)=)[^&\s]+"), r"\g<1>" + REDACTED),
]


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        for pattern, replacement in SENSITIVE_VALUE:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, (list, tuple)):
        return type(value)(redact_value(v) for v in value)
    return value


def redact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(key, str) and SENSITIVE_KEY.search(key) and value not in (None, "", False):
            if isinstance(value, (bool, int)) or key.endswith(("_id", "_set", "_present", "_count")):
                out[key] = value
            else:
                out[key] = REDACTED
        else:
            out[key] = redact_value(value)
    return out


def _redact_processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    event = event_dict.get("event")
    redacted = redact_mapping({k: v for k, v in event_dict.items() if k != "event"})
    if event is not None:
        redacted["event"] = redact_value(event)
    return redacted


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact_processor,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer() if fmt == "json" else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(foreign_pre_chain=shared, processor=renderer)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(log_level)
    # Access logs carry query strings (WebSocket tickets); our middleware logs requests instead.
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = False
    for noisy in ("asyncssh", "httpx", "httpcore", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(max(log_level, logging.WARNING))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
