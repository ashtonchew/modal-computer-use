from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.daemon import budgets
from modal_computer_use.daemon.routes.validation import ensure_desktop_ready
from modal_computer_use.daemon.schemas import CommandRunRequest
from modal_computer_use.models import ActionResult

router = APIRouter(prefix="/v1/commands")


@router.post("/run")
async def run(payload: CommandRunRequest, request: Request) -> ActionResult:
    await ensure_desktop_ready(request)
    budgets.reserve_action(request)
    return await request.app.state.backend.run_command(payload.command, timeout=payload.timeout)
