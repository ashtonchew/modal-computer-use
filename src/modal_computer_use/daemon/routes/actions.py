from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Header, Request

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
    TripleClickAction,
    TypeAction,
    ValidationResult,
    WaitAction,
    ZoomAction,
)

router = APIRouter(prefix="/v1/actions")


@router.post("/validate")
async def validate(payload: ActionBatchRequest) -> ValidationResult:
    return ValidationResult(ok=True, errors=[])


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
    results: list[ActionItemResult] = []
    screenshot = None
    async with request.app.state.input_lock:
        for index, action in enumerate(payload.actions):
            start = time.perf_counter()
            try:
                output = await _execute_action(action, request, call_id=call_id)
                elapsed_ms = (time.perf_counter() - start) * 1000
                results.append(
                    ActionItemResult(
                        index=index,
                        type=action.type,
                        ok=True,
                        elapsed_ms=elapsed_ms,
                        output=output or {},
                    )
                )
                if action.type not in ("screenshot", "zoom", "cursor_position"):
                    request.app.state.action_count += 1
                _enforce_budgets(request)
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - start) * 1000
                with suppress(Exception):
                    await request.app.state.backend.release_all()
                results.append(
                    ActionItemResult(
                        index=index,
                        type=action.type,
                        ok=False,
                        elapsed_ms=elapsed_ms,
                        error=str(exc),
                    )
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


def _enforce_budgets(request: Request) -> None:
    settings = request.app.state.settings
    if settings.max_actions is not None and request.app.state.action_count > settings.max_actions:
        raise DaemonError("action budget exceeded", status_code=429, code="budget_exceeded")
    if (
        settings.max_screenshots is not None
        and request.app.state.screenshot_count > settings.max_screenshots
    ):
        raise DaemonError("screenshot budget exceeded", status_code=429, code="budget_exceeded")
