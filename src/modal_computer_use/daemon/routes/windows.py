from __future__ import annotations

import asyncio
import re
import time

from fastapi import APIRouter, Request

from modal_computer_use.daemon import budgets
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.routes.validation import ensure_desktop_ready, ready_input_lock
from modal_computer_use.daemon.schemas import WaitForWindowRequest
from modal_computer_use.models import ActionResult, X11Window

router = APIRouter(prefix="/v1/windows")


@router.get("")
async def list_windows(request: Request) -> list[X11Window]:
    await ensure_desktop_ready(request)
    return await request.app.state.backend.windows()


@router.get("/active")
async def active(request: Request) -> X11Window | None:
    await ensure_desktop_ready(request)
    return await request.app.state.backend.active_window()


@router.post("/{window_id}/activate")
async def activate(window_id: str, request: Request) -> ActionResult:
    await ensure_desktop_ready(request)
    budget_error = budgets.action_reservation_error(request)
    if budget_error is not None:
        raise budget_error
    async with ready_input_lock(request):
        budgets.reserve_action(request)
        return await request.app.state.backend.activate_window(window_id)


@router.post("/{window_id}/close")
async def close(window_id: str, request: Request) -> ActionResult:
    await ensure_desktop_ready(request)
    budget_error = budgets.action_reservation_error(request)
    if budget_error is not None:
        raise budget_error
    async with ready_input_lock(request):
        budgets.reserve_action(request)
        return await request.app.state.backend.close_window(window_id)


@router.post("/wait-for")
async def wait_for(payload: WaitForWindowRequest, request: Request) -> X11Window:
    deadline = time.monotonic() + payload.timeout
    pattern = re.compile(payload.title_regex) if payload.title_regex else None
    while True:
        await ensure_desktop_ready(request)
        for window in await request.app.state.backend.windows():
            if pattern and not pattern.search(window.title):
                continue
            if payload.class_name is not None and window.class_name != payload.class_name:
                continue
            if payload.pid is not None and window.pid != payload.pid:
                continue
            return window
        if time.monotonic() >= deadline:
            raise DaemonError(
                "window not found before timeout", status_code=404, code="window_not_found"
            )
        await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
