from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Header, Request

from modal_computer_use.adapters.provenance import (
    PROVIDER_ACTION_METADATA_KEY,
    PROVIDER_ACTION_REDACTIONS_METADATA_KEY,
)
from modal_computer_use.daemon import budgets
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.models import (
    ActionBatchRequest,
    ActionBatchResult,
    ActionBatchTiming,
    ActionItemResult,
    ClickAction,
    CursorPositionAction,
    DoubleClickAction,
    DragAction,
    HoldKeyAction,
    HotkeyAction,
    KeyPressAction,
    MouseDownAction,
    MouseUpAction,
    MoveAction,
    Point,
    Region,
    ReleaseAllAction,
    ScreenshotAction,
    ScreenshotOptions,
    ScrollAction,
    TraceEntry,
    TripleClickAction,
    TypeAction,
    ValidationResult,
    WaitAction,
    ZoomAction,
    parse_action,
)
from modal_computer_use.tracing import TraceWriter

router = APIRouter(prefix="/v1/actions")
logger = logging.getLogger("modal_computer_use.daemon.actions")


@router.post("/validate")
async def validate(payload: ActionBatchRequest) -> ValidationResult:
    errors = _validate_actions(payload.actions, width=None, height=None)
    return ValidationResult(ok=not errors, errors=errors)


