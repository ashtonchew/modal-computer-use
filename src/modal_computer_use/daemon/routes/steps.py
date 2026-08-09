from __future__ import annotations

from fastapi import APIRouter, Request, Response

from modal_computer_use.daemon.actions import ActionBatchContext, run_step
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.leases import lease_credentials_from_headers
from modal_computer_use.daemon.steps import StepRequest
from modal_computer_use.models import Screenshot
from modal_computer_use.steps import (
    STEP_MEDIA_TYPE,
    ComputerStepTiming,
    encode_step_envelope,
)

router = APIRouter()


@router.post(
    "/v1/steps",
    response_class=Response,
    responses={
        200: {
            "content": {STEP_MEDIA_TYPE: {}},
            "description": "Ordered actions and the immediate trailing screenshot.",
        }
    },
)
async def step(
    payload: StepRequest,
    request: Request,
) -> Response:
    """Execute one leased action batch and return its immediate observation."""
    accept = request.headers.get("accept")
    if accept not in {None, "*/*", STEP_MEDIA_TYPE}:
        raise DaemonError(
            "requested step representation is not supported",
            status_code=406,
            code="step_not_acceptable",
        )
    if request.headers.get("idempotency-key") is not None:
        raise DaemonError(
            "computer steps do not accept idempotency keys",
            status_code=422,
            code="step_idempotency_not_supported",
        )

    # Fail before desktop readiness checks or receipt creation.  This keeps a
    # missing/stale handoff from ever reaching the shared action executor.
    credentials = lease_credentials_from_headers(request.headers)
    async with request.app.state.lease_lock:
        admitted = request.app.state.lease_coordinator.validate_mutation(credentials)
    if admitted is None:
        raise DaemonError(
            "an active trajectory lease is required",
            status_code=409,
            code="lease_required",
        )

    context = ActionBatchContext(request.app.state, request.headers)
    result, captured = await run_step(payload.to_action_batch(), context)
    if captured is None:
        raise DaemonError(
            "step observation is unavailable; the completed mutation must not be replayed",
            status_code=409,
            code="operation_result_unavailable",
            details={"phase": "observation"},
        )

    screenshot = Screenshot(
        format=captured.format,
        width=captured.width,
        height=captured.height,
        size_bytes=len(captured.data),
        bytes=captured.data,
        sha256=captured.sha256,
        captured_at=captured.captured_at,
        coordinate_space=captured.coordinate_space,
        cursor_visible=captured.cursor_visible,
        cursor_position=captured.cursor_position,
    )
    try:
        timing = ComputerStepTiming(
            daemon_ms=(result.timing.daemon_ms if result.timing is not None else 0.0),
            action_ms=context.step_action_ms,
            screenshot_ms=context.step_screenshot_ms,
            total_ms=context.step_total_ms,
        )
        # Keep the binary transport implementation behind the route seam so
        # importing the daemon remains possible while an older client/runtime
        # is being upgraded.  Capability negotiation still fails closed on
        # that older runtime before the public SDK attempts a step.
        envelope = encode_step_envelope(
            actions=result,
            screenshot=screenshot,
            timing=timing,
        )
    except Exception as exc:
        raise DaemonError(
            "step observation is unavailable; the completed mutation must not be replayed",
            status_code=409,
            code="operation_result_unavailable",
            details={"phase": "envelope"},
        ) from exc
    return Response(
        content=envelope,
        media_type=STEP_MEDIA_TYPE,
        headers={
            "cache-control": "no-store, no-transform",
            "x-content-type-options": "nosniff",
            "x-computer-use-step-protocol": "computer-use.step.v1",
        },
    )
