from __future__ import annotations

from fastapi import APIRouter, Request, Response

from modal_computer_use._version import __version__
from modal_computer_use.daemon.leases import LEASE_PROTOCOL_VERSION
from modal_computer_use.daemon.routes.validation import daemon_readiness
from modal_computer_use.models import Capabilities, ReadyStatus, VersionInfo

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@router.get(
    "/readyz",
    responses={
        503: {
            "model": ReadyStatus,
            "description": "Desktop backend or required process is not ready",
        }
    },
)
async def readyz(request: Request, response: Response) -> ReadyStatus:
    ready, errors = await daemon_readiness(request)
    if not ready:
        response.status_code = 503
    return ReadyStatus(ready=ready, errors=errors)


@router.get("/v1/version")
async def version(request: Request, response: Response) -> VersionInfo:
    response.headers["x-computer-use-lease-protocol"] = LEASE_PROTOCOL_VERSION
    return VersionInfo(
        daemon_version=__version__,
        sdk_min_version="1.0.0",
        sdk_max_version="1.x",
        image_profile=request.app.state.settings.image_profile,
        modal_computer_use_package=__version__,
    )


@router.get("/v1/capabilities")
async def capabilities(request: Request, response: Response) -> Capabilities:
    backend = request.app.state.backend
    response.headers["x-computer-use-lease-protocol"] = LEASE_PROTOCOL_VERSION
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
            "trajectory-leases-v1",
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
        input_backend=getattr(backend, "input_backend", None),
        input_backend_configured=getattr(backend, "configured_input_backend", None),
        input_backends_supported=_sorted_strings(getattr(backend, "supported_input_backends", ())),
        input_backends_available=_sorted_strings(getattr(backend, "available_input_backends", ())),
    )


def _sorted_strings(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return sorted(item for item in value if isinstance(item, str))
    return []
