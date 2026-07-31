from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from modal_computer_use.actions import KEY_ALIASES
from modal_computer_use.daemon.routes.execution import (
    raise_for_failed_action_result,
    run_input_action,
)
from modal_computer_use.daemon.routes.validation import validate_keys
from modal_computer_use.daemon.schemas import HoldRequest, HotkeyRequest, KeyRequest, TypeRequest
from modal_computer_use.models import ActionResult
from modal_computer_use.redaction import sanitize_payload_with_secrets

router = APIRouter(prefix="/v1/keyboard")


@router.post("/type")
async def keyboard_type(payload: TypeRequest, request: Request) -> ActionResult:
    async def operation() -> ActionResult:
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

    return await run_input_action(
        request,
        operation,
        semantic_data=payload,
        fallback_code="keyboard_type_failed",
        fallback_message="keyboard type failed",
    )


@router.post("/press")
async def press(payload: KeyRequest, request: Request) -> ActionResult:
    validate_keys(payload.key, *payload.modifiers)

    async def operation() -> ActionResult:
        return await request.app.state.backend.keyboard_press(
            payload.key,
            modifiers=payload.modifiers,
            duration_ms=payload.duration_ms,
        )

    return await run_input_action(
        request,
        operation,
        semantic_data=payload,
        fallback_code="keyboard_press_failed",
        fallback_message="keyboard press failed",
    )


@router.post("/hotkey")
async def hotkey(payload: HotkeyRequest, request: Request) -> ActionResult:
    validate_keys(*payload.keys)

    async def operation() -> ActionResult:
        return await request.app.state.backend.keyboard_hotkey(
            payload.keys,
            duration_ms=payload.duration_ms,
        )

    return await run_input_action(
        request,
        operation,
        semantic_data=payload,
        fallback_code="keyboard_hotkey_failed",
        fallback_message="keyboard hotkey failed",
    )


@router.post("/hold")
async def hold(payload: HoldRequest, request: Request) -> ActionResult:
    validate_keys(payload.key)

    async def operation() -> ActionResult:
        down = await request.app.state.backend.key_down(payload.key)
        raise_for_failed_action_result(
            down,
            fallback_code="keyboard_hold_failed",
            fallback_message="keyboard hold failed",
        )
        try:
            if payload.duration_ms:
                await asyncio.sleep(payload.duration_ms / 1000)
        finally:
            up = await request.app.state.backend.key_up(payload.key)
            raise_for_failed_action_result(
                up,
                fallback_code="keyboard_hold_failed",
                fallback_message="keyboard hold release failed",
            )
        return ActionResult(ok=True)

    return await run_input_action(
        request,
        operation,
        semantic_data=payload,
        fallback_code="keyboard_hold_failed",
        fallback_message="keyboard hold failed",
    )


@router.get("/keys")
async def supported_keys() -> dict[str, str]:
    return KEY_ALIASES


def _sanitize_action_result(
    result: ActionResult, *, secret: str, replacement: str
) -> ActionResult:
    payload = sanitize_payload_with_secrets(result.model_dump(mode="json"), [(secret, replacement)])
    return ActionResult.model_validate(payload)
