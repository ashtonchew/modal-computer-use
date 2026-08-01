from __future__ import annotations

import re
from typing import Any

_SENSITIVE_PATTERNS = [
    re.compile(
        r"(\b(?:[A-Z][A-Z0-9_]*_)?(?:API_KEY|PASSWORD|SECRET|TOKEN)\s*=\s*)[^\s]+",
        re.IGNORECASE,
    ),
    re.compile(r"((?:x-)?api-key\s*:\s*)[^\s]+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9._-]{6,}\b", re.IGNORECASE),
    re.compile(r"(_modal_connect_token=)[^&\s]+", re.IGNORECASE),
    re.compile(r"(authorization:\s*bearer\s+)[^\s]+", re.IGNORECASE),
    re.compile(r"(bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"https?://[^\s]*?(?:vnc|novnc|token|secret)[^\s]*", re.IGNORECASE),
    re.compile(r"artifact://[^\s]+", re.IGNORECASE),
]

SENSITIVE_PAYLOAD_KEYS = {
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
    "diagnostic_path",
    "image",
    "image_bytes",
    "log_path",
    "no_vnc_url",
    "novnc_url",
    "password",
    "raw_path",
    "screenshot",
    "screenshot_bytes",
    "secret",
    "stderr",
    "stderr_path",
    "stdout",
    "stdout_path",
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
            lambda match: f"{match.group(1)}[redacted]"
            if match.groups()
            else "[redacted]",
            sanitized,
        )
    return sanitized


def sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if is_sensitive_key(str(key)):
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


def is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in SENSITIVE_PAYLOAD_KEYS or normalized.endswith("_token")


def _redacted_value(value: Any) -> Any:
    if isinstance(value, dict) and value.get("redacted") is True:
        return _normalized_marker(value)
    if value is None:
        return value
    length = len(value) if isinstance(value, str | bytes | list | tuple | dict) else 1
    return {"redacted": True, "length": length}


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
                }
            value = value.replace(secret, replacement)
        return value
    return value


def _normalized_marker(value: dict[Any, Any]) -> dict[str, Any]:
    length = value.get("length", value.get("size_bytes", value.get("items", 0)))
    if not isinstance(length, int) or isinstance(length, bool) or length < 0:
        length = 0
    return {"redacted": True, "length": length}


def safe_exception_payload(exc: BaseException) -> dict[str, Any]:
    return {
        "redacted": True,
        "type": type(exc).__name__,
    }


class RedactedException(Exception):
    pass


def redacted_exception(exc: BaseException) -> RedactedException:
    return RedactedException(f"{type(exc).__name__}: [redacted]")
