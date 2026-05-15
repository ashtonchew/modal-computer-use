from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.daemon import budgets
from modal_computer_use.daemon.routes.validation import ensure_desktop_ready, ready_input_lock
from modal_computer_use.daemon.schemas import BrowserOpenUrlRequest
from modal_computer_use.models import ActionResult
from modal_computer_use.redaction import sanitize_payload

router = APIRouter(prefix="/v1/browser")


@router.post("/open-url")
async def open_url(payload: BrowserOpenUrlRequest, request: Request) -> ActionResult:
    await ensure_desktop_ready(request)
    budget_error = budgets.action_reservation_error(request)
    if budget_error is not None:
        raise budget_error
    async with ready_input_lock(request):
        budgets.reserve_action(request)
        result = await request.app.state.backend.open_url(
            payload.url, wait_for_window=payload.wait_for_window
        )
        return ActionResult.model_validate(sanitize_payload(result.model_dump(mode="json")))


@router.get("/status")
async def status(request: Request) -> dict[str, object]:
    await ensure_desktop_ready(request)
    return {
        "configured_browser": request.app.state.settings.browser,
        "prewarm": request.app.state.settings.browser_prewarm,
        "windows": len(await request.app.state.backend.windows()),
    }
