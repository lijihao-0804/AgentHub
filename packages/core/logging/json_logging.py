from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("agenthub_request_id", default=None)
trace_id_var: ContextVar[str | None] = ContextVar("agenthub_trace_id", default=None)

_SENSITIVE_KEY_PARTS = frozenset(
    {"password", "secret", "token", "authorization", "cookie", "api_key", "credential"}
)


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).casefold().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***REDACTED***" if _is_sensitive_key(key) else _redact(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact(child) for child in value]
    if isinstance(value, tuple):
        return tuple(_redact(child) for child in value)
    return value


class JsonFormatter(logging.Formatter):
    reserved = set(logging.LogRecord(None, 0, "", 0, "", (), None).__dict__) | {
        "message",
        "asctime",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        trace_id = trace_id_var.get()
        if request_id is not None:
            payload["request_id"] = request_id
        if trace_id is not None:
            payload["trace_id"] = trace_id
        for key, value in record.__dict__.items():
            if (
                key not in self.reserved
                and key not in {"request_id", "trace_id"}
                and not key.startswith("_")
            ):
                payload[key] = "***REDACTED***" if _is_sensitive_key(key) else _redact(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
