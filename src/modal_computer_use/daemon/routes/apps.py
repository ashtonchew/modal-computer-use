from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.daemon.auth import require_privileged_auth
from modal_computer_use.daemon.routes.execution import budget_policy, run_input_action
from modal_computer_use.daemon.routes.validation import map_e2big, validate_command_vector
from modal_computer_use.daemon.schemas import LaunchRequest, OpenArtifactRequest
from modal_computer_use.models import ActionResult
from modal_computer_use.redaction import sanitize_payload

router = APIRouter(prefix="/v1/apps")


@router.post("/launch")
async def launch(payload: LaunchRequest, request: Request) -> ActionResult:
    require_privileged_auth(request)
    command = [payload.command, *payload.args]
    validate_command_vector(
        command,
        maximum_arguments=request.app.state.settings.max_command_arguments,
    )

    async def operation() -> ActionResult:
        try:
            result = await request.app.state.backend.launch(payload.command, payload.args)
        except OSError as exc:
            mapped = map_e2big(exc)
            if mapped is not None:
                raise mapped from exc
            raise
        return ActionResult.model_validate(sanitize_payload(result.model_dump(mode="json")))

    return await run_input_action(
        request,
        operation,
        semantic_data=payload,
        fallback_code="app_launch_failed",
        fallback_message="app launch failed",
    )


@router.post("/open-artifact")
async def open_artifact(payload: OpenArtifactRequest, request: Request) -> ActionResult:
    budget_error = budget_policy(request).action_reservation_error()
    if budget_error is not None:
        raise budget_error
    path = request.app.state.artifacts.resolve(payload.path)
    if not path.exists():
        raise FileNotFoundError(payload.path)

    async def operation() -> ActionResult:
        try:
            result = await request.app.state.backend.launch("xdg-open", [str(path)])
        except OSError as exc:
            mapped = map_e2big(exc)
            if mapped is not None:
                raise mapped from exc
            raise
        body = sanitize_payload(result.model_dump(mode="json"))
        if isinstance(body, dict) and isinstance(body.get("output"), dict):
            body["output"].pop("args", None)
            body["output"]["artifact_opened"] = True
        return ActionResult.model_validate(body)

    return await run_input_action(
        request,
        operation,
        semantic_data=payload,
        fallback_code="artifact_open_failed",
        fallback_message="artifact open failed",
    )
