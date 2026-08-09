from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic

from fastapi import APIRouter, Request

from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.routes.execution import budget_policy, run_idle_only_mutation
from modal_computer_use.daemon.routes.validation import (
    begin_display_restart,
    daemon_readiness,
    end_display_restart,
    invalidate_desktop_readiness,
)
from modal_computer_use.models import ActionResult, ComputerStatus, LifecycleResult

router = APIRouter(prefix="/v1/computer")

_READINESS_RETRY_WINDOW_SECONDS = 5.0
_READINESS_RETRY_INTERVAL_SECONDS = 0.05


async def _wait_for_display_ready(request: Request) -> None:
    deadline = monotonic() + _READINESS_RETRY_WINDOW_SECONDS
    errors: list[str] = []
    while True:
        ready, errors = await daemon_readiness(request, force=True)
        if ready:
            return
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(_READINESS_RETRY_INTERVAL_SECONDS, remaining))
    raise DaemonError(
        "desktop is not ready after display lifecycle mutation",
        status_code=503,
        code="desktop_not_ready",
        details={"errors": errors},
    )


async def _verify_display_after_restart(request: Request) -> None:
    settings = request.app.state.settings
    await _wait_for_display_ready(request)

    startup_url = settings.browser_open_url_on_start
    browser_configured = bool(startup_url or settings.browser_prewarm)
    if browser_configured:
        # Do not let this pre-browser observation become the generation snapshot
        # exposed to /readyz while the configured browser is still relaunching.
        invalidate_desktop_readiness(request.app.state)
    if startup_url:
        try:
            browser_result = await request.app.state.backend.open_url(
                startup_url,
                wait_for_window=True,
            )
        except Exception as exc:
            request.app.state.browser_prewarm = ActionResult(
                ok=False,
                message="configured browser recovery failed",
                output={"error": type(exc).__name__},
            )
            raise DaemonError(
                "configured browser recovery failed",
                status_code=503,
                code="browser_recovery_failed",
                details={"error": type(exc).__name__},
            ) from exc
    elif settings.browser_prewarm:
        try:
            browser_result = await request.app.state.backend.prewarm_browser()
        except Exception as exc:
            request.app.state.browser_prewarm = ActionResult(
                ok=False,
                message="configured browser recovery failed",
                output={"error": type(exc).__name__},
            )
            raise DaemonError(
                "configured browser recovery failed",
                status_code=503,
                code="browser_recovery_failed",
                details={"error": type(exc).__name__},
            ) from exc
    else:
        browser_result = None
    request.app.state.browser_prewarm = browser_result
    if isinstance(browser_result, ActionResult) and not browser_result.ok:
        raise DaemonError(
            "configured browser recovery failed",
            status_code=503,
            code="browser_recovery_failed",
            details={"error": "browser action failed"},
        )

    if browser_result is not None:
        await _wait_for_display_ready(request)


async def mutate_display_generation(
    request: Request,
    supervisor_operation: Callable[[], Awaitable[None]],
    *,
    verify_readiness: bool,
) -> None:
    state = request.app.state
    begin_display_restart(state)
    state.display_reconstruction_failed = False
    try:
        try:
            invalidate_desktop_readiness(state)
            mutation_error: Exception | None = None
            try:
                await state.backend.invalidate_display_generation()
            except Exception as exc:
                mutation_error = exc
            try:
                await supervisor_operation()
            except Exception as exc:
                if mutation_error is None:
                    mutation_error = exc
            if mutation_error is not None:
                raise mutation_error
            if verify_readiness:
                await _verify_display_after_restart(request)
            state.display_reconstruction_failed = False
        except BaseException:
            # A successful first probe may have populated ReadinessCache before
            # browser recovery or a final probe failed. Never leave that snapshot
            # advertising a failed display mutation as ready.
            state.display_reconstruction_failed = True
            invalidate_desktop_readiness(state)
            raise
    finally:
        end_display_restart(state)


@router.get("/status")
async def status(request: Request) -> ComputerStatus:
    backend = request.app.state.backend
    process_statuses = request.app.state.supervisor.statuses()
    if process_statuses and all(
        process.status == "stopped" for process in process_statuses.values()
    ):
        return ComputerStatus(
            status="stopped",
            ready=False,
            display=request.app.state.settings.display,
            width=backend.width,
            height=backend.height,
            processes=process_statuses,
            resources={"profile": request.app.state.settings.image_profile},
            budgets=budget_policy(request).snapshot(),
        )
    ready, _ = await daemon_readiness(request)
    return ComputerStatus(
        status="running" if ready else "degraded",
        ready=ready,
        display=request.app.state.settings.display,
        width=backend.width,
        height=backend.height,
        processes=process_statuses,
        resources={"profile": request.app.state.settings.image_profile},
        budgets=budget_policy(request).snapshot(),
    )


@router.post("/start")
async def start(request: Request) -> LifecycleResult:
    async def operation() -> LifecycleResult:
        await mutate_display_generation(
            request,
            request.app.state.supervisor.start,
            verify_readiness=True,
        )
        return LifecycleResult(ok=True, status="running")

    return await run_idle_only_mutation(request, operation, semantic_data={})


@router.post("/stop")
async def stop(request: Request) -> LifecycleResult:
    async def operation() -> LifecycleResult:
        await mutate_display_generation(
            request,
            request.app.state.supervisor.stop,
            verify_readiness=False,
        )
        return LifecycleResult(ok=True, status="stopped")

    return await run_idle_only_mutation(request, operation, semantic_data={})


@router.post("/restart")
async def restart(request: Request) -> LifecycleResult:
    async def operation() -> LifecycleResult:
        await mutate_display_generation(
            request,
            request.app.state.supervisor.restart,
            verify_readiness=True,
        )
        return LifecycleResult(ok=True, status="running")

    return await run_idle_only_mutation(request, operation, semantic_data={})
