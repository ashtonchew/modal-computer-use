from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from modal_computer_use.adapters.provenance import (
    PROVIDER_ACTION_METADATA_KEY,
    PROVIDER_ACTION_REDACTIONS_METADATA_KEY,
)
from modal_computer_use.models import ActionBatchRequest, ActionItemResult, TraceEntry
from modal_computer_use.redaction import sanitize_text
from modal_computer_use.tracing import TraceWriter


class ActionTraceWriter:
    def __init__(self, context: Any) -> None:
        self._context = context

    def append_action(
        self,
        payload: ActionBatchRequest,
        action: Any,
        result: ActionItemResult,
        *,
        call_id: str,
        sequence: int,
    ) -> None:
        _append_trace(
            self._context,
            payload,
            action,
            result,
            call_id=call_id,
            sequence=sequence,
        )

    def append_screenshot_after(
        self,
        payload: ActionBatchRequest,
        screenshot: Any | None,
        result: ActionItemResult | None,
        *,
        call_id: str,
    ) -> None:
        _append_screenshot_after_trace(
            self._context,
            payload,
            screenshot,
            result,
            call_id=call_id,
        )

def _append_trace(
    context: Any,
    payload: ActionBatchRequest,
    action: Any,
    result: ActionItemResult,
    *,
    call_id: str,
    sequence: int,
) -> None:
    if not context.state.settings.trace_actions:
        return
    writer = TraceWriter(context.state.settings.trace_dir / "actions.ndjson")
    normalized, redactions = _redacted_action_and_paths(action, preserve_provider_action=True)
    provider_action, provider_redactions = _provider_trace_metadata(normalized)
    redactions.extend(provider_redactions)
    redactions = list(dict.fromkeys(redactions))
    trace_result, result_redactions = _trace_result(result)
    redactions.extend(result_redactions)
    redactions = list(dict.fromkeys(redactions))
    writer.append(
        TraceEntry(
            ts=datetime.now(UTC),
            run_id=_safe_trace_text(payload.run_id or context.state.settings.run_id),
            call_id=_safe_trace_text(call_id) or call_id,
            sequence=payload.sequence if payload.sequence is not None else sequence,
            source=_safe_trace_text(payload.source) or payload.source,
            provider_action=provider_action,
            normalized_action=normalized,
            result=trace_result,
            elapsed_ms=result.elapsed_ms,
            screenshot_after_uri=None,
            coordinate_space=_coordinate_space(result),
            redactions=redactions,
            error=_trace_error(result),
        )
    )


def _trace_result(result: ActionItemResult) -> tuple[dict[str, Any], list[str]]:
    payload = result.model_dump(mode="json")
    redacted, redactions = _redact_trace_result_payload(payload, path="result")
    return redacted if isinstance(redacted, dict) else payload, redactions


_OMITTED_TRACE_RESULT_KEYS = {"bytes", "data_base64"}


def _redact_trace_result_payload(value: Any, *, path: str = "") -> tuple[Any, list[str]]:
    redactions: list[str] = []
    if isinstance(value, dict):
        if _is_redaction_marker(value):
            return _normalized_redaction_marker(value), [path] if path else []
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            item_path = f"{path}.{key}" if path else str(key)
            normalized = str(key).lower().replace("-", "_")
            if normalized in _OMITTED_TRACE_RESULT_KEYS:
                redactions.append(item_path)
                continue
            if _is_sensitive_trace_key(str(key)):
                redacted[key] = _redacted_sensitive_value(item)
                redactions.append(item_path)
                continue
            redacted_item, child_redactions = _redact_trace_result_payload(item, path=item_path)
            redacted[key] = redacted_item
            redactions.extend(child_redactions)
        return redacted, redactions
    if isinstance(value, list):
        redacted_items = []
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]" if path else f"[{index}]"
            redacted_item, child_redactions = _redact_trace_result_payload(item, path=item_path)
            redacted_items.append(redacted_item)
            redactions.extend(child_redactions)
        return redacted_items, redactions
    if isinstance(value, str):
        sanitized = sanitize_text(value)
        if sanitized != value:
            return sanitized, [path] if path else []
    return value, []


def _append_screenshot_after_trace(
    context: Any,
    payload: ActionBatchRequest,
    screenshot: Any | None,
    result: ActionItemResult | None,
    *,
    call_id: str,
) -> None:
    if not context.state.settings.trace_actions:
        return
    writer = TraceWriter(context.state.settings.trace_dir / "actions.ndjson")
    if result is not None:
        trace_result, redactions = _trace_result(result)
    elif screenshot is not None:
        trace_result, redactions = _redact_trace_result_payload(
            {
                "ok": True,
                "format": screenshot.format,
                "width": screenshot.width,
                "height": screenshot.height,
                "size_bytes": screenshot.size_bytes,
                "artifact_uri": screenshot.artifact_uri,
            },
            path="result",
        )
        if not isinstance(trace_result, dict):
            trace_result = {}
    else:
        trace_result = {}
        redactions = []
    writer.append(
        TraceEntry(
            ts=datetime.now(UTC),
            run_id=_safe_trace_text(payload.run_id or context.state.settings.run_id),
            call_id=_safe_trace_text(call_id) or call_id,
            sequence=payload.sequence if payload.sequence is not None else len(payload.actions),
            source=_safe_trace_text(payload.source) or payload.source,
            normalized_action={"type": "screenshot_after"},
            result=trace_result,
            screenshot_after_uri=None,
            coordinate_space=screenshot.coordinate_space if screenshot is not None else None,
            redactions=redactions,
            error=_trace_error(result) if result is not None else None,
        )
    )


