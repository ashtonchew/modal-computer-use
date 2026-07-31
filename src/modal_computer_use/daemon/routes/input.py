from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.daemon.routes.execution import run_input_action
from modal_computer_use.models import ActionResult

router = APIRouter(prefix="/v1/input")


@router.post("/release-all")
async def release_all(request: Request) -> ActionResult:
    async def operation() -> ActionResult:
        return await request.app.state.backend.release_all()

    return await run_input_action(
        request,
        operation,
        semantic_data={},
        fallback_code="release_all_failed",
        fallback_message="release all failed",
    )
