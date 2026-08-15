from __future__ import annotations

import errno
import os
from collections.abc import AsyncIterator, Sized
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
        cache.mark_ready(state.backend)


def invalidate_desktop_readiness(state: Any) -> None:
    cache = getattr(state, "readiness_cache", None)
    if cache is not None:
        cache.invalidate()


def has_current_desktop_readiness_proof(state: Any) -> bool:
    cache = getattr(state, "readiness_cache", None)
    return cache is not None and cache.has_current_proof(state.backend)


def begin_display_restart(state: Any) -> None:
    """Atomically claim the display mutation slot before its first await."""
    if getattr(state, "display_restart_in_progress", False):
        raise DaemonError(
            "display lifecycle mutation is already in progress",
            status_code=409,
            code="display_restart_busy",
            details={"reason": "display_restart"},
        )
    if any(recording.status == "recording" for recording in state.recordings.list()):
        raise DaemonError(
            "display lifecycle mutation requires no active recording",
            status_code=409,
            code="display_restart_busy",
            details={"reason": "recording"},
        )
    if state.websocket_admission.active("observation"):
        raise DaemonError(
            "display lifecycle mutation requires no active observation websocket",
            status_code=409,
            code="display_restart_busy",
            details={"reason": "observation"},
        )
    if getattr(state, "active_http_observe_changes", 0):
        raise DaemonError(
            "display lifecycle mutation requires no active observe-change request",
            status_code=409,
            code="display_restart_busy",
            details={"reason": "observe_change"},
        )
    state.display_restart_in_progress = True


def end_display_restart(state: Any) -> None:
    state.display_restart_in_progress = False


@asynccontextmanager
async def http_observe_change_scope(request: Request) -> AsyncIterator[None]:
    """Admit one HTTP observe-change watcher with synchronous restart exclusion."""
    state = request.app.state
    if getattr(state, "display_restart_in_progress", False):
        raise DaemonError(
            "display lifecycle mutation is already in progress",
            status_code=409,
            code="display_restart_busy",
            details={"reason": "display_restart"},
        )
    state.active_http_observe_changes = getattr(state, "active_http_observe_changes", 0) + 1
    try:
        yield
    finally:
        state.active_http_observe_changes = max(state.active_http_observe_changes - 1, 0)


async def desktop_readiness_state(state: Any, *, force: bool = False) -> tuple[bool, list[str]]:
    """Return the canonical readiness proof for one daemon state object.

    Routes and shared action transports must consult the same lifecycle gates.  Keeping the
    implementation state-based avoids a second, subtly different readiness check for action
    batches that do not have a ``Request`` object.
    """
    if getattr(state, "display_restart_in_progress", False) and not force:
        return False, ["display lifecycle mutation is in progress"]
    if getattr(state, "display_reconstruction_failed", False):
        return False, ["display lifecycle reconstruction failed"]
    supervisor = state.supervisor
    if not supervisor.running:
        invalidate_desktop_readiness(state)
        return False, ["desktop supervisor is stopped"]
    if "xvfb" in supervisor.names:
        xvfb_status = supervisor.status("xvfb")
        if xvfb_status.status not in ("running", "unknown"):
            invalidate_desktop_readiness(state)
            return False, ["xvfb is not running"]
    ready, errors = await backend_readiness(state, force=force)
    return ready, errors


async def desktop_readiness(request: Request, *, force: bool = False) -> tuple[bool, list[str]]:
    return await desktop_readiness_state(request.app.state, force=force)


async def daemon_readiness(
    request: Request,
    *,
    force: bool = False,
) -> tuple[bool, list[str]]:
    ready, errors = await desktop_readiness(request, force=force)
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


async def ensure_desktop_ready_state(state: Any, *, force: bool = False) -> None:
    ready, errors = await desktop_readiness_state(state, force=force)
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
            await ensure_desktop_ready(
                request,
                force=not has_current_desktop_readiness_proof(request.app.state),
            )
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
    reuse_current_readiness_proof: bool = False,
) -> AsyncIterator[None]:
    lock_was_contended = request.app.state.input_lock.locked()
    async with request.app.state.input_lock:
        await request.app.state.receipt_journal.ensure_mutation_allowed()
        credentials = lease_credentials_from_headers(request.headers)
        if lock_was_contended:
            await ensure_desktop_ready(
                request,
                force=(
                    not reuse_current_readiness_proof
                    or not has_current_desktop_readiness_proof(request.app.state)
                ),
            )
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


def validate_collection_size(
    values: object,
    *,
    maximum: int,
    field: str,
    code: str,
) -> None:
    if maximum == 0 or not isinstance(values, Sized):
        return
    count = len(values)
    if count <= maximum:
        return
    raise DaemonError(
        f"{field} exceeds the configured limit",
        status_code=422,
        code=code,
        details={"field": field, "count": count, "maximum": maximum},
    )


def command_argument_byte_limit() -> int:
    """Return Linux's effective maximum byte length for one argv element."""
    try:
        page_size = os.sysconf("SC_PAGESIZE")
    except (OSError, ValueError):
        page_size = 4096
    return min(131_071, (32 * page_size) - 1)


def validate_command_vector(
    command: object,
    *,
    maximum_arguments: int,
    field: str = "command",
) -> None:
    if not isinstance(command, (list, tuple)):
        return
    validate_collection_size(
        command,
        maximum=maximum_arguments,
        field=field,
        code="too_many_command_arguments",
    )
    maximum_bytes = command_argument_byte_limit()
    for index, argument in enumerate(command):
        if not isinstance(argument, str):
            continue
        encoded_bytes = len(os.fsencode(argument))
        if encoded_bytes <= maximum_bytes:
            continue
        raise DaemonError(
            "command argument exceeds the platform byte limit",
            status_code=422,
            code="command_argument_too_large",
            details={
                "field": f"{field}[{index}]",
                "encoded_bytes": encoded_bytes,
                "maximum_bytes": maximum_bytes,
            },
        )


def map_e2big(exc: BaseException) -> DaemonError | None:
    if not isinstance(exc, OSError) or exc.errno != errno.E2BIG:
        return None
    return DaemonError(
        "command exceeds the platform execution limit",
        status_code=422,
        code="command_too_large",
        details={
            "errno": "E2BIG",
            "retry_safe": True,
            "emission_state": "not_started",
        },
    )
