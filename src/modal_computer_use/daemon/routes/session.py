from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.models import SandboxRef

router = APIRouter(prefix="/v1/session")


def _metadata(request: Request) -> SandboxRef:
    settings = request.app.state.settings
    return SandboxRef(
        sandbox_id=settings.run_id or "local",
        app_name="local-daemon",
        name=None,
        run_id=settings.run_id,
        status="ready",
        tags={"computer-use": "true"},
        artifacts_dir=str(settings.artifacts_dir),
    )


@router.get("/metadata")
async def metadata(request: Request) -> SandboxRef:
    return _metadata(request)


@router.post("/refresh")
async def refresh(request: Request) -> SandboxRef:
    return _metadata(request)
