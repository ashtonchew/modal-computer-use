from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from modal_computer_use.actions import KEY_ALIASES
from modal_computer_use.daemon import budgets
from modal_computer_use.daemon.schemas import HoldRequest, HotkeyRequest, KeyRequest, TypeRequest
from modal_computer_use.models import ActionResult

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
    async with request.app.state.input_lock:
        budgets.reserve_action(request)
        await request.app.state.backend.key_down(payload.key)
        try:
            if payload.duration_ms:
                await asyncio.sleep(payload.duration_ms / 1000)
        finally:
            await request.app.state.backend.key_up(payload.key)
    return ActionResult(ok=True)


@router.get("/keys")
async def supported_keys() -> dict[str, str]:
    return KEY_ALIASES