@router.post("/run")
async def run(
    payload: ActionBatchRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ActionBatchResult:
    cache = request.app.state.idempotency_cache
    request_fingerprint = _request_fingerprint(payload)
    _prune_idempotency_cache(request)
    if idempotency_key and idempotency_key in cache:
        entry = cache[idempotency_key]
        if entry["fingerprint"] != request_fingerprint:
            raise DaemonError(
                "idempotency key was already used with a different request body",
                status_code=409,
                code="idempotency_key_conflict",
            )
        cache.move_to_end(idempotency_key)
        return ActionBatchResult.model_validate(entry["result"])
    if len(payload.actions) > request.app.state.settings.max_batch_actions:
        raise DaemonError(
            "batch exceeds max_batch_actions",
            status_code=413,
            code="batch_too_large",
            details={"max_batch_actions": request.app.state.settings.max_batch_actions},
        )
    call_id = payload.call_id or f"call_{uuid.uuid4().hex[:12]}"
    validation_errors = _validate_actions(
        payload.actions,
        width=request.app.state.backend.width,
        height=request.app.state.backend.height,
    )
    if validation_errors:
        raise DaemonError(
            "action validation failed",
            status_code=422,
            code="action_validation_failed",
            details={"errors": validation_errors},
        )
    timeout_errors = _validate_action_timeouts(
        payload, request.app.state.settings.max_action_timeout_ms
    )
    if timeout_errors:
        raise DaemonError(
            "action timeout validation failed",
            status_code=422,
            code="action_validation_failed",
            details={"errors": timeout_errors},
        )
    batch_start = time.perf_counter()
    results: list[ActionItemResult] = []
    screenshot = None
    batch_timed_out = False
    async with request.app.state.input_lock:
        batch_timeout_ms = request.app.state.settings.max_batch_duration_ms
        batch_deadline = time.perf_counter() + (batch_timeout_ms / 1000)
        for index, action in enumerate(payload.actions):
            remaining_batch_seconds = _remaining_seconds(batch_deadline)
            if remaining_batch_seconds <= 0:
                item = _batch_timeout_result(
                    index=index,
                    action_type=action.type,
                    batch_timeout_ms=batch_timeout_ms,
                    elapsed_ms=0,
                )
                results.append(item)
                batch_timed_out = True
                _append_trace(request, payload, action, item, call_id=call_id, sequence=index)
                logger.info(
                    "action failed",
                    extra={
                        "extra": {
                            "call_id": call_id,
                            "sequence": index,
                            "action": _redacted_action(action),
                            "ok": False,
                            "elapsed_ms": 0,
                            "error_code": "timeout",
                            "timeout_scope": "batch",
                        }
                    },
                )
                break
            start = time.perf_counter()
            timeout_ms = _effective_action_timeout_ms(action, payload, request)
            timeout_seconds = min(timeout_ms / 1000, remaining_batch_seconds)
            timeout_scope = "batch" if timeout_seconds < (timeout_ms / 1000) else "action"
            if _counts_against_action_budget(action):
                budget_item = _reserve_action_budget(request, index=index, action_type=action.type)
                if budget_item is not None:
                    results.append(budget_item)
                    _append_trace(
                        request, payload, action, budget_item, call_id=call_id, sequence=index
                    )
                    logger.info(
                        "action failed",
                        extra={
                            "extra": {
                                "call_id": call_id,
                                "sequence": index,
                                "action": _redacted_action(action),
                                "ok": False,
                                "elapsed_ms": 0,
                                "error_code": "budget_exceeded",
                            }
                        },
                    )
                    if not payload.continue_on_error:
                        break
                    continue
            try:
                output = await asyncio.wait_for(
                    _execute_action(action, request, call_id=call_id),
                    timeout=timeout_seconds,
                )
                elapsed_ms = (time.perf_counter() - start) * 1000
                budget_kinds = _budget_kinds_for_action(action)
                if budget_kinds:
                    budgets.enforce(request, *budget_kinds)
                item = ActionItemResult(
                    index=index,
                    type=action.type,
                    ok=True,
                    elapsed_ms=elapsed_ms,
                    output=output or {},
                )
                results.append(item)
                _append_trace(request, payload, action, item, call_id=call_id, sequence=index)
                logger.info(
                    "action executed",
                    extra={
                        "extra": {
                            "call_id": call_id,
                            "sequence": index,
                            "action": _redacted_action(action),
                            "ok": True,
                            "elapsed_ms": elapsed_ms,
                        }
                    },
                )
            except TimeoutError:
                elapsed_ms = (time.perf_counter() - start) * 1000
                with suppress(Exception):
                    await request.app.state.backend.release_all()
                effective_timeout_ms = (
                    batch_timeout_ms if timeout_scope == "batch" else timeout_ms
                )
                item = ActionItemResult(
                    index=index,
                    type=action.type,
                    ok=False,
                    elapsed_ms=elapsed_ms,
                    error_code="timeout",
                    error=f"{timeout_scope} timed out after {effective_timeout_ms} ms",
                    output={
                        "code": "timeout",
                        "timeout_ms": effective_timeout_ms,
                        "scope": timeout_scope,
                    },
                )
                results.append(item)
                batch_timed_out = timeout_scope == "batch"
                _append_trace(request, payload, action, item, call_id=call_id, sequence=index)
                logger.info(
                    "action failed",
                    extra={
                        "extra": {
                            "call_id": call_id,
                            "sequence": index,
                            "action": _redacted_action(action),
                            "ok": False,
                            "elapsed_ms": elapsed_ms,
                            "error_code": "timeout",
                            "timeout_scope": timeout_scope,
                        }
                    },
                )
                if batch_timed_out or not payload.continue_on_error:
                    break
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - start) * 1000
                with suppress(Exception):
                    await request.app.state.backend.release_all()
                error_code = exc.code if isinstance(exc, DaemonError) else "action_failed"
                error = _action_error_message(action, exc)
                item = ActionItemResult(
                    index=index,
                    type=action.type,
                    ok=False,
                    elapsed_ms=elapsed_ms,
                    error_code=error_code,
                    error=error,
                    output=_exception_output(exc, action),
                )
                results.append(item)
                _append_trace(request, payload, action, item, call_id=call_id, sequence=index)
                logger.info(
                    "action failed",
                    extra={
                        "extra": {
                            "call_id": call_id,
                            "sequence": index,
                            "action": _redacted_action(action),
                            "ok": False,
                            "elapsed_ms": elapsed_ms,
                            "error_code": error_code,
                        }
                    },
                )
                if not payload.continue_on_error:
                    break
        if payload.screenshot_after and not batch_timed_out:
            options = payload.screenshot_options or ScreenshotOptions()
            start = time.perf_counter()
            timeout_ms = _effective_screenshot_after_timeout_ms(payload, request)
            remaining_batch_seconds = _remaining_seconds(batch_deadline)
            if remaining_batch_seconds <= 0:
                item = _screenshot_after_timeout_result(
                    index=len(results),
                    elapsed_ms=0,
                    timeout_ms=batch_timeout_ms,
                    scope="batch",
                )
                results.append(item)
                _append_screenshot_after_trace(request, payload, None, item, call_id=call_id)
            else:
                timeout_seconds = min(timeout_ms / 1000, remaining_batch_seconds)
                timeout_scope = "batch" if timeout_seconds < (timeout_ms / 1000) else "action"
                try:
                    screenshot = await asyncio.wait_for(
                        request.app.state.backend.screenshot(
                            options,
                            artifact_store=request.app.state.artifacts,
                            call_id=call_id,
                            retention_class="trace",
                        ),
                        timeout=timeout_seconds,
                    )
                    request.app.state.screenshot_count += 1
                    budgets.enforce(request, "screenshots", "artifacts")
                    _append_screenshot_after_trace(
                        request, payload, screenshot, None, call_id=call_id
                    )
                except TimeoutError:
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    effective_timeout_ms = (
                        batch_timeout_ms if timeout_scope == "batch" else timeout_ms
                    )
                    item = _screenshot_after_timeout_result(
                        index=len(results),
                        elapsed_ms=elapsed_ms,
                        timeout_ms=effective_timeout_ms,
                        scope=timeout_scope,
                    )
                    results.append(item)
                    _append_screenshot_after_trace(request, payload, None, item, call_id=call_id)
                except Exception as exc:
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    error_code = exc.code if isinstance(exc, DaemonError) else "action_failed"
                    failed_screenshot = screenshot
                    screenshot = None
                    item = ActionItemResult(
                        index=len(results),
                        type="screenshot_after",
                        ok=False,
                        elapsed_ms=elapsed_ms,
                        error_code=error_code,
                        error=str(exc),
                        output=_exception_output(exc),
                    )
                    results.append(item)
                    _append_screenshot_after_trace(
                        request, payload, failed_screenshot, item, call_id=call_id
                    )
    result = ActionBatchResult(
        ok=all(item.ok for item in results),
        call_id=call_id,
        results=results,
        screenshot=screenshot,
        timing=ActionBatchTiming(daemon_ms=(time.perf_counter() - batch_start) * 1000),
    )
    if idempotency_key:
        cache[idempotency_key] = {
            "fingerprint": request_fingerprint,
            "created_at": time.monotonic(),
            "result": result.model_dump(mode="json"),
        }
        _prune_idempotency_cache(request)
    return result


