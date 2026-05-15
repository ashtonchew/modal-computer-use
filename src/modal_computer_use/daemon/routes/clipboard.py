from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.daemon import budgets
from modal_computer_use.daemon.routes.validation import ensure_desktop_ready, ready_input_lock
from modal_computer_use.daemon.schemas import TextRequest
from modal_computer_use.models import ActionResult
from modal_computer_use.redaction import sanitize_payload_with_secrets

router = APIRouter(prefix="/v1/clipboard")


@router.get("/text")
async def get_text(request: Request) -> dict[str, str]:
    await ensure_desktop_ready(request)
    async with ready_input_lock(request):
        return {"text": await request.app.state.backend.clipboard_get()}


@router.put("/text")
async def set_text(payload: TextRequest, request: Request) -> ActionResult:
    await ensure_desktop_ready(request)
    budget_error = budgets.action_reservation_error(request)
    if budget_error is not None:
        raise budget_error
    async with ready_input_lock(request):
        budgets.reserve_action(request)
        result = await request.app.state.backend.clipboard_set(payload.text)
        budgets.touch_activity(request)
        return _sanitize_action_result(
            result,
            secret=payload.text,
            replacement="[redacted clipboard text]",
        )


@router.delete("/text")
async def clear_text(request: Request) -> ActionResult:
    await ensure_desktop_ready(request)
    budget_error = budgets.action_reservation_error(request)
    if budget_error is not None:
        raise budget_error
    async with ready_input_lock(request):
        budgets.reserve_action(request)
        result = await request.app.state.backend.clipboard_clear()
        budgets.touch_activity(request)
        return result


def _sanitize_action_result(
    result: ActionResult, *, secret: str, replacement: str
) -> ActionResult:
    payload = sanitize_payload_with_secrets(result.model_dump(mode="json"), [(secret, replacement)])
    return ActionResult.model_validate(payload)
