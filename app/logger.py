"""Opt-in operational logs; callers must not include case text or credentials."""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, TextIO


_context: ContextVar[dict[str, Any]] = ContextVar("lexvault_log_context", default={})
request_id: ContextVar[str] = ContextVar("lexvault_request_id", default="")
_FIELDS = {"case_id", "run_id", "job_id", "user_name", "duration_ms", "tool_name", "error_type", "runtime", "status"}
_HANDLER_NAME = "lexvault_structured"


def _safe_message(message: str) -> str:
    # Defense in depth: callers must still avoid arbitrary exception bodies.
    message = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", message)
    return re.sub(
        r"(?i)\b(token|password|passwd|api[_-]?key|authorization|cookie)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]", message,
    )


class StructuredFormatter(logging.Formatter):
    """One JSON object per event; exceptions are categorized, not dumped."""

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _safe_message(record.getMessage()),
        }
        context = _context.get()
        for key in _FIELDS:
            value = getattr(record, key, context.get(key))
            if value is not None and isinstance(value, (str, int, float, bool)):
                data[key] = _safe_message(value) if isinstance(value, str) else value
        if request_id.get():
            data["request_id"] = request_id.get()
        if record.exc_info and record.exc_info[0]:
            data["error_type"] = record.exc_info[0].__name__
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def setup_logging(level: str = "INFO", *, stream: TextIO | None = None) -> None:
    """Explicit, idempotent setup that preserves host-provided log handlers."""
    resolved_level = logging.getLevelNamesMapping().get(level.upper())
    if resolved_level is None:
        raise ValueError("Unsupported logging level")
    root = logging.getLogger()
    handler = next((item for item in root.handlers if item.get_name() == _HANDLER_NAME), None)
    if handler is None:
        handler = logging.StreamHandler(stream or sys.stdout)
        handler.set_name(_HANDLER_NAME)
        root.addHandler(handler)
    elif stream is not None:
        handler.setStream(stream)
    handler.setFormatter(StructuredFormatter())
    handler.setLevel(resolved_level)
    root.setLevel(resolved_level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"law_review.{name}")


class LogContext:
    """Task-local context; thread pools must propagate copy_context explicitly."""

    def __init__(self, logger: logging.Logger, **fields: Any):
        self.logger = logger
        self.fields = {key: value for key, value in fields.items() if key in _FIELDS}
        self.token = None

    def __enter__(self):
        self.token = _context.set({**_context.get(), **self.fields})
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.token is not None:
            _context.reset(self.token)
            self.token = None
