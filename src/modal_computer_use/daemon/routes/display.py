from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.models import DisplayInfo

router = APIRouter(prefix="/v1/display")


@router.get("/info")
async def info(request: Request) -> DisplayInfo:
    return await request.app.state.backend.display_info()
