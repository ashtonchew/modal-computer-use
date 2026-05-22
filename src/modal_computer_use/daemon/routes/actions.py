from __future__ import annotations

import base64

from fastapi import APIRouter, Header, Request, Response

from modal_computer_use.daemon.actions import ActionBatchContext, run_with_screenshot_bytes
from modal_computer_use.daemon.actions import run as run_batch
from modal_computer_use.daemon.actions import validate as validate_batch
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.routes.screenshots import _screenshot_headers
from modal_computer_use.models import ActionBatchRequest, ActionBatchResult, ValidationResult

router = APIRouter(prefix="/v1/actions")


@router.post("/validate")
async def validate(payload: ActionBatchRequest, request: Request) -> ValidationResult:
    return await validate_batch(payload, ActionBatchContext(request.app.state))


@router.post("/run")
async def run(
    payload: ActionBatchRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ActionBatchResult:
    return await run_batch(
        payload,
        ActionBatchContext(request.app.state),
        idempotency_key=idempotency_key,
    )


@router.post(
    "/run/raw-screenshot",
    responses={200: {"content": {"image/png": {}, "image/jpeg": {}, "image/webp": {}}}},
)
async def run_raw_screenshot(
    payload: ActionBatchRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    result, shot = await run_with_screenshot_bytes(
        payload,
        ActionBatchContext(request.app.state),
        idempotency_key=idempotency_key,
    )
    if shot is None:
        raise DaemonError(
            "action batch did not capture a raw screenshot",
            status_code=409,
            code="raw_screenshot_after_not_captured",
            details={"result": result.model_dump(mode="json")},
        )
    action_result = result.model_dump_json(exclude={"screenshot"})
    headers = {
        **_screenshot_headers(shot),
        "x-computer-use-action-result": base64.b64encode(action_result.encode("utf-8")).decode(
            "ascii"
        ),
    }
    return Response(
        content=shot.data,
        media_type=f"image/{shot.format}",
        headers=headers,
    )
