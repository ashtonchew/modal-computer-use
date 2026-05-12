from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Header, Request

from modal_computer_use.daemon import budgets
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.models import (
    ActionBatchRequest,
    ActionBatchResult,
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
    if idempotency_key and idempotency_key in cache:
        return ActionBatchResult.model_validate(cache[idempotency_key])
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
    results: list[ActionItemResult] = []
    screenshot = None
    async with request.app.state.input_lock:
        for index, action in enumerate(payload.actions):
            start = time.perf_counter()
            try:
                output = await _execute_action(action, request, call_id=call_id)
                elapsed_ms = (time.perf_counter() - start) * 1000
                item = ActionItemResult(
                    index=index,
                    type=action.type,
                    ok=True,
                    elapsed_ms=elapsed_ms,
                    output=output or {},
                )
                results.append(item)
                if action.type not in ("screenshot", "zoom", "cursor_position"):
                    request.app.state.action_count += 1
                budgets.enforce(request)
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
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - start) * 1000
                with suppress(Exception):
                    await request.app.state.backend.release_all()
                item = ActionItemResult(
                    index=index,
                    type=action.type,
                    ok=False,
                    elapsed_ms=elapsed_ms,
                    error=str(exc),
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
                            "error": str(exc),
                        }
                    },
                )
                if not payload.continue_on_error:
                    break
        if payload.screenshot_after:
            options = payload.screenshot_options or ScreenshotOptions()
            screenshot = await request.app.state.backend.screenshot(
                options,
                artifact_store=request.app.state.artifacts,
                call_id=call_id,
                retention_class="trace",
            )
            request.app.state.screenshot_count += 1
            budgets.enforce(request)
            _append_screenshot_after_trace(request, payload, screenshot, call_id=call_id)
    result = ActionBatchResult(
        ok=all(item.ok for item in results), call_id=call_id, results=results, screenshot=screenshot
    )
    if idempotency_key:
        cache[idempotency_key] = result.model_dump(mode="json")
    return result


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
            if action.duration_ms is not None:
                await asyncio.sleep(action.duration_ms / 1000)
        finally:
            await backend.key_up(action.key)
        return {}
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
    return errors


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
    writer.append(
        TraceEntry(
            ts=datetime.now(UTC),
            run_id=payload.run_id or request.app.state.settings.run_id,
            call_id=call_id,
            sequence=payload.sequence if payload.sequence is not None else sequence,
            source=payload.source,
            normalized_action=normalized,
            result=result.model_dump(mode="json"),
            elapsed_ms=result.elapsed_ms,
            screenshot_after_uri=_screenshot_uri(result),
            coordinate_space=_coordinate_space(result),
            redactions=["text"] if isinstance(action, TypeAction) else [],
            error={"message": result.error} if result.error else None,
        )
    )


def _append_screenshot_after_trace(
    request: Request,
    payload: ActionBatchRequest,
    screenshot: Any,
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
            result={
                "ok": True,
                "format": screenshot.format,
                "width": screenshot.width,
                "height": screenshot.height,
                "size_bytes": screenshot.size_bytes,
                "artifact_uri": screenshot.artifact_uri,
            },
            screenshot_after_uri=screenshot.artifact_uri,
            coordinate_space=screenshot.coordinate_space,
        )
    )


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
