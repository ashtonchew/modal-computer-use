from __future__ import annotations

import asyncio
import base64
import json
from time import perf_counter
from typing import Any

from fastapi import APIRouter, Header, Request, Response

from modal_computer_use.daemon.actions import ActionBatchContext, run_with_screenshot_bytes
from modal_computer_use.daemon.actions import run as run_batch
from modal_computer_use.daemon.actions import validate as validate_batch
from modal_computer_use.daemon.desktop.xdamage import (
    XDamageWaitResult,
    XDamageWatcher,
    prepare_change_signal,
)
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.routes.execution import run_screenshot_capture
from modal_computer_use.daemon.routes.screenshots import (
    _screenshot_headers,
    enforce_screenshot_options_pixels,
)
from modal_computer_use.daemon.routes.validation import validate_region
from modal_computer_use.daemon.schemas import ActionObserveChangeScreenshotRequest
from modal_computer_use.models import (
    ActionBatchRequest,
    ActionBatchResult,
    Region,
    ScreenshotOptions,
    ValidationResult,
)

router = APIRouter(prefix="/v1/actions")


@router.post("/validate")
async def validate(payload: ActionBatchRequest, request: Request) -> ValidationResult:
    return await validate_batch(payload, ActionBatchContext(request.app.state))


@router.post("/run")
async def run(
    payload: ActionBatchRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ActionBatchResult:
    return await run_batch(
        payload,
        ActionBatchContext(request.app.state),
        idempotency_key=idempotency_key,
    )


@router.post(
    "/run/raw-screenshot",
    responses={200: {"content": {"image/png": {}, "image/jpeg": {}, "image/webp": {}}}},
)
async def run_raw_screenshot(
    payload: ActionBatchRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    result, shot = await run_with_screenshot_bytes(
        payload,
        ActionBatchContext(request.app.state),
        idempotency_key=idempotency_key,
    )
    if shot is None:
        raise DaemonError(
            "action batch did not capture a raw screenshot",
            status_code=409,
            code="raw_screenshot_after_not_captured",
            details={"result": result.model_dump(mode="json")},
        )
    action_result = result.model_dump_json(exclude={"screenshot"})
    headers = {
        **_screenshot_headers(shot),
        "x-computer-use-action-result": base64.b64encode(action_result.encode("utf-8")).decode(
            "ascii"
        ),
    }
    return Response(
        content=shot.data,
        media_type=f"image/{shot.format}",
        headers=headers,
    )


@router.post(
    "/run/observe-change/raw-screenshot",
    responses={200: {"content": {"image/png": {}, "image/jpeg": {}, "image/webp": {}}}},
)
async def run_observe_change_raw_screenshot(
    payload: ActionObserveChangeScreenshotRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    started = perf_counter()
    options = payload.screenshot_options
    enforce_screenshot_options_pixels(
        request,
        source_width=request.app.state.backend.width,
        source_height=request.app.state.backend.height,
        scale=options.scale,
    )
    region = _resolve_change_detection_region(request, payload)
    signal_prepare_started = perf_counter()
    change_signal = prepare_change_signal(
        payload.change_signal,
        display=getattr(request.app.state.backend, "display", None),
        watcher_factory=XDamageWatcher,
    )
    signal_prepare_ms = _elapsed_ms(signal_prepare_started)
    baseline_started = perf_counter()
    baseline_sha256 = payload.previous_source_sha256
    baseline_capture_ms = 0.0
    needs_poll_baseline = change_signal.active == "poll"
    if needs_poll_baseline and baseline_sha256 is None:
        baseline_sha256 = await _capture_source_sha256(request, region=region)
        baseline_capture_ms = _elapsed_ms(baseline_started)
    action_request = ActionBatchRequest.model_validate(
        payload.model_dump(
            mode="json",
            exclude={
                "screenshot_options",
                "previous_source_sha256",
                "capture_delay_ms",
                "change_timeout_ms",
                "poll_interval_ms",
                "poll_strategy",
                "change_detection",
                "change_signal",
                "change_detection_region",
                "change_region_radius",
            },
        )
    )
    signal_result: XDamageWaitResult | None = None
    try:
        action_started = perf_counter()
        action_result = await run_batch(
            action_request,
            ActionBatchContext(request.app.state),
            idempotency_key=idempotency_key,
        )
        action_wall_ms = _elapsed_ms(action_started)
        capture_delay_wall_ms = 0.0
        if payload.capture_delay_ms > 0:
            capture_delay_started = perf_counter()
            await asyncio.sleep(payload.capture_delay_ms / 1000)
            capture_delay_wall_ms = _elapsed_ms(capture_delay_started)
        wait_started = perf_counter()
        signal_wait_wall_ms = 0.0
        if change_signal.wait_watcher is not None:
            signal_wait_started = perf_counter()
            signal_result = await asyncio.to_thread(
                change_signal.wait_watcher.wait,
                payload.change_timeout_ms,
            )
            signal_wait_wall_ms = _elapsed_ms(signal_wait_started)
        attempts = 0
        source_sha256 = baseline_sha256
        detected = bool(signal_result.detected) if signal_result is not None else False
        poll_ms = 0.0
        if needs_poll_baseline and baseline_sha256 is not None:
            poll_started = perf_counter()
            deadline = wait_started + payload.change_timeout_ms / 1000
            while True:
                attempts += 1
                source_sha256 = await _capture_source_sha256(request, region=region)
                detected = source_sha256 != baseline_sha256
                if detected or perf_counter() >= deadline:
                    break
                await asyncio.sleep(
                    _change_poll_sleep_ms(
                        attempt=attempts,
                        poll_interval_ms=payload.poll_interval_ms,
                        poll_strategy=payload.poll_strategy,
                    )
                    / 1000
                )
            poll_ms = _elapsed_ms(poll_started)
        screenshot_started = perf_counter()

        async def operation():
            return await request.app.state.backend.screenshot_bytes(options, prefer_native_png=True)

        shot = await run_screenshot_capture(request, operation)
    finally:
        change_signal.close()
    screenshot_ms = _elapsed_ms(screenshot_started)
    change_timing = {
        "baseline_capture_ms": baseline_capture_ms,
        "signal_prepare_ms": signal_prepare_ms,
        "action_wall_ms": action_wall_ms,
        "capture_delay_wall_ms": capture_delay_wall_ms,
        "signal_wait_wall_ms": signal_wait_wall_ms,
        "poll_ms": poll_ms,
        "screenshot_ms": screenshot_ms,
        "total_ms": _elapsed_ms(started),
    }
    change_result = {
        "detected": detected,
        "attempts": attempts,
        "timeout_reached": not detected,
        "baseline_source_sha256": baseline_sha256,
        "source_sha256": source_sha256,
        "change_detection": payload.change_detection,
        "change_detection_region": region.model_dump(mode="json") if region is not None else None,
        **change_signal.metadata(signal_result),
    }
    headers = {
        **_screenshot_headers(shot),
        "x-computer-use-action-result": _json_header(
            action_result.model_dump(mode="json", exclude={"screenshot"})
        ),
        "x-computer-use-change-result": _json_header(change_result),
        "x-computer-use-change-timing-ms": json.dumps(change_timing, separators=(",", ":")),
    }
    return Response(content=shot.data, media_type=f"image/{shot.format}", headers=headers)


async def _capture_source_sha256(request: Request, *, region: Region | None) -> str:
    async def raw_operation():
        return await request.app.state.backend.screenshot_raw_pixels(region=region)

    raw = await run_screenshot_capture(request, raw_operation)
    if raw is not None:
        return raw.sha256

    async def operation():
        return await request.app.state.backend.screenshot_bytes(
            ScreenshotOptions(format="png", show_cursor=False),
            region=region,
            prefer_native_png=True,
        )

    shot = await run_screenshot_capture(request, operation)
    return shot.sha256


def _resolve_change_detection_region(
    request: Request,
    payload: ActionObserveChangeScreenshotRequest,
) -> Region | None:
    if payload.change_detection == "full":
        return None
    region = payload.change_detection_region
    if region is None and payload.change_detection == "auto_region":
        region = _auto_change_region(
            payload.actions,
            width=request.app.state.backend.width,
            height=request.app.state.backend.height,
            radius=payload.change_region_radius,
        )
    if region is None:
        return None
    validate_region(request, region, field="change_detection_region")
    return region


def _auto_change_region(
    actions: list[Any],
    *,
    width: int,
    height: int,
    radius: int,
) -> Region | None:
    for action in reversed(actions):
        x = getattr(action, "x", None)
        y = getattr(action, "y", None)
        if not isinstance(x, int) or not isinstance(y, int):
            x = getattr(action, "end_x", None)
            y = getattr(action, "end_y", None)
        if not isinstance(x, int) or not isinstance(y, int):
            continue
        left = max(x - radius, 0)
        top = max(y - radius, 0)
        right = min(x + radius, width)
        bottom = min(y + radius, height)
        if right <= left or bottom <= top:
            return None
        return Region(x=left, y=top, width=right - left, height=bottom - top)
    return None


def _change_poll_sleep_ms(
    *,
    attempt: int,
    poll_interval_ms: int,
    poll_strategy: str,
) -> int:
    if poll_strategy != "adaptive":
        return poll_interval_ms
    return min(4 * (2 ** max(attempt - 1, 0)), poll_interval_ms)


def _json_header(payload: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode(
        "ascii"
    )


def _elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1000
