from __future__ import annotations

import secrets
import time

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


@router.post("/tunnel-authorize")
async def tunnel_authorize(request: Request) -> dict[str, object]:
    token = secrets.token_urlsafe(32)
    ttl_seconds = request.app.state.settings.tunnel_token_ttl_seconds
    expires_at = time.time() + ttl_seconds
    request.app.state.tunnel_sessions[token] = expires_at
    return {
        "token": token,
        "token_type": "Bearer",
        "expires_in": ttl_seconds,
        "expires_at": expires_at,
    }