def _trace_error(result: ActionItemResult) -> dict[str, Any] | None:
    if result.error is None and result.error_code is None:
        return None
    error: dict[str, Any] = {}
    if result.error_code is not None:
        error["code"] = result.error_code
    if result.error is not None:
        error["message"] = result.error
    return error


def _safe_trace_text(value: str | None) -> str | None:
    if value is None:
        return None
    return sanitize_text(value)


def _coordinate_space(result: ActionItemResult) -> Any:
    output = result.output or {}
    return output.get("coordinate_space")


def _redacted_action(action: Any) -> dict[str, Any]:
    return _redacted_action_and_paths(action)[0]


def _redacted_action_and_paths(
    action: Any, *, preserve_provider_action: bool = False
) -> tuple[dict[str, Any], list[str]]:
    data = action.model_dump(mode="json")
    redacted, redactions = _redact_action_payload(
        data, preserve_provider_action=preserve_provider_action
    )
    return redacted if isinstance(redacted, dict) else data, redactions


def _redact_action_payload(
    value: Any, *, path: str = "", preserve_provider_action: bool = False
) -> tuple[Any, list[str]]:
    redactions: list[str] = []
    if isinstance(value, dict):
        if _is_redaction_marker(value):
            return _normalized_redaction_marker(value), [path] if path else []
        redacted: dict[str, Any] = {}
        is_type_action = value.get("type") == "type" and isinstance(value.get("text"), str)
        for key, item in value.items():
            item_path = f"{path}.{key}" if path else key
            if preserve_provider_action and key == PROVIDER_ACTION_METADATA_KEY:
                redacted[key] = item
                continue
            if is_type_action and key == "text":
                redacted[key] = _redacted_text(item)
                redactions.append(item_path)
                continue
            if _is_sensitive_trace_key(str(key)):
                redacted[key] = _redacted_sensitive_value(item)
                redactions.append(item_path)
                continue
            redacted_item, child_redactions = _redact_action_payload(
                item,
                path=item_path,
                preserve_provider_action=preserve_provider_action,
            )
            redacted[key] = redacted_item
            redactions.extend(child_redactions)
        return redacted, redactions
    if isinstance(value, list):
        redacted_items = []
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]" if path else f"[{index}]"
            redacted_item, child_redactions = _redact_action_payload(
                item,
                path=item_path,
                preserve_provider_action=preserve_provider_action,
            )
            redacted_items.append(redacted_item)
            redactions.extend(child_redactions)
        return redacted_items, redactions
    if isinstance(value, str):
        sanitized = sanitize_text(value)
        if sanitized != value:
            return sanitized, [path] if path else []
    return value, []


def _redacted_text(text: str) -> dict[str, Any]:
    return {
        "redacted": True,
        "length": len(text),
    }


_SENSITIVE_TRACE_KEYS = {
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
    "stderr",
    "stdout",
    "text",
    "token",
    "typed_text",
    "url",
    "vnc_url",
}


def _is_sensitive_trace_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _SENSITIVE_TRACE_KEYS or normalized.endswith("_token")


def _redacted_sensitive_value(value: Any) -> dict[str, Any]:
    if _is_redaction_marker(value):
        return _normalized_redaction_marker(value)
    if isinstance(value, str | bytes | list | tuple | dict):
        length = len(value)
    elif value is None:
        length = 0
    else:
        length = 1
    return {"redacted": True, "length": length}


def _is_redaction_marker(value: Any) -> bool:
    return isinstance(value, dict) and value.get("redacted") is True


def _normalized_redaction_marker(value: dict[str, Any]) -> dict[str, Any]:
    length = value.get("length", value.get("size_bytes", value.get("items", 0)))
    if not isinstance(length, int) or isinstance(length, bool) or length < 0:
        length = 0
    return {"redacted": True, "length": length}


def _provider_trace_metadata(
    normalized_action: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    metadata = normalized_action.get("metadata")
    if not isinstance(metadata, dict):
        return None, []
    provider_action = metadata.pop(PROVIDER_ACTION_METADATA_KEY, None)
    raw_redactions = metadata.pop(PROVIDER_ACTION_REDACTIONS_METADATA_KEY, [])
    if not metadata:
        normalized_action["metadata"] = {}
    redactions = [
        f"provider_action.{item}" for item in raw_redactions if isinstance(item, str) and item
    ]
    if isinstance(provider_action, dict):
        provider_action, inferred_redactions = _redact_action_payload(
            provider_action, path="provider_action"
        )
        redactions.extend(inferred_redactions)
        return provider_action if isinstance(provider_action, dict) else None, redactions
    return None, redactions
