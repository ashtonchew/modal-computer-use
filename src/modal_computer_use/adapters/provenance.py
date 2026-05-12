from __future__ import annotations

from typing import Any

PROVIDER_ACTION_METADATA_KEY = "provider_action"
PROVIDER_ACTION_REDACTIONS_METADATA_KEY = "provider_action_redactions"

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
    "text",
    "token",
    "typed_text",
    "url",
    "vnc_url",
}


def with_provider_provenance(
    payload: dict[str, Any],
    provider_action: dict[str, Any],
) -> dict[str, Any]:
    redacted_action, redactions = redact_provider_action(provider_action)
    metadata = dict(payload.get("metadata") or {})
    metadata[PROVIDER_ACTION_METADATA_KEY] = redacted_action
    if redactions:
        metadata[PROVIDER_ACTION_REDACTIONS_METADATA_KEY] = redactions
    payload["metadata"] = metadata
    return payload


def redact_provider_action(action: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    redactions: list[str] = []
    action_name = str(action.get("type") or action.get("action") or "")
    redacted = _redact_value(action, redactions, path="", action_name=action_name)
    return redacted, redactions


def _redact_value(
    value: Any,
    redactions: list[str],
    *,
    path: str,
    action_name: str,
) -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            key_path = f"{path}.{key}" if path else str(key)
            if _is_sensitive_key(str(key), action_name=action_name):
                redactions.append(key_path)
                output[key] = _redaction_marker(item)
                continue
            output[key] = _redact_value(
                item,
                redactions,
                path=key_path,
                action_name=action_name,
            )
        return output
    if isinstance(value, list):
        return [
            _redact_value(
                item,
                redactions,
                path=f"{path}[{index}]",
                action_name=action_name,
            )
            for index, item in enumerate(value)
        ]
    return value


def _is_sensitive_key(key: str, *, action_name: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if normalized == "text" and action_name in {"key", "hold_key"}:
        return False
    return normalized in _SENSITIVE_KEYS or normalized.endswith("_token")


def _redaction_marker(value: Any) -> dict[str, Any]:
    marker: dict[str, Any] = {"redacted": True}
    if isinstance(value, str):
        marker["length"] = len(value)
    elif isinstance(value, bytes):
        marker["size_bytes"] = len(value)
    elif isinstance(value, list | tuple | dict):
        marker["items"] = len(value)
    return marker
