from __future__ import annotations

import hashlib
import re
from typing import Any

_SENSITIVE_PATTERNS = [
    re.compile(r"(_modal_connect_token=)[^&\s]+", re.IGNORECASE),
    re.compile(r"(authorization:\s*bearer\s+)[^\s]+", re.IGNORECASE),
    re.compile(r"(bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"https?://[^\s]*?(?:vnc|novnc|token|secret)[^\s]*", re.IGNORECASE),
    re.compile(r"artifact://[^\s]+", re.IGNORECASE),
]

_SENSITIVE_KEYS = {
    "api_key",
    "artifact_bytes",
    "artifact_uri",
    "authorization",
    "bearer",
    "bytes",
    "clipboard",
    "clipboard_text",
    "connect_token",
    "content",
    "credential",
    "data",
    "data_base64",
    "image",
    "image_bytes",
    "no_vnc_url",
    "novnc_url",
    "password",
    "raw_path",
    "screenshot",
    "screenshot_bytes",
    "secret",
    "stderr",
    "stdout",
    "text",
    "token",
    "typed_text",
    "url",
    "vnc",
    "vnc_url",
}


def sanitize_text(value: str) -> str:
    sanitized = value
    for pattern in _SENSITIVE_PATTERNS:
        sanitized = pattern.sub(
            lambda match: f"{match.group(1)}[redacted]" if match.groups() else "[redacted]",
            sanitized,
        )
    return sanitized


def sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                redacted[key] = _redacted_value(item)
                continue
            redacted[key] = sanitize_payload(item)
        return redacted
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def sanitize_payload_with_secrets(
    value: Any,
    replacements: list[tuple[str, str]],
) -> Any:
    sanitized = sanitize_payload(value)
    active = [(secret, replacement) for secret, replacement in replacements if secret]
    if not active:
        return sanitized
    return _replace_known_secrets(sanitized, active)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or normalized.endswith("_token")


def _redacted_value(value: Any) -> Any:
    if isinstance(value, dict) and value.get("redacted") is True:
        return value
    if value is None or isinstance(value, bool | int | float):
        return value
    marker: dict[str, Any] = {"redacted": True}
    if isinstance(value, str):
        marker["length"] = len(value)
    elif isinstance(value, bytes):
        marker["size_bytes"] = len(value)
    elif isinstance(value, list | tuple | dict):
        marker["items"] = len(value)
    return marker


def _replace_known_secrets(value: Any, replacements: list[tuple[str, str]]) -> Any:
    if isinstance(value, dict):
        if value.get("redacted") is True:
            return value
        return {key: _replace_known_secrets(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_known_secrets(item, replacements) for item in value]
    if isinstance(value, tuple):
        return [_replace_known_secrets(item, replacements) for item in value]
    if isinstance(value, str):
        for secret, replacement in replacements:
            if value == secret:
                return {
                    "redacted": True,
                    "length": len(secret),
                    "sha256": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
                }
            value = value.replace(secret, replacement)
        return value
    return value


def safe_exception_payload(exc: BaseException) -> dict[str, Any]:
    return {
        "redacted": True,
        "type": type(exc).__name__,
    }


class RedactedException(Exception):
    pass


def redacted_exception(exc: BaseException) -> RedactedException:
    return RedactedException(f"{type(exc).__name__}: [redacted]")
