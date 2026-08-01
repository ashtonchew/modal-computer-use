from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from modal_computer_use.redaction import (
    SENSITIVE_PAYLOAD_KEYS,
    safe_exception_payload,
    sanitize_text,
)

SENSITIVE_KEYS = SENSITIVE_PAYLOAD_KEYS


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if _is_sensitive_log_key(lowered):
                if isinstance(item, str | bytes | list | tuple | dict):
                    length = len(item)
                else:
                    length = 0 if item is None else 1
                result[key] = {"redacted": True, "length": length}
            else:
                result[key] = redact(item)
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def _is_sensitive_log_key(key: str) -> bool:
    normalized = key.replace("-", "_")
    return normalized in SENSITIVE_KEYS or normalized.endswith("_token")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": (
                "[redacted exception]" if record.exc_info else sanitize_text(record.getMessage())
            ),
        }
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            payload.update(redact(extra))
        if record.exc_info:
            payload["exc_info"] = safe_exception_payload(record.exc_info[1] or Exception())
        return json.dumps(payload, separators=(",", ":"))


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("modal_computer_use")
    root.handlers[:] = [handler]
    root.setLevel(level)
