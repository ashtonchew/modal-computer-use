from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.daemon import budgets
from modal_computer_use.daemon.routes.validation import ensure_desktop_ready
from modal_computer_use.daemon.schemas import LaunchRequest, OpenArtifactRequest
from modal_computer_use.models import ActionResult
from modal_computer_use.redaction import sanitize_payload

router = APIRouter(prefix="/v1/apps")


@router.post("/launch")
async def launch(payload: LaunchRequest, request: Request) -> ActionResult:
    await ensure_desktop_ready(request)
    budget_error = budgets.action_reservation_error(request)
    if budget_error is not None:
        raise budget_error
    async with request.app.state.input_lock:
        budgets.reserve_action(request)
        result = await request.app.state.backend.launch(payload.command, payload.args)
        return ActionResult.model_validate(sanitize_payload(result.model_dump(mode="json")))


@router.post("/open-artifact")
async def open_artifact(payload: OpenArtifactRequest, request: Request) -> ActionResult:
    path = request.app.state.artifacts.resolve(payload.path)
    if not path.exists():
        raise FileNotFoundError(payload.path)
    await ensure_desktop_ready(request)
    budget_error = budgets.action_reservation_error(request)
    if budget_error is not None:
        raise budget_error
    async with request.app.state.input_lock:
        budgets.reserve_action(request)
        result = await request.app.state.backend.launch("xdg-open", [str(path)])
        body = sanitize_payload(result.model_dump(mode="json"))
        if isinstance(body, dict) and isinstance(body.get("output"), dict):
            body["output"].pop("args", None)
            body["output"]["artifact_opened"] = True
        return ActionResult.model_validate(body)
