from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.daemon import budgets
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
            budgets=budgets.snapshot(request),
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
        budgets=budgets.snapshot(request),
    )


@router.post("/start")
async def start(request: Request) -> LifecycleResult:
    budgets.enforce_idle(request)
    await request.app.state.supervisor.start()
    budgets.touch_activity(request)
    return LifecycleResult(ok=True, status="running")


@router.post("/stop")
async def stop(request: Request) -> LifecycleResult:
    budgets.enforce_idle(request)
    await request.app.state.supervisor.stop()
    budgets.touch_activity(request)
    return LifecycleResult(ok=True, status="stopped")


@router.post("/restart")
async def restart(request: Request) -> LifecycleResult:
    budgets.enforce_idle(request)
    await request.app.state.supervisor.restart()
    budgets.touch_activity(request)
    return LifecycleResult(ok=True, status="running")
