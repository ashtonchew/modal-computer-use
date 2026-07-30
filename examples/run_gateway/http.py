"""Strict HTTP models, routes, and sanitized error boundaries."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .domain import ReplayTombstone, RunRecord, RunState
from .service import (
    AuthenticationRequired,
    DesktopBusy,
    GatewayError,
    IdempotencyConflict,
    ObjectNotFound,
    RunConflict,
    RunGatewayService,
    TenantQuotaExceeded,
)


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    desktop_key: str = Field(min_length=1, max_length=256)
    task_key: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=256)


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    state: RunState


class CancelRunResponse(RunResponse):
    cancellation_requested: bool


def build_run_gateway_app(service: RunGatewayService) -> FastAPI:
    if not isinstance(service, RunGatewayService):
        raise ValueError("an application-configured RunGatewayService is required")
    app = FastAPI(title="application-owned Modal run gateway", version="1.0.0")

    @app.exception_handler(GatewayError)
    async def gateway_error(_request: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": "invalid_request"})

    @app.post("/v1/runs", response_model=RunResponse, status_code=202)
    async def create_run(body: CreateRunRequest, request: Request) -> RunResponse | JSONResponse:
        try:
            return _run_response(await service.create_run(request, body))
        except asyncio.CancelledError:
            raise
        except GatewayError:
            raise
        except Exception:
            return _internal_error_response()

    @app.get("/v1/runs/{run_id}", response_model=RunResponse)
    async def get_run(run_id: str, request: Request) -> RunResponse | JSONResponse:
        try:
            return _run_response(await service.get_run(request, run_id))
        except asyncio.CancelledError:
            raise
        except GatewayError:
            raise
        except Exception:
            return _internal_error_response()

    @app.post("/v1/runs/{run_id}/cancel", response_model=CancelRunResponse)
    async def cancel_run(
        run_id: str,
        request: Request,
    ) -> CancelRunResponse | JSONResponse:
        try:
            record, requested = await service.cancel_run(request, run_id)
            return CancelRunResponse(
                run_id=record.run_id,
                state=record.state,
                cancellation_requested=requested,
            )
        except asyncio.CancelledError:
            raise
        except GatewayError:
            raise
        except Exception:
            return _internal_error_response()

    return app


def _run_response(record: RunRecord | ReplayTombstone) -> RunResponse:
    return RunResponse(run_id=record.run_id, state=record.state)


def _internal_error_response() -> JSONResponse:
    return JSONResponse(status_code=500, content={"error": "internal_error"})


__all__ = [
    "AuthenticationRequired",
    "CancelRunResponse",
    "CreateRunRequest",
    "DesktopBusy",
    "GatewayError",
    "IdempotencyConflict",
    "ObjectNotFound",
    "RunConflict",
    "RunResponse",
    "TenantQuotaExceeded",
    "build_run_gateway_app",
]
