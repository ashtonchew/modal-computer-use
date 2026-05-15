from __future__ import annotations

import asyncio
from contextlib import suppress

from fastapi import APIRouter, Request

from modal_computer_use.actions import KEY_ALIASES
from modal_computer_use.daemon import budgets
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.routes.actions import (
    _budget_counts,
    _count_action_tree,
    _counts_against_action_budget,
    _effective_action_timeout_ms,
    _execute_action,
    _validate_action_timeouts,
    _validate_actions,
    _validate_screenshot_pixel_budget,
)
from modal_computer_use.daemon.routes.validation import ensure_desktop_ready, validate_keys
from modal_computer_use.daemon.schemas import HoldRequest, HotkeyRequest, KeyRequest, TypeRequest
from modal_computer_use.errors import ActionValidationError
from modal_computer_use.models import ActionBatchRequest, ActionResult, parse_action
from modal_computer_use.redaction import sanitize_payload_with_secrets

router = APIRouter(prefix="/v1/keyboard")


@router.post("/type")
async def keyboard_type(payload: TypeRequest, request: Request) -> ActionResult:
    await ensure_desktop_ready(request)
    budget_error = budgets.action_reservation_error(request)
    if budget_error is not None:
        raise budget_error
    async with request.app.state.input_lock:
        budgets.reserve_action(request)
        result = await request.app.state.backend.keyboard_type(
            payload.text,
            delay_ms=payload.delay_ms,
            method=payload.method,
        )
        return _sanitize_action_result(
            result,
            secret=payload.text,
            replacement="[redacted typed text]",
        )


@router.post("/press")
async def press(payload: KeyRequest, request: Request) -> ActionResult:
    validate_keys(payload.key, *payload.modifiers)
    await ensure_desktop_ready(request)
    budget_error = budgets.action_reservation_error(request)
    if budget_error is not None:
        raise budget_error
    async with request.app.state.input_lock:
        budgets.reserve_action(request)
        return await request.app.state.backend.keyboard_press(
            payload.key,
            modifiers=payload.modifiers,
            duration_ms=payload.duration_ms,
        )


@router.post("/hotkey")
async def hotkey(payload: HotkeyRequest, request: Request) -> ActionResult:
    validate_keys(*payload.keys)
    await ensure_desktop_ready(request)
    budget_error = budgets.action_reservation_error(request)
    if budget_error is not None:
        raise budget_error
    async with request.app.state.input_lock:
        budgets.reserve_action(request)
        return await request.app.state.backend.keyboard_hotkey(
            payload.keys,
            duration_ms=payload.duration_ms,
        )


@router.post("/hold")
async def hold(payload: HoldRequest, request: Request) -> ActionResult:
    validate_keys(payload.key)
    try:
        nested_actions = [parse_action(action) for action in payload.actions]
    except ActionValidationError as exc:
        raise DaemonError(
            "nested hold action validation failed",
            status_code=422,
            code="action_validation_failed",
            details={"errors": ["invalid nested hold action"]},
        ) from exc
    errors = _validate_actions(
        nested_actions,
        width=request.app.state.backend.width,
        height=request.app.state.backend.height,
    )
    batch_payload = ActionBatchRequest(actions=nested_actions)
    nested_count = _count_action_tree(nested_actions)
    if nested_count > request.app.state.settings.max_batch_actions:
        errors.append(
            "batch exceeds max_batch_actions "
            f"{request.app.state.settings.max_batch_actions} "
            f"with {nested_count} total actions"
        )
    errors.extend(
        _validate_action_timeouts(
            batch_payload, request.app.state.settings.max_action_timeout_ms
        )
    )
    errors.extend(_validate_screenshot_pixel_budget(batch_payload, request))
    if errors:
        raise DaemonError(
            "nested hold action validation failed",
            status_code=422,
            code="action_validation_failed",
            details={"errors": errors},
        )
    await ensure_desktop_ready(request)
    _preflight_hold_budget(request, nested_actions)
    async with request.app.state.input_lock:
        _preflight_hold_budget(request, nested_actions)
        budgets.reserve_action(request)
        released_all = False
        await request.app.state.backend.key_down(payload.key)
        try:
            nested_results = []
            for nested_action in nested_actions:
                if _counts_against_action_budget(nested_action):
                    budgets.reserve_action(request)
                timeout_seconds = (
                    _effective_action_timeout_ms(nested_action, batch_payload, request) / 1000
                )
                output = await asyncio.wait_for(
                    _execute_action(
                        nested_action,
                        request,
                        call_id="keyboard_hold",
                        payload=batch_payload,
                    ),
                    timeout=timeout_seconds,
                )
                nested_results.append(
                    {"type": nested_action.type, "ok": True, "output": output or {}}
                )
            if payload.duration_ms:
                await asyncio.sleep(payload.duration_ms / 1000)
        except TimeoutError as exc:
            released_all = True
            with suppress(Exception):
                await request.app.state.backend.release_all()
            raise DaemonError(
                "keyboard hold timed out",
                status_code=408,
                code="timeout",
                details={},
            ) from exc
        except Exception:
            released_all = True
            with suppress(Exception):
                await request.app.state.backend.release_all()
            raise
        finally:
            if released_all:
                with suppress(Exception):
                    await request.app.state.backend.key_up(payload.key)
            else:
                await request.app.state.backend.key_up(payload.key)
    return ActionResult(ok=True, output={"actions": nested_results} if nested_results else {})


@router.get("/keys")
async def supported_keys() -> dict[str, str]:
    return KEY_ALIASES


def _sanitize_action_result(
    result: ActionResult, *, secret: str, replacement: str
) -> ActionResult:
    payload = sanitize_payload_with_secrets(result.model_dump(mode="json"), [(secret, replacement)])
    return ActionResult.model_validate(payload)


def _preflight_hold_budget(request: Request, nested_actions: list[object]) -> None:
    nested_action_count, nested_screenshot_count = _budget_counts(nested_actions)
    action_error = budgets.action_reservation_error(request, count=1 + nested_action_count)
    if action_error is not None:
        raise action_error
    if nested_screenshot_count:
        screenshot_error = budgets.screenshot_reservation_error(
            request, count=nested_screenshot_count
        )
        if screenshot_error is not None:
            raise screenshot_error
