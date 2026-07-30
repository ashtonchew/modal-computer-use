from __future__ import annotations

import asyncio
import secrets
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.leases import lease_credentials_from_headers
from modal_computer_use.daemon.receipts import (
    MAX_OPERATION_SEQUENCE,
    RECEIPT_PROTOCOL_VERSION,
)

router = APIRouter(include_in_schema=False)
OWNER_PROOF_HEADER = "x-computer-use-owner-proof"


class _AcknowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str

    @field_validator("incident_id")
    @classmethod
    def _non_blank_incident_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("incident_id must be non-empty")
        return value


class _ReceiptStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    sequence: int = Field(ge=0, le=MAX_OPERATION_SEQUENCE)

    @field_validator("run_id")
    @classmethod
    def _non_blank_run_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("run_id must be non-empty")
        return value


class _ReceiptResolveRequest(_ReceiptStatusRequest):
    pass


@router.get("/v1/recovery/status")
async def recovery_status(request: Request, response: Response) -> dict[str, object]:
    async with request.app.state.input_lock:
        _require_owner_or_current_lease(request)
        result = await request.app.state.receipt_journal.recovery_status()
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/v1/recovery/acknowledge")
async def acknowledge(
    payload: _AcknowledgeRequest,
    request: Request,
    response: Response,
) -> dict[str, object]:
    _require_owner(request)
    response.headers["Cache-Control"] = "no-store"
    async with request.app.state.input_lock:
        result = await _acknowledge_and_reset_cancellation_safe(
            request.app.state,
            payload.incident_id,
        )
    return result


@router.post("/v1/receipts/status")
async def receipt_status(
    payload: _ReceiptStatusRequest,
    request: Request,
    response: Response,
) -> dict[str, object]:
    async with request.app.state.input_lock:
        if not _has_owner_proof(request):
            lease = request.app.state.lease_coordinator.validate_mutation(
                lease_credentials_from_headers(request.headers)
            )
            if lease is None or not secrets.compare_digest(lease.run_id, payload.run_id):
                raise DaemonError(
                    "receipt access is denied",
                    status_code=403,
                    code="receipt_access_denied",
                )
        result = await request.app.state.receipt_journal.receipt_status(
            payload.run_id,
            payload.sequence,
        )
    response.headers["Cache-Control"] = "no-store"
    response.headers["x-computer-use-receipt-protocol"] = RECEIPT_PROTOCOL_VERSION
    return result


@router.post("/v1/receipts/resolve")
async def resolve_receipt(
    payload: _ReceiptResolveRequest,
    request: Request,
    response: Response,
) -> dict[str, object]:
    credentials = lease_credentials_from_headers(request.headers)
    async with request.app.state.input_lock:
        if _has_owner_proof(request):
            proof = await request.app.state.receipt_journal.resolved_missing_proof(
                payload.run_id,
                payload.sequence,
            )
            if proof is None:
                raise _receipt_access_denied_error("receipt resolve access is denied")
            result = proof
        else:
            released_lease = request.app.state.lease_coordinator.authenticate_last_released(
                credentials
            )
            if released_lease is not None:
                if not secrets.compare_digest(released_lease.run_id, payload.run_id):
                    raise _receipt_access_denied_error("receipt resolve access is denied")
                proof = await request.app.state.receipt_journal.resolved_missing_proof(
                    payload.run_id,
                    payload.sequence,
                )
                if proof is None:
                    raise _receipt_access_denied_error("receipt resolve access is denied")
                result = proof
            else:
                lease = request.app.state.lease_coordinator.validate_mutation(credentials)
                if lease is None or not secrets.compare_digest(lease.run_id, payload.run_id):
                    raise _receipt_access_denied_error("receipt resolve access is denied")
                result = await _resolve_and_release_cancellation_safe(
                    request.app.state,
                    lease,
                    payload.run_id,
                    payload.sequence,
                )
    response.headers["Cache-Control"] = "no-store"
    response.headers["x-computer-use-receipt-protocol"] = RECEIPT_PROTOCOL_VERSION
    return result


def _require_owner(request: Request) -> None:
    if not _has_owner_proof(request):
        raise DaemonError(
            "owner authorization is required",
            status_code=403,
            code="owner_authorization_required",
        )


def _require_owner_or_current_lease(request: Request) -> None:
    if _has_owner_proof(request):
        return
    lease = request.app.state.lease_coordinator.validate_mutation(
        lease_credentials_from_headers(request.headers)
    )
    if lease is None:
        raise DaemonError(
            "recovery status access is denied",
            status_code=403,
            code="recovery_access_denied",
        )


def _has_owner_proof(request: Request) -> bool:
    provided = request.headers.get(OWNER_PROOF_HEADER)
    if not isinstance(provided, str) or not provided:
        return False
    settings = request.app.state.settings
    expected_tokens = (settings.tunnel_token, settings.local_token)
    matched = False
    for expected in expected_tokens:
        if isinstance(expected, str) and expected:
            matched = secrets.compare_digest(expected, provided) or matched
    return matched


def _receipt_access_denied_error(message: str) -> DaemonError:
    return DaemonError(
        message,
        status_code=403,
        code="receipt_access_denied",
    )


async def _acknowledge_and_reset_cancellation_safe(
    state: Any,
    incident_id: str,
) -> dict[str, object]:
    async def acknowledge_and_reset() -> dict[str, object]:
        result = await state.receipt_journal.acknowledge(incident_id)
        state.lease_coordinator.reset_after_owner_recovery()
        return result

    task = asyncio.create_task(acknowledge_and_reset())
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            if cancellation is None:
                cancellation = exc
            continue
        if cancellation is not None:
            raise cancellation
        return result


async def _resolve_and_release_cancellation_safe(
    state: Any,
    admitted_lease: Any,
    run_id: str,
    sequence: int,
) -> dict[str, object]:
    async def resolve_and_release() -> dict[str, object]:
        result = await state.receipt_journal.resolve(run_id, sequence)
        if result["state"] == "MISSING":
            state.lease_coordinator.release_validated(admitted_lease)
        return result

    task = asyncio.create_task(resolve_and_release())
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            if cancellation is None:
                cancellation = exc
            continue
        if cancellation is not None:
            raise cancellation
        return result
