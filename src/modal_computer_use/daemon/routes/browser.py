from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.daemon.schemas import BrowserOpenUrlRequest
from modal_computer_use.models import ActionResult

router = APIRouter(prefix="/v1/browser")


@router.post("/open-url")
async def open_url(payload: BrowserOpenUrlRequest, request: Request) -> ActionResult:
    return await request.app.state.backend.open_url(
        payload.url, wait_for_window=payload.wait_for_window
    )


@router.get("/status")
async def status(request: Request) -> dict[str, object]:
    return {
        "configured_browser": request.app.state.settings.browser,
        "prewarm": request.app.state.settings.browser_prewarm,
        "windows": len(await request.app.state.backend.windows()),
    }
