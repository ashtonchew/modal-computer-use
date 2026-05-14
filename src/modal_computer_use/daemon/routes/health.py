from __future__ import annotations

from fastapi import APIRouter, Request, Response

from modal_computer_use._version import __version__
from modal_computer_use.daemon.routes.validation import desktop_readiness
from modal_computer_use.models import Capabilities, ReadyStatus, VersionInfo

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> ReadyStatus:
    ready, errors = await desktop_readiness(request)
    if request.app.state.settings.vnc_mode != "off":
        # noVNC health is process-supervisor based until a browser-accessible tunnel exists.
        x11vnc = request.app.state.supervisor.status("x11vnc")
        novnc = request.app.state.supervisor.status("novnc")
        if x11vnc.status not in ("running", "unknown"):
            ready = False
            errors.append("x11vnc is not running")
        if novnc.status not in ("running", "unknown"):
            ready = False
            errors.append("novnc is not running")
    if not ready:
        response.status_code = 503
    return ReadyStatus(ready=ready, errors=errors)


@router.get("/v1/version")
async def version(request: Request) -> VersionInfo:
    return VersionInfo(
        daemon_version=__version__,
        sdk_min_version="1.0.0",
        sdk_max_version="1.x",
        image_profile=request.app.state.settings.image_profile,
        modal_computer_use_package=__version__,
    )


@router.get("/v1/capabilities")
async def capabilities(request: Request) -> Capabilities:
    return Capabilities(
        primitives=[
            "mouse",
            "keyboard",
            "clipboard",
            "screenshots",
            "recordings",
            "display",
            "windows",
            "actions",
            "artifacts",
            "browser",
            "apps",
            "commands",
            "input",
            "lifecycle",
            "processes",
            "session",
            "debug",
        ],
        screenshot_formats=["png", "jpeg", "webp"],
        action_types=[
            "move",
            "click",
            "double_click",
            "triple_click",
            "drag",
            "scroll",
            "mouse_down",
            "mouse_up",
            "type",
            "keypress",
            "hotkey",
            "hold_key",
            "wait",
            "screenshot",
            "zoom",
            "cursor_position",
            "release_all",
        ],
        adapter_versions={
            "anthropic": ["computer_20241022", "computer_20250124", "computer_20251124"],
            "openai": ["computer-use"],
        },
        image_profile=request.app.state.settings.image_profile,
        vnc_enabled=request.app.state.settings.vnc_mode != "off",
    )