def _request_fingerprint(payload: ActionBatchRequest) -> str:
    encoded = json.dumps(
        payload.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prune_idempotency_cache(request: Request) -> None:
    cache = request.app.state.idempotency_cache
    settings = request.app.state.settings
    now = time.monotonic()
    ttl_seconds = settings.idempotency_cache_ttl_seconds
    if ttl_seconds > 0:
        for key in list(cache.keys()):
            if now - cache[key]["created_at"] <= ttl_seconds:
                continue
            cache.pop(key, None)
    max_entries = settings.idempotency_cache_max_entries
    while max_entries > 0 and len(cache) > max_entries:
        cache.popitem(last=False)


def _validate_action_timeouts(
    payload: ActionBatchRequest, max_action_timeout_ms: int
) -> list[str]:
    errors: list[str] = []
    if payload.max_action_timeout_ms is not None and (
        payload.max_action_timeout_ms > max_action_timeout_ms
    ):
        errors.append(
            "max_action_timeout_ms "
            f"{payload.max_action_timeout_ms} exceeds configured maximum {max_action_timeout_ms}"
        )
    for index, action in enumerate(payload.actions):
        timeout_ms = getattr(action, "timeout_ms", None)
        if timeout_ms is not None and timeout_ms > max_action_timeout_ms:
            errors.append(
                f"actions[{index}] timeout_ms {timeout_ms} exceeds configured maximum "
                f"{max_action_timeout_ms}"
            )
    return errors


def _effective_action_timeout_ms(
    action: Any, payload: ActionBatchRequest, request: Request
) -> int:
    if action.timeout_ms is not None:
        return action.timeout_ms
    if payload.max_action_timeout_ms is not None:
        return payload.max_action_timeout_ms
    return request.app.state.settings.default_action_timeout_ms


def _effective_screenshot_after_timeout_ms(
    payload: ActionBatchRequest, request: Request
) -> int:
    if payload.max_action_timeout_ms is not None:
        return payload.max_action_timeout_ms
    return request.app.state.settings.default_action_timeout_ms


def _remaining_seconds(deadline: float) -> float:
    return max(0, deadline - time.perf_counter())


def _counts_against_action_budget(action: Any) -> bool:
    return not isinstance(action, ScreenshotAction | ZoomAction | CursorPositionAction)


def _budget_kinds_for_action(action: Any) -> tuple[budgets.BudgetKind, ...]:
    if isinstance(action, ScreenshotAction | ZoomAction):
        return ("screenshots", "artifacts")
    return ()


def _reserve_action_budget(
    request: Request,
    *,
    index: int,
    action_type: str,
) -> ActionItemResult | None:
    max_actions = request.app.state.settings.max_actions
    if max_actions is not None and request.app.state.action_count >= max_actions:
        return ActionItemResult(
            index=index,
            type=action_type,
            ok=False,
            elapsed_ms=0,
            error_code="budget_exceeded",
            error="action budget exceeded",
            output={"code": "budget_exceeded", "budgets": budgets.snapshot(request)},
        )
    request.app.state.action_count += 1
    return None


def _exception_output(exc: Exception, action: Any | None = None) -> dict[str, Any]:
    if isinstance(exc, DaemonError):
        output: dict[str, Any] = {"code": exc.code}
        output.update(exc.details)
        if isinstance(action, TypeAction):
            return _redact_type_payload(output, action)
        return output
    return {}


def _action_error_message(action: Any, exc: Exception) -> str:
    message = str(exc)
    if isinstance(action, TypeAction):
        message = message.replace(action.text, "[redacted typed text]")
    return message


def _redact_type_payload(value: Any, action: TypeAction) -> Any:
    if isinstance(value, str):
        return value.replace(action.text, "[redacted typed text]")
    if isinstance(value, list):
        return [_redact_type_payload(item, action) for item in value]
    if isinstance(value, dict):
        return {
            ("redacted_text" if key == "text" else key): _redact_type_payload(item, action)
            for key, item in value.items()
        }
    return value


def _batch_timeout_result(
    *,
    index: int,
    action_type: str,
    batch_timeout_ms: int,
    elapsed_ms: float,
) -> ActionItemResult:
    return ActionItemResult(
        index=index,
        type=action_type,
        ok=False,
        elapsed_ms=elapsed_ms,
        error_code="timeout",
        error=f"batch timed out after {batch_timeout_ms} ms",
        output={"code": "timeout", "timeout_ms": batch_timeout_ms, "scope": "batch"},
    )


def _screenshot_after_timeout_result(
    *,
    index: int,
    elapsed_ms: float,
    timeout_ms: int,
    scope: str,
) -> ActionItemResult:
    return ActionItemResult(
        index=index,
        type="screenshot_after",
        ok=False,
        elapsed_ms=elapsed_ms,
        error_code="timeout",
        error=f"{scope} timed out after {timeout_ms} ms",
        output={"code": "timeout", "timeout_ms": timeout_ms, "scope": scope},
    )


async def _execute_action(action: Any, request: Request, *, call_id: str) -> dict[str, Any]:
    backend = request.app.state.backend
    if isinstance(action, MoveAction):
        point = await backend.mouse_move(action.x, action.y)
        return point.model_dump()
    if isinstance(action, (ClickAction, DoubleClickAction, TripleClickAction)):
        count = 1
        if isinstance(action, DoubleClickAction):
            count = 2
        if isinstance(action, TripleClickAction):
            count = 3
        point = await backend.mouse_click(
            action.x,
            action.y,
            button=action.button,
            count=count,
            modifiers=action.modifiers,
        )
        return point.model_dump()
    if isinstance(action, DragAction):
        start = (
            Point(x=action.start_x, y=action.start_y)
            if action.start_x is not None and action.start_y is not None
            else None
        )
        end = (
            Point(x=action.end_x, y=action.end_y)
            if action.end_x is not None and action.end_y is not None
            else None
        )
        point = await backend.mouse_drag(
            start=start,
            end=end,
            path=action.path,
            button=action.button,
            duration_ms=action.duration_ms,
            modifiers=action.modifiers,
        )
        return point.model_dump()
    if isinstance(action, ScrollAction):
        result = await backend.mouse_scroll(
            action.direction,
            action.amount,
            x=action.x,
            y=action.y,
        )
        return result.output
    if isinstance(action, MouseDownAction):
        result = await backend.mouse_down(action.button, action.x, action.y)
        return result.output
    if isinstance(action, MouseUpAction):
        result = await backend.mouse_up(action.button, action.x, action.y)
        return result.output
    if isinstance(action, TypeAction):
        result = await backend.keyboard_type(action.text, action.delay_ms, action.method)
        return result.output
    if isinstance(action, KeyPressAction):
        result = await backend.keyboard_press(
            action.key,
            modifiers=action.modifiers,
            duration_ms=action.duration_ms,
        )
        return result.output
    if isinstance(action, HotkeyAction):
        result = await backend.keyboard_hotkey(action.keys, duration_ms=action.duration_ms)
        return result.output
    if isinstance(action, HoldKeyAction):
        await backend.key_down(action.key)
        try:
            nested_results: list[dict[str, Any]] = []
            for nested_action in _nested_hold_actions(action):
                nested_start = time.perf_counter()
                nested_output = await _execute_action(nested_action, request, call_id=call_id)
                nested_results.append(
                    {
                        "type": nested_action.type,
                        "ok": True,
                        "elapsed_ms": (time.perf_counter() - nested_start) * 1000,
                        "output": nested_output or {},
                    }
                )
            if action.duration_ms is not None:
                await asyncio.sleep(action.duration_ms / 1000)
        finally:
            await backend.key_up(action.key)
        return {"actions": nested_results} if action.actions else {}
    if isinstance(action, WaitAction):
        await asyncio.sleep(action.duration_ms / 1000)
        return {"duration_ms": action.duration_ms}
    if isinstance(action, ScreenshotAction):
        options = action.options or ScreenshotOptions()
        shot = await backend.screenshot(
            options, artifact_store=request.app.state.artifacts, call_id=call_id
        )
        request.app.state.screenshot_count += 1
        return shot.model_dump(mode="json")
    if isinstance(action, ZoomAction):
        options = action.options or ScreenshotOptions(scale=action.scale, show_cursor=True)
        options.scale = action.scale
        shot = await backend.screenshot(
            options,
            region=Region.model_validate(action.region),
            artifact_store=request.app.state.artifacts,
            call_id=call_id,
        )
        request.app.state.screenshot_count += 1
        return shot.model_dump(mode="json")
    if isinstance(action, CursorPositionAction):
        point = await backend.mouse_position()
        return point.model_dump()
    if isinstance(action, ReleaseAllAction):
        result = await backend.release_all()
        return result.output
    raise DaemonError(
        f"unsupported action type: {getattr(action, 'type', None)}", code="unsupported_action"
    )


def _validate_actions(actions: list[Any], *, width: int | None, height: int | None) -> list[str]:
    errors: list[str] = []
    for index, action in enumerate(actions):
        for point in _action_points(action):
            if width is not None and point.x >= width:
                errors.append(
                    f"actions[{index}] x coordinate {point.x} exceeds desktop width {width}"
                )
            if height is not None and point.y >= height:
                errors.append(
                    f"actions[{index}] y coordinate {point.y} exceeds desktop height {height}"
                )
        region = getattr(action, "region", None)
        if (
            isinstance(region, Region)
            and width is not None
            and height is not None
            and (region.right > width or region.bottom > height)
        ):
            errors.append(f"actions[{index}] region extends beyond desktop geometry")
        if isinstance(action, HoldKeyAction) and action.actions:
            for nested_index, nested_action in enumerate(action.actions):
                try:
                    parsed = parse_action(nested_action)
                except Exception as exc:
                    errors.append(
                        f"actions[{index}].actions[{nested_index}] is invalid: {exc}"
                    )
                    continue
                for point in _action_points(parsed):
                    if width is not None and point.x >= width:
                        errors.append(
                            f"actions[{index}].actions[{nested_index}] x coordinate "
                            f"{point.x} exceeds desktop width {width}"
                        )
                    if height is not None and point.y >= height:
                        errors.append(
                            f"actions[{index}].actions[{nested_index}] y coordinate "
                            f"{point.y} exceeds desktop height {height}"
                        )
                nested_region = getattr(parsed, "region", None)
                if (
                    isinstance(nested_region, Region)
                    and width is not None
                    and height is not None
                    and (nested_region.right > width or nested_region.bottom > height)
                ):
                    errors.append(
                        f"actions[{index}].actions[{nested_index}] region extends beyond "
                        "desktop geometry"
                    )
    return errors


def _nested_hold_actions(action: HoldKeyAction) -> list[Any]:
    return [parse_action(item) for item in action.actions or []]


def _action_points(action: Any) -> list[Point]:
    points: list[Point] = []
    x = getattr(action, "x", None)
    y = getattr(action, "y", None)
    if isinstance(x, int) and isinstance(y, int):
        points.append(Point(x=x, y=y))
    for prefix in ("start", "end"):
        px = getattr(action, f"{prefix}_x", None)
        py = getattr(action, f"{prefix}_y", None)
        if isinstance(px, int) and isinstance(py, int):
            points.append(Point(x=px, y=py))
    path = getattr(action, "path", None)
    if path:
        points.extend(path)
    return points


def _append_trace(
    request: Request,
    payload: ActionBatchRequest,
    action: Any,
    result: ActionItemResult,
    *,
    call_id: str,
    sequence: int,
) -> None:
    if not request.app.state.settings.trace_actions:
        return
    writer = TraceWriter(request.app.state.settings.trace_dir / "actions.ndjson")
    normalized = _redacted_action(action)
    provider_action, provider_redactions = _provider_trace_metadata(normalized)
    redactions = ["text"] if isinstance(action, TypeAction) else []
    redactions.extend(provider_redactions)
    writer.append(
        TraceEntry(
            ts=datetime.now(UTC),
            run_id=payload.run_id or request.app.state.settings.run_id,
            call_id=call_id,
            sequence=payload.sequence if payload.sequence is not None else sequence,
            source=payload.source,
            provider_action=provider_action,
            normalized_action=normalized,
            result=result.model_dump(mode="json"),
            elapsed_ms=result.elapsed_ms,
            screenshot_after_uri=_screenshot_uri(result),
            coordinate_space=_coordinate_space(result),
            redactions=redactions,
            error=_trace_error(result),
        )
    )


def _append_screenshot_after_trace(
    request: Request,
    payload: ActionBatchRequest,
    screenshot: Any | None,
    result: ActionItemResult | None,
    *,
    call_id: str,
) -> None:
    if not request.app.state.settings.trace_actions:
        return
    writer = TraceWriter(request.app.state.settings.trace_dir / "actions.ndjson")
    writer.append(
        TraceEntry(
            ts=datetime.now(UTC),
            run_id=payload.run_id or request.app.state.settings.run_id,
            call_id=call_id,
            sequence=payload.sequence,
            source=payload.source,
            normalized_action={"type": "screenshot_after"},
            result=(
                result.model_dump(mode="json")
                if result is not None
                else {
                    "ok": True,
                    "format": screenshot.format,
                    "width": screenshot.width,
                    "height": screenshot.height,
                    "size_bytes": screenshot.size_bytes,
                    "artifact_uri": screenshot.artifact_uri,
                }
            ),
            screenshot_after_uri=screenshot.artifact_uri if screenshot is not None else None,
            coordinate_space=screenshot.coordinate_space if screenshot is not None else None,
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


def _screenshot_uri(result: ActionItemResult) -> str | None:
    output = result.output or {}
    uri = output.get("artifact_uri")
    return uri if isinstance(uri, str) else None


def _coordinate_space(result: ActionItemResult) -> Any:
    output = result.output or {}
    return output.get("coordinate_space")


def _redacted_action(action: Any) -> dict[str, Any]:
    data = action.model_dump(mode="json")
    if isinstance(action, TypeAction):
        text = action.text
        data["text"] = {"redacted": True, "length": len(text)}
    return data


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
        f"provider_action.{item}"
        for item in raw_redactions
        if isinstance(item, str) and item
    ]
    return provider_action if isinstance(provider_action, dict) else None, redactions
