from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.daemon import budgets
from modal_computer_use.daemon.routes.validation import (
    ensure_desktop_ready,
    validate_keys,
    validate_optional_point,
    validate_point,
)
from modal_computer_use.daemon.schemas import (
    MouseButtonRequest,
    MouseClickRequest,
    MouseDragRequest,
    MouseMoveRequest,
    MouseScrollRequest,
)
from modal_computer_use.models import ActionResult, Point

router = APIRouter(prefix="/v1/mouse")


@router.post("/move")
async def move(payload: MouseMoveRequest, request: Request) -> Point:
    validate_point(request, payload)
    await ensure_desktop_ready(request)
    async with request.app.state.input_lock:
        budgets.reserve_action(request)
        return await request.app.state.backend.mouse_move(payload.x, payload.y)


@router.post("/click")
async def click(payload: MouseClickRequest, request: Request) -> Point:
    validate_optional_point(request, x=payload.x, y=payload.y)
    validate_keys(*payload.modifiers)
    await ensure_desktop_ready(request)
    async with request.app.state.input_lock:
        budgets.reserve_action(request)
        return await request.app.state.backend.mouse_click(
            payload.x,
            payload.y,
            button=payload.button,
            count=2 if payload.double else 1,
            modifiers=payload.modifiers,
        )


@router.post("/drag")
async def drag(payload: MouseDragRequest, request: Request) -> Point:
    start = None
    end = None
    if payload.start_x is not None and payload.start_y is not None:
        start = Point(x=payload.start_x, y=payload.start_y)
        validate_point(request, start, field="start")
    if payload.end_x is not None and payload.end_y is not None:
        end = Point(x=payload.end_x, y=payload.end_y)
        validate_point(request, end, field="end")
    for index, point in enumerate(payload.path or []):
        validate_point(request, point, field=f"path[{index}]")
    validate_keys(*payload.modifiers)
    await ensure_desktop_ready(request)
    async with request.app.state.input_lock:
        budgets.reserve_action(request)
        return await request.app.state.backend.mouse_drag(
            start=start,
            end=end,
            path=payload.path,
            button=payload.button,
            duration_ms=payload.duration_ms,
            modifiers=payload.modifiers,
        )


@router.post("/scroll")
async def scroll(payload: MouseScrollRequest, request: Request) -> ActionResult:
    validate_optional_point(request, x=payload.x, y=payload.y)
    await ensure_desktop_ready(request)
    async with request.app.state.input_lock:
        budgets.reserve_action(request)
        return await request.app.state.backend.mouse_scroll(
            payload.direction,
            amount=payload.amount,
            x=payload.x,
            y=payload.y,
        )


@router.post("/down")
async def down(payload: MouseButtonRequest, request: Request) -> ActionResult:
    validate_optional_point(request, x=payload.x, y=payload.y)
    await ensure_desktop_ready(request)
    async with request.app.state.input_lock:
        budgets.reserve_action(request)
        return await request.app.state.backend.mouse_down(payload.button, payload.x, payload.y)


@router.post("/up")
async def up(payload: MouseButtonRequest, request: Request) -> ActionResult:
    validate_optional_point(request, x=payload.x, y=payload.y)
    await ensure_desktop_ready(request)
    async with request.app.state.input_lock:
        budgets.reserve_action(request)
        return await request.app.state.backend.mouse_up(payload.button, payload.x, payload.y)


@router.get("/position")
async def position(request: Request) -> Point:
    await ensure_desktop_ready(request)
    return await request.app.state.backend.mouse_position()
