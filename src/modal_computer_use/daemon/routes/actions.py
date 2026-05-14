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

from modal_computer_use.actions import is_supported_key
from modal_computer_use.adapters.provenance import (
    PROVIDER_ACTION_METADATA_KEY,
    PROVIDER_ACTION_REDACTIONS_METADATA_KEY,
)
from modal_computer_use.daemon import budgets
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.routes.screenshots import enforce_screenshot_options_pixels
from modal_computer_use.daemon.routes.validation import ensure_desktop_ready
from modal_computer_use.errors import BudgetExceededError
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
from modal_computer_use.redaction import sanitize_text
from modal_computer_use.tracing import TraceWriter

router = APIRouter(prefix="/v1/actions")
logger = logging.getLogger("modal_computer_use.daemon.actions")


@router.post("/validate")
async def validate(payload: ActionBatchRequest, request: Request) -> ValidationResult:
    errors = _validate_batch_request(payload, request)
    return ValidationResult(ok=not errors, errors=errors)


@router.post("/run")
async def run(
    payload: ActionBatchRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ActionBatchResult:
    effective_idempotency_key = _effective_idempotency_key(payload, idempotency_key)
    request_fingerprint = _request_fingerprint(payload)
    action_count = _count_action_tree(payload.actions)
    if action_count > request.app.state.settings.max_batch_actions:
        raise DaemonError(
            "batch exceeds max_batch_actions",
            status_code=413,
            code="batch_too_large",
            details={
                "max_batch_actions": request.app.state.settings.max_batch_actions,
                "action_count": action_count,
            },
        )
    call_id = payload.call_id or f"call_{uuid.uuid4().hex[:12]}"
    validation_errors = _validate_batch_request(payload, request)
    if validation_errors:
        raise DaemonError(
            "action validation failed",
            status_code=422,
            code="action_validation_failed",
            details={"errors": validation_errors},
        )
    await ensure_desktop_ready(request)
    batch_start = time.perf_counter()
    results: list[ActionItemResult] = []
    screenshot = None
    batch_timed_out = False
    screenshot_after_blocked = False
    async with request.app.state.input_lock:
        cache = request.app.state.idempotency_cache
        _prune_idempotency_cache(request)
        if effective_idempotency_key and effective_idempotency_key in cache:
            entry = cache[effective_idempotency_key]
            if entry["fingerprint"] != request_fingerprint:
                raise DaemonError(
                    "idempotency key was already used with a different request body",
                    status_code=409,
                    code="idempotency_key_conflict",
                )
            cache.move_to_end(effective_idempotency_key)
            return ActionBatchResult.model_validate(entry["result"])

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
                    screenshot_after_blocked = True
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
                with request.app.state.tracer.span(
                    "daemon.action",
                    {
                        "action.type": action.type,
                        "action.sequence": index,
                        "call_id": call_id,
                    },
                ):
                    output = await asyncio.wait_for(
                        _execute_action(
                            action,
                            request,
                            call_id=call_id,
                            payload=payload,
                            batch_deadline=batch_deadline,
                        ),
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
                if item.ok:
                    budgets.touch_activity(request)
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
                if _uses_post_action_delay(action) and (
                    payload.screenshot_after or index < len(payload.actions) - 1
                ):
                    await _post_action_delay(request, batch_deadline)
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
                error_code = _exception_code(exc)
                if error_code in {"budget_exceeded", "rate_limited"}:
                    screenshot_after_blocked = True
                error = _action_error_message(action, exc)
                item = ActionItemResult(
                    index=index,
                    type=action.type,
                    ok=False,
                    elapsed_ms=elapsed_ms,
                    error_code=error_code,
                    error=error,
                    output=_exception_output(exc, action, request=request),
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
                if (
                    payload.continue_on_error
                    and _uses_post_action_delay(action)
                    and index < len(payload.actions) - 1
                ):
                    await _post_action_delay(request, batch_deadline)
                if not payload.continue_on_error:
                    break
        if payload.screenshot_after and not batch_timed_out and not screenshot_after_blocked:
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
                    enforce_screenshot_options_pixels(
                        request,
                        source_width=request.app.state.backend.width,
                        source_height=request.app.state.backend.height,
                        scale=options.scale,
                    )
                    screenshot_budget_error = budgets.screenshot_reservation_error(request)
                    if screenshot_budget_error is not None:
                        raise screenshot_budget_error
                    screenshot = await asyncio.wait_for(
                        request.app.state.backend.screenshot(
                            options,
                            artifact_store=request.app.state.artifacts,
                            call_id=call_id,
                            retention_class="trace",
                        ),
                        timeout=timeout_seconds,
                    )
                    budgets.reserve_screenshot(request)
                    budgets.enforce(request, "screenshots", "artifacts")
                    budgets.touch_activity(request)
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
                    error_code = _exception_code(exc)
                    failed_screenshot = screenshot
                    screenshot = None
                    item = ActionItemResult(
                        index=len(results),
                        type="screenshot_after",
                        ok=False,
                        elapsed_ms=elapsed_ms,
                        error_code=error_code,
                        error=sanitize_text(str(exc)),
                        output=_exception_output(exc, request=request),
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
        if effective_idempotency_key:
            cache[effective_idempotency_key] = {
                "fingerprint": request_fingerprint,
                "created_at": time.monotonic(),
                "result": result.model_dump(mode="json"),
            }
            _prune_idempotency_cache(request)
        return result


def _effective_idempotency_key(
    payload: ActionBatchRequest, header_key: str | None
) -> str | None:
    if header_key and payload.idempotency_key and header_key != payload.idempotency_key:
        raise DaemonError(
            "Idempotency-Key header and body idempotency_key differ",
            status_code=409,
            code="idempotency_key_conflict",
        )
    return header_key or payload.idempotency_key


def _request_fingerprint(payload: ActionBatchRequest) -> str:
    encoded = json.dumps(
        payload.model_dump(mode="json", exclude={"idempotency_key"}),
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
    for action_path, action in _iter_timeout_actions(payload.actions):
        timeout_ms = getattr(action, "timeout_ms", None)
        if timeout_ms is not None and timeout_ms > max_action_timeout_ms:
            errors.append(
                f"{action_path} timeout_ms {timeout_ms} exceeds configured maximum "
                f"{max_action_timeout_ms}"
            )
    return errors


def _validate_batch_request(payload: ActionBatchRequest, request: Request) -> list[str]:
    errors = _validate_actions(
        payload.actions,
        width=request.app.state.backend.width,
        height=request.app.state.backend.height,
    )
    action_count = _count_action_tree(payload.actions)
    if action_count > request.app.state.settings.max_batch_actions:
        errors.append(
            "batch exceeds max_batch_actions "
            f"{request.app.state.settings.max_batch_actions} "
            f"with {action_count} total actions"
        )
    errors.extend(
        _validate_action_timeouts(payload, request.app.state.settings.max_action_timeout_ms)
    )
    errors.extend(_validate_screenshot_pixel_budget(payload, request))
    return errors


def _validate_screenshot_pixel_budget(
    payload: ActionBatchRequest, request: Request
) -> list[str]:
    errors: list[str] = []
    backend = request.app.state.backend
    for action_path, action in _iter_action_tree(payload.actions):
        if isinstance(action, ScreenshotAction):
            options = action.options or ScreenshotOptions()
            errors.extend(
                _screenshot_pixel_errors(
                    request,
                    source_width=backend.width,
                    source_height=backend.height,
                    scale=options.scale,
                    label=action_path,
                )
            )
        elif isinstance(action, ZoomAction):
            options = action.options or ScreenshotOptions(scale=action.scale)
            errors.extend(
                _screenshot_pixel_errors(
                    request,
                    source_width=action.region.width,
                    source_height=action.region.height,
                    scale=options.scale,
                    label=action_path,
                )
            )
    if payload.screenshot_after:
        options = payload.screenshot_options or ScreenshotOptions()
        errors.extend(
            _screenshot_pixel_errors(
                request,
                source_width=backend.width,
                source_height=backend.height,
                scale=options.scale,
                label="screenshot_after",
            )
        )
    return errors


def _screenshot_pixel_errors(
    request: Request,
    *,
    source_width: int,
    source_height: int,
    scale: float,
    label: str,
) -> list[str]:
    output_pixels = round(source_width * scale) * round(source_height * scale)
    max_pixels = request.app.state.settings.screenshot_max_pixels
    if output_pixels <= max_pixels:
        return []
    return [
        f"{label} screenshot output {output_pixels} pixels exceeds "
        f"max screenshot pixels {max_pixels}"
    ]


def _iter_timeout_actions(actions: list[Any], *, path: str = "actions"):
    yield from _iter_action_tree(actions, path=path)


def _iter_action_tree(actions: list[Any], *, path: str = "actions"):
    for index, action in enumerate(actions):
        action_path = f"{path}[{index}]"
        yield action_path, action
        if isinstance(action, HoldKeyAction) and action.actions:
            nested = []
            for nested_action in action.actions:
                with suppress(Exception):
                    nested.append(parse_action(nested_action))
            yield from _iter_action_tree(nested, path=f"{action_path}.actions")


def _count_action_tree(actions: list[Any]) -> int:
    count = 0
    for action in actions:
        count += 1
        if isinstance(action, HoldKeyAction) and action.actions:
            nested = []
            for nested_action in action.actions:
                with suppress(Exception):
                    nested.append(parse_action(nested_action))
            count += _count_action_tree(nested)
    return count


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


def _uses_post_action_delay(action: Any) -> bool:
    return not isinstance(action, WaitAction | ScreenshotAction | ZoomAction | CursorPositionAction)


async def _post_action_delay(request: Request, batch_deadline: float) -> None:
    delay_ms = request.app.state.settings.post_action_delay_ms
    if delay_ms <= 0:
        return
    remaining_seconds = _remaining_seconds(batch_deadline)
    if remaining_seconds <= 0:
        return
    await asyncio.sleep(min(delay_ms / 1000, remaining_seconds))


def _reserve_action_budget(
    request: Request,
    *,
    index: int,
    action_type: str,
) -> ActionItemResult | None:
    error = budgets.action_reservation_error(request)
    if error is not None:
        return ActionItemResult(
            index=index,
            type=action_type,
            ok=False,
            elapsed_ms=0,
            error_code=error.code,
            error=error.message,
            output={"code": error.code, **error.details},
        )
    budgets.reserve_action(request)
    return None


def _exception_code(exc: Exception) -> str:
    if isinstance(exc, DaemonError):
        return exc.code
    if isinstance(exc, BudgetExceededError):
        return "budget_exceeded"
    return "action_failed"


def _exception_output(
    exc: Exception, action: Any | None = None, *, request: Request | None = None
) -> dict[str, Any]:
    if isinstance(exc, DaemonError):
        output: dict[str, Any] = {"code": exc.code}
        output.update(exc.details)
        if isinstance(action, TypeAction):
            return _redact_type_payload(output, action)
        return output
    if isinstance(exc, BudgetExceededError):
        output = {"code": "budget_exceeded"}
        if request is not None:
            output["budgets"] = budgets.snapshot(request)
        return output
    return {}


def _action_error_message(action: Any, exc: Exception) -> str:
    message = sanitize_text(str(exc))
    for text in _typed_texts_for_action(action):
        message = message.replace(text, "[redacted typed text]")
    return message


def _typed_texts_for_action(action: Any) -> list[str]:
    if isinstance(action, TypeAction):
        return [action.text]
    if isinstance(action, HoldKeyAction) and action.actions:
        texts: list[str] = []
        for nested_action in action.actions:
            with suppress(Exception):
                texts.extend(_typed_texts_for_action(parse_action(nested_action)))
        return texts
    return []


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


async def _execute_action(
    action: Any,
    request: Request,
    *,
    call_id: str,
    payload: ActionBatchRequest | None = None,
    batch_deadline: float | None = None,
) -> dict[str, Any]:
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
    if isinstance(action, MouseDownAction | MouseUpAction):
        if action.type == "mouse_down":
            result = await backend.mouse_down(action.button, action.x, action.y)
            return result.output
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
                if _counts_against_action_budget(nested_action):
                    budgets.reserve_action(request)
                nested_start = time.perf_counter()
                nested_call = _execute_action(
                    nested_action,
                    request,
                    call_id=call_id,
                    payload=payload,
                    batch_deadline=batch_deadline,
                )
                if payload is not None:
                    nested_timeout_seconds = (
                        _effective_action_timeout_ms(nested_action, payload, request) / 1000
                    )
                    if batch_deadline is not None:
                        nested_timeout_seconds = min(
                            nested_timeout_seconds,
                            _remaining_seconds(batch_deadline),
                        )
                    nested_output = await asyncio.wait_for(
                        nested_call, timeout=nested_timeout_seconds
                    )
                else:
                    nested_output = await nested_call
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
        enforce_screenshot_options_pixels(
            request,
            source_width=request.app.state.backend.width,
            source_height=request.app.state.backend.height,
            scale=options.scale,
        )
        error = budgets.screenshot_reservation_error(request)
        if error is not None:
            raise error
        shot = await backend.screenshot(
            options, artifact_store=request.app.state.artifacts, call_id=call_id
        )
        budgets.reserve_screenshot(request)
        return shot.model_dump(mode="json")
    if isinstance(action, ZoomAction):
        options = action.options or ScreenshotOptions(scale=action.scale, show_cursor=True)
        options.scale = action.scale
        region = Region.model_validate(action.region)
        enforce_screenshot_options_pixels(
            request,
            source_width=region.width,
            source_height=region.height,
            scale=options.scale,
        )
        error = budgets.screenshot_reservation_error(request)
        if error is not None:
            raise error
        shot = await backend.screenshot(
            options,
            region=region,
            artifact_store=request.app.state.artifacts,
            call_id=call_id,
        )
        budgets.reserve_screenshot(request)
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


def _validate_actions(
    actions: list[Any],
    *,
    width: int | None,
    height: int | None,
    path: str = "actions",
) -> list[str]:
    errors: list[str] = []
    for index, action in enumerate(actions):
        action_path = f"{path}[{index}]"
        key_errors = _key_validation_errors(action, field=action_path)
        errors.extend(key_errors)
        for point in _action_points(action):
            if width is not None and point.x >= width:
                errors.append(
                    f"{action_path} x coordinate {point.x} exceeds desktop width {width}"
                )
            if height is not None and point.y >= height:
                errors.append(
                    f"{action_path} y coordinate {point.y} exceeds desktop height {height}"
                )
        region = getattr(action, "region", None)
        if (
            isinstance(region, Region)
            and width is not None
            and height is not None
            and (region.right > width or region.bottom > height)
        ):
            errors.append(f"{action_path} region extends beyond desktop geometry")
        if isinstance(action, HoldKeyAction) and action.actions:
            parsed_nested = []
            for nested_index, nested_action in enumerate(action.actions):
                try:
                    parsed = parse_action(nested_action)
                except Exception as exc:
                    errors.append(
                        f"{action_path}.actions[{nested_index}] is invalid: {exc}"
                    )
                    continue
                parsed_nested.append(parsed)
            errors.extend(
                _validate_actions(
                    parsed_nested,
                    width=width,
                    height=height,
                    path=f"{action_path}.actions",
                )
            )
    return errors


def _key_validation_errors(action: Any, *, field: str) -> list[str]:
    keys: list[tuple[str, str]] = []
    if isinstance(action, KeyPressAction):
        keys.append((f"{field}.key", action.key))
        keys.extend(
            (f"{field}.modifiers[{index}]", key)
            for index, key in enumerate(action.modifiers)
        )
    elif isinstance(action, HotkeyAction):
        keys.extend((f"{field}.keys[{index}]", key) for index, key in enumerate(action.keys))
    elif isinstance(action, HoldKeyAction):
        keys.append((f"{field}.key", action.key))
    elif isinstance(action, ClickAction | DoubleClickAction | TripleClickAction | DragAction):
        keys.extend(
            (f"{field}.modifiers[{index}]", key)
            for index, key in enumerate(action.modifiers)
        )
    return [
        f"{path} is not a supported key: {key}"
        for path, key in keys
        if not is_supported_key(key)
    ]


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
    normalized, redactions = _redacted_action_and_paths(action)
    provider_action, provider_redactions = _provider_trace_metadata(normalized)
    redactions.extend(provider_redactions)
    redactions = list(dict.fromkeys(redactions))
    trace_result, result_redactions = _trace_result(result)
    redactions.extend(result_redactions)
    redactions = list(dict.fromkeys(redactions))
    writer.append(
        TraceEntry(
            ts=datetime.now(UTC),
            run_id=payload.run_id or request.app.state.settings.run_id,
            call_id=call_id,
            sequence=payload.sequence if payload.sequence is not None else sequence,
            source=payload.source,
            provider_action=provider_action,
            normalized_action=normalized,
            result=trace_result,
            elapsed_ms=result.elapsed_ms,
            screenshot_after_uri=_screenshot_uri(result),
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
            redacted_item, child_redactions = _redact_trace_result_payload(
                item, path=item_path
            )
            redacted[key] = redacted_item
            redactions.extend(child_redactions)
        return redacted, redactions
    if isinstance(value, list):
        redacted_items = []
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]" if path else f"[{index}]"
            redacted_item, child_redactions = _redact_trace_result_payload(
                item, path=item_path
            )
            redacted_items.append(redacted_item)
            redactions.extend(child_redactions)
        return redacted_items, redactions
    if isinstance(value, str):
        sanitized = sanitize_text(value)
        if sanitized != value:
            return sanitized, [path] if path else []
    return value, []


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
    return _redacted_action_and_paths(action)[0]


def _redacted_action_and_paths(action: Any) -> tuple[dict[str, Any], list[str]]:
    data = action.model_dump(mode="json")
    redacted, redactions = _redact_action_payload(data)
    return redacted if isinstance(redacted, dict) else data, redactions


def _redact_action_payload(value: Any, *, path: str = "") -> tuple[Any, list[str]]:
    redactions: list[str] = []
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        is_type_action = value.get("type") == "type" and isinstance(value.get("text"), str)
        for key, item in value.items():
            item_path = f"{path}.{key}" if path else key
            if key == PROVIDER_ACTION_METADATA_KEY:
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
            redacted_item, child_redactions = _redact_action_payload(item, path=item_path)
            redacted[key] = redacted_item
            redactions.extend(child_redactions)
        return redacted, redactions
    if isinstance(value, list):
        redacted_items = []
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]" if path else f"[{index}]"
            redacted_item, child_redactions = _redact_action_payload(item, path=item_path)
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
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
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
        return value
    marker: dict[str, Any] = {"redacted": True}
    if isinstance(value, str):
        marker["length"] = len(value)
    elif isinstance(value, bytes):
        marker["size_bytes"] = len(value)
    elif isinstance(value, list | tuple | dict):
        marker["items"] = len(value)
    return marker


def _is_redaction_marker(value: Any) -> bool:
    return isinstance(value, dict) and value.get("redacted") is True


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
    if isinstance(provider_action, dict):
        provider_action, inferred_redactions = _redact_action_payload(
            provider_action, path="provider_action"
        )
        redactions.extend(inferred_redactions)
        return provider_action if isinstance(provider_action, dict) else None, redactions
    return None, redactions
