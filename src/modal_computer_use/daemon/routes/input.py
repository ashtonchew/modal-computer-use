from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.daemon import budgets
from modal_computer_use.daemon.routes.validation import ensure_desktop_ready
from modal_computer_use.models import ActionResult

router = APIRouter(prefix="/v1/input")


@router.post("/release-all")
async def release_all(request: Request) -> ActionResult:
    await ensure_desktop_ready(request)
    budget_error = budgets.action_reservation_error(request)
    if budget_error is not None:
        raise budget_error
    async with request.app.state.input_lock:
        budgets.reserve_action(request)
        result = await request.app.state.backend.release_all()
        return result
