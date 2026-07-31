from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Request

from modal_computer_use.actions import is_supported_key
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.leases import lease_credentials_from_headers
from modal_computer_use.daemon.receipts import (
    begin_mutation_receipt,
    finish_mutation_receipt,
    operation_sequence_from_headers,
)
from modal_computer_use.models import Point, Region
from modal_computer_use.operation_kinds import stable_operation_kind


async def backend_readiness(state: Any, *, force: bool = False) -> tuple[bool, list[str]]:
    cache = getattr(state, "readiness_cache", None)
    if cache is None:
        return await state.backend.ready()
    return await cache.backend_ready(state.backend, force=force)


def mark_desktop_ready(state: Any) -> None:
    cache = getattr(state, "readiness_cache", None)
    if cache is not None:
        cache.mark_ready()


async def desktop_readiness(request: Request, *, force: bool = False) -> tuple[bool, list[str]]:
    ready, errors = await backend_readiness(request.app.state, force=force)
    if not request.app.state.supervisor.running:
        return False, ["desktop supervisor is stopped", *errors]
    return ready, errors


async def daemon_readiness(request: Request) -> tuple[bool, list[str]]:
    ready, errors = await desktop_readiness(request, force=True)
    if request.app.state.settings.vnc_mode == "off":
        return ready, errors
    for name in ("x11vnc", "novnc"):
        status = request.app.state.supervisor.status(name)
        if status.status in ("running", "unknown"):
            continue
        ready = False
        errors.append(f"{name} is not running")
    return ready, errors


async def ensure_desktop_ready(request: Request, *, force: bool = False) -> None:
    ready, errors = await desktop_readiness(request, force=force)
    if ready:
        return
    raise DaemonError(
        "desktop is not ready",
        status_code=503,
        code="desktop_not_ready",
        details={"errors": errors},
    )


@asynccontextmanager
async def ready_input_lock(request: Request) -> AsyncIterator[None]:
    lock_was_contended = request.app.state.input_lock.locked()
    async with request.app.state.input_lock:
        if lock_was_contended:
            await ensure_desktop_ready(request, force=True)
        yield


@asynccontextmanager
async def mutation_lock(
    request: Request,
    *,
    semantic_data: Any,
    operation_kind: str | None = None,
) -> AsyncIterator[None]:
    async with request.app.state.input_lock:
        handle = await begin_mutation_receipt(
            request.app.state,
            credentials=lease_credentials_from_headers(request.headers),
            sequence=operation_sequence_from_headers(request.headers),
            operation_kind=operation_kind or _operation_kind(request),
            semantic_data=semantic_data,
        )
        try:
            yield
        except BaseException as exc:
            await finish_mutation_receipt(request.app.state, handle, exc)
            raise
        else:
            await finish_mutation_receipt(request.app.state, handle, None)


@asynccontextmanager
async def ready_mutation_lock(
    request: Request,
    *,
    semantic_data: Any,
    operation_kind: str | None = None,
) -> AsyncIterator[None]:
    lock_was_contended = request.app.state.input_lock.locked()
    async with request.app.state.input_lock:
        await request.app.state.receipt_journal.ensure_mutation_allowed()
        credentials = lease_credentials_from_headers(request.headers)
        if lock_was_contended:
            await ensure_desktop_ready(request, force=True)
        handle = await begin_mutation_receipt(
            request.app.state,
            credentials=credentials,
            sequence=operation_sequence_from_headers(request.headers),
            operation_kind=operation_kind or _operation_kind(request),
            semantic_data=semantic_data,
        )
        try:
            yield
        except BaseException as exc:
            await finish_mutation_receipt(request.app.state, handle, exc)
            raise
        else:
            await finish_mutation_receipt(request.app.state, handle, None)


def _operation_kind(request: Request) -> str:
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    return stable_operation_kind(template) or "daemon.mutation"


def validate_point(request: Request, point: Point, *, field: str = "coordinate") -> None:
    width = request.app.state.backend.width
    height = request.app.state.backend.height
    if point.x >= width or point.y >= height:
        raise DaemonError(
            f"{field} is outside desktop geometry",
            status_code=422,
            code="coordinate_out_of_bounds",
            details={"field": field, "width": width, "height": height},
        )


def validate_optional_point(
    request: Request,
    *,
    x: int | None,
    y: int | None,
    field: str = "coordinate",
) -> None:
    if x is None and y is None:
        return
    if x is None or y is None:
        raise DaemonError(
            f"{field} requires both x and y",
            status_code=422,
            code="coordinate_pair_required",
            details={"field": field},
        )
    validate_point(request, Point(x=x, y=y), field=field)


def validate_region(request: Request, region: Region, *, field: str = "region") -> None:
    width = request.app.state.backend.width
    height = request.app.state.backend.height
    if region.right > width or region.bottom > height:
        raise DaemonError(
            f"{field} extends beyond desktop geometry",
            status_code=422,
            code="region_out_of_bounds",
            details={"field": field, "width": width, "height": height},
        )


def validate_keys(*keys: str) -> None:
    invalid = [key for key in keys if not is_supported_key(key)]
    if invalid:
        raise DaemonError(
            "unsupported key",
            status_code=422,
            code="unsupported_key",
            details={"keys": invalid},
        )
