from __future__ import annotations

import re
from typing import Any

_SENSITIVE_PATTERNS = [
    re.compile(r"(_modal_connect_token=)[^&\s]+", re.IGNORECASE),
    re.compile(r"(authorization:\s*bearer\s+)[^\s]+", re.IGNORECASE),
    re.compile(r"(bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"https?://[^\s]*?(?:vnc|novnc|token|secret)[^\s]*", re.IGNORECASE),
    re.compile(r"artifact://[^\s]+", re.IGNORECASE),
]


def sanitize_text(value: str) -> str:
    sanitized = value
    for pattern in _SENSITIVE_PATTERNS:
        sanitized = pattern.sub(
            lambda match: f"{match.group(1)}[redacted]" if match.groups() else "[redacted]",
            sanitized,
        )
    return sanitized


def safe_exception_payload(exc: BaseException) -> dict[str, Any]:
    return {
        "redacted": True,
        "type": type(exc).__name__,
    }


class RedactedException(Exception):
    pass


def redacted_exception(exc: BaseException) -> RedactedException:
    return RedactedException(f"{type(exc).__name__}: [redacted]")
