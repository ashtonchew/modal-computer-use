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
        grant = request.app.state.lease_coordinator.acquire(payload.run_id)
        _schedule_expiry(request)
    response.headers[LEASE_TOKEN_HEADER] = grant.token
    response.headers["x-computer-use-lease-protocol"] = LEASE_PROTOCOL_VERSION
    response.headers["Cache-Control"] = "no-store"
    return grant.public_payload()


@router.post("/heartbeat")
async def heartbeat(request: Request, response: Response) -> dict[str, object]:
    async with request.app.state.input_lock:
        result = request.app.state.lease_coordinator.heartbeat(
            lease_credentials_from_headers(request.headers)
        )
        _schedule_expiry(request)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/release")
async def release(request: Request, response: Response) -> dict[str, object]:
    async with request.app.state.input_lock:
        result = request.app.state.lease_coordinator.release(
            lease_credentials_from_headers(request.headers)
        )
        _cancel_expiry(request.app.state)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/status")
async def status(request: Request, response: Response) -> dict[str, object]:
    async with request.app.state.input_lock:
        result = request.app.state.lease_coordinator.status()
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
            state.lease_coordinator.status()
    except asyncio.CancelledError:
        raise
    finally:
        current = getattr(state, "lease_expiry_task", None)
        if current is asyncio.current_task():
            state.lease_expiry_task = None
