"""Structured operational logging for the gateway.

One JSON line per chat request. The record deliberately excludes message text,
completion text, credentials and raw backend bodies.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

LOGGER_NAME = "gateway"
_HANDLER_MARKER = "gateway_handler"


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """Return the gateway logger, adding a plain handler at most once."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    for handler in logger.handlers:
        if getattr(handler, _HANDLER_MARKER, False):
            return logger

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    setattr(handler, _HANDLER_MARKER, True)
    logger.addHandler(handler)
    return logger


def log_chat_request(
    logger: logging.Logger,
    *,
    request_id: str,
    application_id: str | None,
    model: str | None,
    status: int,
    code: str | None,
    duration_ms: float,
    usage: dict[str, int] | None,
) -> None:
    record: dict[str, Any] = {
        "event": "chat_request",
        "request_id": request_id,
        "application_id": application_id,
        "model": model,
        "status": status,
        "code": code,
        "duration_ms": round(duration_ms, 3),
    }
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        record[key] = usage.get(key) if usage is not None else None
    logger.info(json.dumps(record, sort_keys=True))
