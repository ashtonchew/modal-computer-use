from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.daemon.routes.validation import ensure_desktop_ready
from modal_computer_use.models import DisplayInfo

router = APIRouter(prefix="/v1/display")


@router.get("/info")
async def info(request: Request) -> DisplayInfo:
    await ensure_desktop_ready(request)
    return await request.app.state.backend.display_info()
