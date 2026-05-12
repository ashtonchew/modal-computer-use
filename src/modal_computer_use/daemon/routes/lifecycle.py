from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.daemon import budgets
from modal_computer_use.models import ComputerStatus, LifecycleResult

router = APIRouter(prefix="/v1/computer")


@router.get("/status")
async def status(request: Request) -> ComputerStatus:
    backend = request.app.state.backend
    ready, _ = await backend.ready()
    return ComputerStatus(
        status="running" if ready else "degraded",
        ready=ready,
        display=request.app.state.settings.display,
        width=backend.width,
        height=backend.height,
        processes=request.app.state.supervisor.statuses(),
        resources={"profile": request.app.state.settings.image_profile},
        budgets=budgets.snapshot(request),
    )


@router.post("/start")
async def start(request: Request) -> LifecycleResult:
    await request.app.state.supervisor.start()
    return LifecycleResult(ok=True, status="running")


@router.post("/stop")
async def stop(request: Request) -> LifecycleResult:
    await request.app.state.supervisor.stop()
    return LifecycleResult(ok=True, status="stopped")


@router.post("/restart")
async def restart(request: Request) -> LifecycleResult:
    await request.app.state.supervisor.restart()
    return LifecycleResult(ok=True, status="running")
