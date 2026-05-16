from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..errors import DaemonHTTPError


def _safe_base_url(base_url: str | None) -> str | None:
    if base_url is None:
        return None
    parsed = urlsplit(base_url)
    netloc = parsed.netloc
    if parsed.username or parsed.password:
        hostname = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port is not None else ""
        netloc = f"{hostname}{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))

def _safe_url_origin(url: str | None) -> str | None:
    if url is None:
        return None
    parsed = urlsplit(url)
    netloc = parsed.netloc
    if parsed.username or parsed.password:
        hostname = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port is not None else ""
        netloc = f"{hostname}{port}"
    return urlunsplit((parsed.scheme, netloc, "", "", ""))

def _safe_screenshot_request(request: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in request.items()
        if key not in {"bytes", "data_base64", "text", "clipboard", "token"}
    }

def _safe_action_metadata(action: dict[str, Any]) -> dict[str, Any]:
    metadata = {"type": action.get("type") or action.get("action") or "unknown"}
    if "button" in action:
        metadata["button"] = action["button"]
    return metadata

def _safe_screenshot_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise RuntimeError("daemon returned a non-object screenshot response")
    required = ("format", "width", "height", "size_bytes")
    missing = [key for key in required if key not in result]
    if missing:
        raise RuntimeError(f"daemon screenshot response missing fields: {', '.join(missing)}")
    return {
        "format": result["format"],
        "width": result["width"],
        "height": result["height"],
        "size_bytes": result["size_bytes"],
        "storage": "artifact" if result.get("artifact_uri") else "inline",
        "artifact_backed": result.get("artifact_uri") is not None,
        "cursor_visible": result.get("cursor_visible"),
    }

def _recording_id(result: Any) -> str:
    if not isinstance(result, dict):
        raise RuntimeError("daemon returned a non-object recording start response")
    recording_id = result.get("id")
    if not isinstance(recording_id, str) or not recording_id:
        raise RuntimeError("daemon recording start response missing id")
    return recording_id

def _safe_recording_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise RuntimeError("daemon returned a non-object recording stop response")
    required = ("status", "format", "size_bytes")
    missing = [key for key in required if key not in result]
    if missing:
        raise RuntimeError(f"daemon recording stop response missing fields: {', '.join(missing)}")
    if result["status"] != "stopped":
        raise RuntimeError(f"daemon recording status was {result['status']}")
    return {
        "status": result["status"],
        "format": result["format"],
        "fps": result.get("fps"),
        "size_bytes": result["size_bytes"],
        "artifact_backed": result.get("artifact_uri") is not None,
        "duration_seconds": result.get("duration_seconds"),
        "stop_method": result.get("stop_method"),
        "return_code": result.get("return_code"),
    }

def _ensure_ok_result(result: Any) -> None:
    if not isinstance(result, dict):
        raise RuntimeError("daemon returned a non-object action response")
    if result.get("ok") is not True:
        detail = _failed_action_detail(result)
        message = "daemon action response was not ok"
        if detail:
            message = f"{message}: {detail}"
        raise RuntimeError(message)

def _failed_action_detail(result: dict[str, Any]) -> str | None:
    results = result.get("results")
    if not isinstance(results, list):
        return None
    for item in results:
        if not isinstance(item, dict) or item.get("ok") is not False:
            continue
        index = item.get("index")
        prefix = f"result[{index}]" if isinstance(index, int) else "result"
        error_code = item.get("error_code")
        error = item.get("error")
        if isinstance(error_code, str) and isinstance(error, str) and error:
            return f"{prefix} {error_code}: {error}"
        if isinstance(error_code, str):
            return f"{prefix} {error_code}"
        if isinstance(error, str) and error:
            return f"{prefix}: {error}"
    return None

def _extract_daemon_ms(result: dict[str, Any]) -> float | None:
    timing = result.get("timing")
    if timing is None:
        return None
    if not isinstance(timing, dict):
        raise RuntimeError("daemon action timing was malformed")
    daemon_ms = timing.get("daemon_ms")
    if isinstance(daemon_ms, bool) or not isinstance(daemon_ms, int | float):
        raise RuntimeError("daemon action timing.daemon_ms was malformed")
    if daemon_ms < 0:
        raise RuntimeError("daemon action timing.daemon_ms was negative")
    return float(daemon_ms)

def _failure(
    case: str,
    *,
    phase: str,
    iteration: int,
    exc: Exception,
    elapsed_ms: float | None = None,
    redacted_text: str | None = None,
) -> dict[str, Any]:
    failure: dict[str, Any] = {
        "case": case,
        "phase": phase,
        "iteration": iteration,
        "type": type(exc).__name__,
        "message": _redact_text(str(exc), redacted_text),
    }
    if elapsed_ms is not None:
        failure["elapsed_ms"] = elapsed_ms
    if isinstance(exc, DaemonHTTPError):
        failure["status_code"] = exc.status_code
        failure["code"] = exc.code
        failure["details"] = _redact_text(exc.details, redacted_text)
    elif isinstance(exc, _SandboxExecBenchmarkError):
        failure["code"] = exc.code
    elif isinstance(exc, httpx.HTTPError):
        failure["code"] = "http_error"
    return failure

def _redact_text(value: Any, redacted_text: str | None) -> Any:
    if isinstance(value, str):
        output = value
        if redacted_text:
            output = output.replace(redacted_text, "[redacted typed text]")
        output = re.sub(r"https?://[^'\"\s<>]+", _redact_url_match, output)
        replacements = [
            (r"(?i)(authorization:\s*bearer\s+)[^\s,;]+", r"\1[redacted]"),
            (r"(?i)(bearer\s+)[a-z0-9._~+/=-]+", r"\1[redacted]"),
            (
                r"(?i)(api[_-]?key|apiKey|clientSecret|secret|token)"
                r"(['\"]?\s*[:=]\s*['\"]?)[^'\"\s,;}]+",
                r"\1\2[redacted]",
            ),
        ]
        for pattern, replacement in replacements:
            output = re.sub(pattern, replacement, output)
        return output
    if isinstance(value, list):
        return [_redact_text(item, redacted_text) for item in value]
    if isinstance(value, dict):
        return {
            ("redacted_text" if key == "text" else key): _redact_text(item, redacted_text)
            for key, item in value.items()
        }
    return value

def _redact_url_match(match: re.Match[str]) -> str:
    value = match.group(0)
    trailing = ""
    while value and value[-1] in ".,);]":
        trailing = value[-1] + trailing
        value = value[:-1]
    parsed = urlsplit(value)
    safe = _safe_url_origin(value) or "[redacted-url]"
    if parsed.path or parsed.query or parsed.fragment:
        safe = f"{safe}/[redacted-url]"
    return f"{safe}{trailing}"

def _is_timeout_exception(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError) or "Timeout" in type(exc).__name__

class _SandboxExecBenchmarkError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
