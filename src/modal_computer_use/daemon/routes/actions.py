from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from math import ceil
from time import perf_counter
from typing import Any

from fastapi import APIRouter, Header, Request, Response

from modal_computer_use.daemon.actions import ActionBatchContext, run_with_screenshot_bytes
from modal_computer_use.daemon.actions import run as run_batch
from modal_computer_use.daemon.actions import validate as validate_batch
from modal_computer_use.daemon.desktop.screenshots import (
    CapturedRawScreenshot,
    try_encode_captured_raw,
)
from modal_computer_use.daemon.desktop.xdamage import XDamageWaitResult, XDamageWatcher
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


@dataclass(frozen=True)
class _PreparedActionChangeSignal:
    requested: str
    active: str
    watcher: XDamageWatcher | None = None
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class _SourceConfirmation:
    source_sha256: str
    raw_frame: CapturedRawScreenshot | None


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
    change_signal = _prepare_action_change_signal(request, payload.change_signal)
    signal_prepare_ms = _elapsed_ms(signal_prepare_started)
    baseline_started = perf_counter()
    baseline_sha256 = payload.previous_source_sha256
    baseline_capture_ms = 0.0
    needs_poll_baseline = change_signal.active == "poll"
    if needs_poll_baseline and baseline_sha256 is None:
        baseline_sha256 = (
            await _capture_source_confirmation(request, region=region)
        ).source_sha256
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
        deadline = wait_started + payload.change_timeout_ms / 1000
        signal_wait_wall_ms = 0.0
        if change_signal.watcher is not None:
            signal_wait_started = perf_counter()
            signal_result = await asyncio.to_thread(
                change_signal.watcher.wait,
                _remaining_timeout_ms(deadline),
            )
            signal_wait_wall_ms = _elapsed_ms(signal_wait_started)
        attempts = 0
        source_sha256 = baseline_sha256
        detected = (
            bool(signal_result.detected)
            if signal_result is not None and baseline_sha256 is None
            else False
        )
        source_confirmation_active = baseline_sha256 is not None
        source_confirmation_fallback_used = False
        source_confirmation_fallback_reason: str | None = None
        confirmed_raw_frame: CapturedRawScreenshot | None = None
        poll_ms = 0.0
        should_poll = needs_poll_baseline and baseline_sha256 is not None
        if change_signal.active == "xdamage" and baseline_sha256 is not None:
            if signal_result is None or not signal_result.available:
                source_confirmation_fallback_used = True
                source_confirmation_fallback_reason = "wait_unavailable"
                should_poll = True
            elif not signal_result.detected:
                source_confirmation_fallback_used = True
                source_confirmation_fallback_reason = "signal_timeout"
                should_poll = True
            else:
                poll_started = perf_counter()
                attempts += 1
                confirmation = await _capture_source_confirmation(request, region=region)
                source_sha256 = confirmation.source_sha256
                detected = source_sha256 != baseline_sha256
                if detected:
                    confirmed_raw_frame = confirmation.raw_frame
                poll_ms = _elapsed_ms(poll_started)
                if not detected:
                    source_confirmation_fallback_used = True
                    source_confirmation_fallback_reason = "source_unchanged"
                    should_poll = True
        elif (
            change_signal.active == "poll"
            and change_signal.requested != "poll"
            and baseline_sha256 is not None
        ):
            source_confirmation_fallback_used = True
            source_confirmation_fallback_reason = "setup_unavailable"
        if should_poll and not detected:
            poll_started = perf_counter()
            while perf_counter() < deadline or (needs_poll_baseline and attempts == 0):
                if attempts > 0:
                    sleep_ms = _change_poll_sleep_ms(
                        attempt=attempts,
                        poll_interval_ms=payload.poll_interval_ms,
                        poll_strategy=payload.poll_strategy,
                    )
                    remaining_ms = _remaining_timeout_ms(deadline)
                    if remaining_ms <= 0:
                        break
                    await asyncio.sleep(min(sleep_ms, remaining_ms) / 1000)
                    if perf_counter() >= deadline:
                        break
                attempts += 1
                confirmation = await _capture_source_confirmation(request, region=region)
                source_sha256 = confirmation.source_sha256
                detected = source_sha256 != baseline_sha256
                if detected:
                    confirmed_raw_frame = confirmation.raw_frame
                if detected or perf_counter() >= deadline:
                    break
            poll_ms += _elapsed_ms(poll_started)
        screenshot_started = perf_counter()
        shot = try_encode_captured_raw(
            confirmed_raw_frame,
            options,
            output_region=None,
        )
        confirmed_frame_reused = shot is not None
        if shot is None:

            async def operation():
                return await request.app.state.backend.screenshot_bytes(
                    options,
                    prefer_native_png=True,
                )

            shot = await run_screenshot_capture(request, operation)
    finally:
        if change_signal.watcher is not None:
            change_signal.watcher.close()
    screenshot_ms = _elapsed_ms(screenshot_started)
    confirmed_frame_encode_ms = screenshot_ms if confirmed_frame_reused else 0.0
    fresh_final_capture_ms = screenshot_ms if not confirmed_frame_reused else 0.0
    change_timing = {
        "baseline_capture_ms": baseline_capture_ms,
        "signal_prepare_ms": signal_prepare_ms,
        "action_wall_ms": action_wall_ms,
        "capture_delay_wall_ms": capture_delay_wall_ms,
        "signal_wait_wall_ms": signal_wait_wall_ms,
        "poll_ms": poll_ms,
        "screenshot_ms": screenshot_ms,
        "confirmed_frame_encode_ms": confirmed_frame_encode_ms,
        "fresh_final_capture_ms": fresh_final_capture_ms,
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
        "change_signal_requested": change_signal.requested,
        "change_signal_active": change_signal.active,
        "change_signal_available": _change_signal_available(signal_result, change_signal),
        "change_signal_detected": None if signal_result is None else signal_result.detected,
        "change_signal_wait_ms": None if signal_result is None else signal_result.wait_ms,
        "change_signal_reason": _change_signal_reason(signal_result, change_signal),
        "change_signal_version": None if signal_result is None else signal_result.version,
        "source_confirmation_active": source_confirmation_active,
        "source_confirmation_attempts": attempts,
        "source_confirmation_fallback_used": source_confirmation_fallback_used,
        "source_confirmation_fallback_reason": source_confirmation_fallback_reason,
        "confirmed_frame_reused": confirmed_frame_reused,
        "final_frame_source": "source_confirmation" if confirmed_frame_reused else "fresh_capture",
        "final_desktop_capture_performed": not confirmed_frame_reused,
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


async def _capture_source_confirmation(
    request: Request,
    *,
    region: Region | None,
) -> _SourceConfirmation:
    async def raw_operation():
        return await request.app.state.backend.screenshot_raw_pixels(region=region)

    raw = await run_screenshot_capture(request, raw_operation)
    if raw is not None:
        return _SourceConfirmation(source_sha256=raw.sha256, raw_frame=raw)

    async def encoded_operation():
        return await request.app.state.backend.screenshot_bytes(
            ScreenshotOptions(format="png", show_cursor=False),
            region=region,
            prefer_native_png=True,
        )

    shot = await run_screenshot_capture(request, encoded_operation)
    return _SourceConfirmation(source_sha256=shot.sha256, raw_frame=None)


def _prepare_action_change_signal(
    request: Request,
    requested: str,
) -> _PreparedActionChangeSignal:
    if requested == "poll":
        return _PreparedActionChangeSignal(requested=requested, active="poll")
    display = getattr(request.app.state.backend, "display", None)
    if not isinstance(display, str) or not display:
        return _PreparedActionChangeSignal(
            requested=requested,
            active="poll",
            unavailable_reason="backend has no X11 display",
        )
    watcher = XDamageWatcher(display=display)
    try:
        watcher.arm()
    except Exception:
        if requested == "auto":
            watcher.close()
            return _PreparedActionChangeSignal(
                requested=requested,
                active="poll",
                unavailable_reason=watcher.failure or "XDamage unavailable",
            )
        return _PreparedActionChangeSignal(
            requested=requested,
            active="xdamage",
            watcher=watcher,
            unavailable_reason=watcher.failure or "XDamage unavailable",
        )
    return _PreparedActionChangeSignal(
        requested=requested,
        active="xdamage",
        watcher=watcher,
    )


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


def _remaining_timeout_ms(deadline: float) -> int:
    return max(0, ceil((deadline - perf_counter()) * 1000))


def _change_signal_available(
    result: XDamageWaitResult | None,
    signal: _PreparedActionChangeSignal,
) -> bool | None:
    if signal.active == "poll":
        return False if signal.requested != "poll" else None
    if result is None:
        return None
    return result.available


def _change_signal_reason(
    result: XDamageWaitResult | None,
    signal: _PreparedActionChangeSignal,
) -> str | None:
    if result is not None:
        if not result.available:
            return "unavailable"
        if not result.detected:
            return "timeout"
        return None
    if signal.unavailable_reason is not None:
        return "unavailable"
    return None


def _json_header(payload: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode(
        "ascii"
    )


def _elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1000
