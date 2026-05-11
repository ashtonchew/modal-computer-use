from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.models import ActionResult

router = APIRouter(prefix="/v1/input")


@router.post("/release-all")
async def release_all(request: Request) -> ActionResult:
    async with request.app.state.input_lock:
        return await request.app.state.backend.release_all()
