from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from modal_computer_use.actions import KEY_ALIASES
from modal_computer_use.daemon import budgets
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.routes.actions import (
    _counts_against_action_budget,
    _execute_action,
    _validate_actions,
)
from modal_computer_use.daemon.schemas import HoldRequest, HotkeyRequest, KeyRequest, TypeRequest
from modal_computer_use.errors import ActionValidationError
from modal_computer_use.models import ActionResult, parse_action

router = APIRouter(prefix="/v1/keyboard")


@router.post("/type")
async def keyboard_type(payload: TypeRequest, request: Request) -> ActionResult:
    async with request.app.state.input_lock:
        budgets.reserve_action(request)
        return await request.app.state.backend.keyboard_type(
            payload.text,
            delay_ms=payload.delay_ms,
            method=payload.method,
        )


@router.post("/press")
async def press(payload: KeyRequest, request: Request) -> ActionResult:
    async with request.app.state.input_lock:
        budgets.reserve_action(request)
        return await request.app.state.backend.keyboard_press(
            payload.key,
            modifiers=payload.modifiers,
            duration_ms=payload.duration_ms,
        )


@router.post("/hotkey")
async def hotkey(payload: HotkeyRequest, request: Request) -> ActionResult:
    async with request.app.state.input_lock:
        budgets.reserve_action(request)
        return await request.app.state.backend.keyboard_hotkey(
            payload.keys,
            duration_ms=payload.duration_ms,
        )


@router.post("/hold")
async def hold(payload: HoldRequest, request: Request) -> ActionResult:
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
    if errors:
        raise DaemonError(
            "nested hold action validation failed",
            status_code=422,
            code="action_validation_failed",
            details={"errors": errors},
        )
    async with request.app.state.input_lock:
        budgets.reserve_action(request)
        await request.app.state.backend.key_down(payload.key)
        try:
            nested_results = []
            for nested_action in nested_actions:
                if _counts_against_action_budget(nested_action):
                    budgets.reserve_action(request)
                output = await _execute_action(nested_action, request, call_id="keyboard_hold")
                nested_results.append(
                    {"type": nested_action.type, "ok": True, "output": output or {}}
                )
            if payload.duration_ms:
                await asyncio.sleep(payload.duration_ms / 1000)
        finally:
            await request.app.state.backend.key_up(payload.key)
    return ActionResult(ok=True, output={"actions": nested_results} if nested_results else {})


@router.get("/keys")
async def supported_keys() -> dict[str, str]:
    return KEY_ALIASES
