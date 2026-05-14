from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.daemon import budgets
from modal_computer_use.daemon.schemas import TextRequest
from modal_computer_use.models import ActionResult

router = APIRouter(prefix="/v1/clipboard")


@router.get("/text")
async def get_text(request: Request) -> dict[str, str]:
    async with request.app.state.input_lock:
        return {"text": await request.app.state.backend.clipboard_get()}


@router.put("/text")
async def set_text(payload: TextRequest, request: Request) -> ActionResult:
    async with request.app.state.input_lock:
        budgets.enforce_idle(request)
        result = await request.app.state.backend.clipboard_set(payload.text)
        budgets.touch_activity(request)
        return result


@router.delete("/text")
async def clear_text(request: Request) -> ActionResult:
    async with request.app.state.input_lock:
        budgets.enforce_idle(request)
        result = await request.app.state.backend.clipboard_clear()
        budgets.touch_activity(request)
        return result
