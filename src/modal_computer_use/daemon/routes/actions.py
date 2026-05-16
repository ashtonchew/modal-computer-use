from __future__ import annotations

from fastapi import APIRouter, Header, Request

from modal_computer_use.daemon.actions import ActionBatchContext
from modal_computer_use.daemon.actions import run as run_batch
from modal_computer_use.daemon.actions import validate as validate_batch
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
