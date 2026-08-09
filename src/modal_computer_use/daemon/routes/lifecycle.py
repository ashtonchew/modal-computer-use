from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.daemon.routes.execution import budget_policy, run_idle_only_mutation
from modal_computer_use.daemon.routes.validation import daemon_readiness
from modal_computer_use.models import ComputerStatus, LifecycleResult

router = APIRouter(prefix="/v1/computer")


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
        await request.app.state.supervisor.start()
        return LifecycleResult(ok=True, status="running")

    return await run_idle_only_mutation(request, operation, semantic_data={})


@router.post("/stop")
async def stop(request: Request) -> LifecycleResult:
    async def operation() -> LifecycleResult:
        request.app.state.backend.reset_screenshot_capture()
        await request.app.state.supervisor.stop()
        return LifecycleResult(ok=True, status="stopped")

    return await run_idle_only_mutation(request, operation, semantic_data={})


@router.post("/restart")
async def restart(request: Request) -> LifecycleResult:
    async def operation() -> LifecycleResult:
        request.app.state.backend.reset_screenshot_capture()
        await request.app.state.supervisor.restart()
        return LifecycleResult(ok=True, status="running")

    return await run_idle_only_mutation(request, operation, semantic_data={})
