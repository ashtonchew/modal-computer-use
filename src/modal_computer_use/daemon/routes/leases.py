from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, field_validator

from modal_computer_use.daemon.leases import (
    LEASE_PROTOCOL_VERSION,
    LEASE_TOKEN_HEADER,
    lease_credentials_from_headers,
)

router = APIRouter(prefix="/v1/leases", include_in_schema=False)


class _AcquireRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str

    @field_validator("run_id")
    @classmethod
    def _non_blank_run_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("run_id must be non-empty")
        return value


@router.post("/acquire")
async def acquire(
    payload: _AcquireRequest,
    request: Request,
    response: Response,
) -> dict[str, object]:
    async with request.app.state.input_lock:
        async with request.app.state.lease_lock:
            previous = request.app.state.lease_coordinator.prepare_acquire()
        if previous["state"] == "expired" and isinstance(previous["run_id"], str):
            await request.app.state.receipt_journal.seal_run(
                previous["run_id"],
                "lease_expired",
            )
        await request.app.state.receipt_journal.validate_acquire(payload.run_id)
        await request.app.state.receipt_journal.activate_run(payload.run_id)
        async with request.app.state.lease_lock:
            grant = request.app.state.lease_coordinator.acquire(payload.run_id)
        _schedule_expiry(request)
    response.headers[LEASE_TOKEN_HEADER] = grant.token
    response.headers["x-computer-use-lease-protocol"] = LEASE_PROTOCOL_VERSION
    response.headers["Cache-Control"] = "no-store"
    return grant.public_payload()


@router.post("/heartbeat")
async def heartbeat(request: Request, response: Response) -> dict[str, object]:
    async with request.app.state.lease_lock:
        result = request.app.state.lease_coordinator.heartbeat(
            lease_credentials_from_headers(request.headers)
        )
        _schedule_expiry(request)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/release")
async def release(request: Request, response: Response) -> dict[str, object]:
    async with request.app.state.input_lock:
        credentials = lease_credentials_from_headers(request.headers)
        async with request.app.state.lease_lock:
            lease = request.app.state.lease_coordinator.validate_mutation(credentials)
        assert lease is not None
        result = await _seal_and_release_cancellation_safe(
            request.app.state,
            lease,
        )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/status")
async def status(request: Request, response: Response) -> dict[str, object]:
    async with request.app.state.input_lock:
        async with request.app.state.lease_lock:
            result = request.app.state.lease_coordinator.status()
        if result["state"] == "expired" and isinstance(result["run_id"], str):
            await request.app.state.receipt_journal.seal_run(
                result["run_id"],
                "lease_expired",
            )
    response.headers["Cache-Control"] = "no-store"
    return result


def _schedule_expiry(request: Request) -> None:
    state = request.app.state
    _cancel_expiry(state)
    state.lease_expiry_task = asyncio.create_task(_expire_after_ttl(state))


def _cancel_expiry(state: Any) -> None:
    task: asyncio.Task[None] | None = getattr(state, "lease_expiry_task", None)
    if task is not None and not task.done():
        task.cancel()
    state.lease_expiry_task = None


async def _expire_after_ttl(state: Any) -> None:
    try:
        await asyncio.sleep(state.lease_coordinator.ttl_seconds)
        async with state.input_lock:
            async with state.lease_lock:
                status = state.lease_coordinator.status()
            if status["state"] == "expired" and isinstance(status["run_id"], str):
                await state.receipt_journal.seal_run(status["run_id"], "lease_expired")
    except asyncio.CancelledError:
        raise
    finally:
        current = getattr(state, "lease_expiry_task", None)
        if current is asyncio.current_task():
            state.lease_expiry_task = None


async def _seal_and_release_cancellation_safe(
    state: Any,
    admitted_lease: Any,
) -> dict[str, object]:
    async def seal_and_release() -> dict[str, object]:
        await state.receipt_journal.seal_run(admitted_lease.run_id, "lease_released")
        async with state.lease_lock:
            result = state.lease_coordinator.release_validated(admitted_lease)
        _cancel_expiry(state)
        return result

    task = asyncio.create_task(seal_and_release())
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
