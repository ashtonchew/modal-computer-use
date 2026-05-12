from __future__ import annotations

from fastapi import APIRouter, Request

from modal_computer_use.models import DebugUrls

router = APIRouter(prefix="/v1/debug")


@router.get("/urls")
async def urls(request: Request) -> DebugUrls:
    return DebugUrls(
        vnc=None,
        daemon=None,
        recording_dashboard=None,
    )
