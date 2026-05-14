from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.daemon import budgets
from modal_computer_use.daemon.routes.validation import ensure_desktop_ready
from modal_computer_use.daemon.schemas import LaunchRequest, OpenArtifactRequest
from modal_computer_use.models import ActionResult

router = APIRouter(prefix="/v1/apps")


@router.post("/launch")
async def launch(payload: LaunchRequest, request: Request) -> ActionResult:
    await ensure_desktop_ready(request)
    async with request.app.state.input_lock:
        budgets.reserve_action(request)
        return await request.app.state.backend.launch(payload.command, payload.args)


@router.post("/open-artifact")
async def open_artifact(payload: OpenArtifactRequest, request: Request) -> ActionResult:
    path = request.app.state.artifacts.resolve(payload.path)
    if not path.exists():
        raise FileNotFoundError(payload.path)
    await ensure_desktop_ready(request)
    async with request.app.state.input_lock:
        budgets.reserve_action(request)
        return await request.app.state.backend.launch("xdg-open", [str(path)])
