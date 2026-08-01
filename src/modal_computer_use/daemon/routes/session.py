from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.tunnel_sessions import TunnelSessionLimitError
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
    auth_kind = getattr(request.state, "auth_kind", None)
    if auth_kind not in {"connect", "bootstrap_tunnel"}:
        raise DaemonError(
            "verified Connect or bootstrap tunnel authentication is required",
            status_code=403,
            code="tunnel_authorization_not_allowed",
        )
    ttl_seconds = request.app.state.settings.tunnel_token_ttl_seconds
    try:
        token, expires_at = request.app.state.tunnel_sessions.mint(ttl_seconds)
    except TunnelSessionLimitError as exc:
        raise DaemonError(
            "active tunnel session limit reached",
            status_code=429,
            code="tunnel_session_limit_reached",
        ) from exc
    return {
        "token": token,
        "token_type": "Bearer",
        "expires_in": ttl_seconds,
        "expires_at": expires_at,
    }
