from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("agenthub_request_id", default=None)
trace_id_var: ContextVar[str | None] = ContextVar("agenthub_trace_id", default=None)


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
                payload[key] = value
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
