from __future__ import annotations

import re
from importlib import metadata as importlib_metadata
from typing import Any

from ..constants import PROVIDER_BENCHMARK_TEXT
from ..safety import _redact_text, _safe_url_origin


def import_provider_module(module: str, *fromlist: str) -> Any:
    return __import__(module, fromlist=list(fromlist))


def package_version(package: str) -> str | None:
    try:
        return importlib_metadata.version(package)
    except importlib_metadata.PackageNotFoundError:
        return None


def provider_computer_use(sandbox: Any) -> Any:
    computer_use = getattr(sandbox, "computer_use", None)
    if computer_use is None:
        computer_use = getattr(sandbox, "computerUse", None)
    if computer_use is None:
        raise RuntimeError("provider sandbox did not expose computer use")
    return computer_use


def call_first_available(target: Any, names: tuple[str, ...], *args: Any) -> Any:
    for name in names:
        method = getattr(target, name, None)
        if callable(method):
            return method(*args)
    raise RuntimeError(f"provider object did not expose any of: {', '.join(names)}")


def sanitize_provider_observation(observation: Any) -> dict[str, Any] | None:
    if observation is None:
        return None
    if not isinstance(observation, dict):
        return {"type": type(observation).__name__}
    redacted = _redact_provider_value(observation)
    return redacted if isinstance(redacted, dict) else {"value": redacted}


def safe_provider_metadata_value(value: str | None) -> str | None:
    if value is None:
        return None
    if value.startswith(("http://", "https://")):
        return _safe_url_origin(value)
    return _redact_text(value, PROVIDER_BENCHMARK_TEXT)


def provider_numeric_attr(value: Any, names: tuple[str, ...]) -> float | None:
    for name in names:
        raw_value = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
        parsed = _float_or_none(raw_value)
        if parsed is not None:
            return parsed
    return None


def provider_exit_code(result: Any) -> int | None:
    for attr in ("exit_code", "return_code", "code"):
        value = getattr(result, attr, None)
        if value is not None:
            return int(value)
    if isinstance(result, dict):
        for key in ("exit_code", "return_code", "code"):
            value = result.get(key)
            if value is not None:
                return int(value)
    return 0


def provider_stdout(result: Any) -> str:
    if isinstance(result, dict):
        for key in ("stdout", "result", "output"):
            value = result.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                nested = value.get("stdout") or value.get("result")
                if isinstance(nested, str):
                    return nested
    for attr in ("stdout", "result", "output"):
        value = getattr(result, attr, None)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            nested = value.get("stdout") or value.get("result")
            if isinstance(nested, str):
                return nested
    return ""


def _redact_provider_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _is_sensitive_provider_key(key):
        return _redaction_marker(value)
    if isinstance(value, dict):
        return {
            item_key: _redact_provider_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_provider_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_provider_value(item) for item in value]
    if isinstance(value, str):
        return safe_provider_metadata_value(value)
    return value


def _is_sensitive_provider_key(key: str) -> bool:
    normalized = _normalize_provider_key(key)
    return normalized in {
        "stdout",
        "stderr",
        "text",
        "bytes",
        "data",
        "data_base64",
        "secret",
        "client_secret",
        "access_key",
        "secret_key",
        "private_key",
        "credential",
        "credentials",
        "url",
        "auth_key",
        "authorization",
        "bearer",
        "api_key",
        "token",
        "password",
    } or normalized.endswith(("_token", "_url", "_uri", "_secret", "_key"))


def _normalize_provider_key(key: str) -> str:
    underscored = re.sub(r"(?<!^)(?=[A-Z])", "_", key)
    return underscored.lower().replace("-", "_")


def _redaction_marker(value: Any) -> dict[str, Any]:
    marker: dict[str, Any] = {"redacted": True}
    if isinstance(value, str):
        marker["length"] = len(value)
    elif isinstance(value, bytes | bytearray):
        marker["size_bytes"] = len(value)
    elif isinstance(value, list | tuple | dict):
        marker["items"] = len(value)
    return marker


def _float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
