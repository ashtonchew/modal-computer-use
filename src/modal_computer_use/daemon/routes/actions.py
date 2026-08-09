from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from io import BytesIO
from math import ceil
from time import perf_counter
from typing import Any

from fastapi import APIRouter, Header, Request, Response
from PIL import Image

from modal_computer_use.daemon.actions import ActionBatchContext, run_with_screenshot_bytes
from modal_computer_use.daemon.actions import run as run_batch
from modal_computer_use.daemon.actions import validate as validate_batch
from modal_computer_use.daemon.desktop.screenshots import (
    CapturedRawScreenshot,
    CapturedScreenshot,
    encode_image,
)
from modal_computer_use.daemon.desktop.xdamage import (
    ChangeSignal,
    PreparedChangeSignal,
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
    CoordinateSpace,
    Point,
    Region,
    ScreenshotOptions,
    ValidationResult,
    sha256_bytes,
)

router = APIRouter(prefix="/v1/actions")


def _prepare_change_signal(
    requested: ChangeSignal,
    *,
    show_cursor: bool,
    display: str | None,
) -> PreparedChangeSignal:
    if requested == "poll":
        return PreparedChangeSignal(requested=requested, active="poll")
    if show_cursor:
        return PreparedChangeSignal(
            requested=requested,
            active="poll",
            unavailable_reason="cursor-visible screenshots require pixel polling",
        )
    return prepare_change_signal(
        requested,
        display=display,
        watcher_factory=XDamageWatcher,
    )


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
        ActionBatchContext(request.app.state, request.headers),
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
        ActionBatchContext(request.app.state, request.headers),
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
    summary="Experimental: run actions and observe the first visual change",
    description=(
        "Runs one ordered action batch. Then it returns the first frame whose full-resolution "
        "pixels differ from the pre-action baseline. XDamage can wake the verifier, and pixel "
        "polling remains the fallback. This result does not establish application readiness, "
        "visual stability, or task completion."
    ),
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
    change_signal = _prepare_change_signal(
        payload.change_signal,
        show_cursor=options.show_cursor,
        display=getattr(request.app.state.backend, "display", None),
    )
    signal_prepare_ms = _elapsed_ms(signal_prepare_started)
    baseline_started = perf_counter()
    baseline_sha256 = payload.previous_source_sha256
    baseline_capture_ms = 0.0
    uses_polling = change_signal.active == "poll"
    if baseline_sha256 is None:
        baseline_frame = await _capture_source_frame(request, options=options, region=region)
        baseline_sha256 = baseline_frame.source_sha256
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
            ActionBatchContext(request.app.state, request.headers),
            idempotency_key=idempotency_key,
        )
        action_wall_ms = _elapsed_ms(action_started)
        capture_delay_wall_ms = 0.0
        if payload.capture_delay_ms > 0:
            capture_delay_started = perf_counter()
            await asyncio.sleep(payload.capture_delay_ms / 1000)
            capture_delay_wall_ms = _elapsed_ms(capture_delay_started)
        wait_started = perf_counter()
        attempts = 0
        source_sha256 = baseline_sha256
        detected = False
        timeout_reached = False
        verified_frame: _CapturedSourceFrame | None = None
        deadline = wait_started + payload.change_timeout_ms / 1000
        signal_wait_wall_ms = 0.0
        signal_wait_ms = 0.0
        verification_capture_ms = 0.0
        signal_detected = False
        last_signal_result: XDamageWaitResult | None = None
        last_detected_signal_result: XDamageWaitResult | None = None
        if change_signal.wait_watcher is not None:
            while True:
                remaining_ms = max(ceil((deadline - perf_counter()) * 1000), 0)
                signal_wait_started = perf_counter()
                current_signal_result = await asyncio.to_thread(
                    change_signal.wait_watcher.wait,
                    remaining_ms,
                )
                signal_wait_wall_ms += _elapsed_ms(signal_wait_started)
                signal_wait_ms += current_signal_result.wait_ms
                last_signal_result = current_signal_result
                if not current_signal_result.detected:
                    attempts += 1
                    verification_capture_started = perf_counter()
                    current_frame = await _capture_source_frame(
                        request,
                        options=options,
                        region=region,
                    )
                    verification_capture_ms += _elapsed_ms(verification_capture_started)
                    source_sha256 = current_frame.source_sha256
                    capture_completed_at = perf_counter()
                    detected = (
                        source_sha256 != baseline_sha256 and capture_completed_at <= deadline
                    )
                    verified_frame = current_frame
                    timeout_reached = not detected and capture_completed_at >= deadline
                    break
                signal_detected = True
                last_detected_signal_result = current_signal_result
                attempts += 1
                verification_capture_started = perf_counter()
                current_frame = await _capture_source_frame(
                    request,
                    options=options,
                    region=region,
                )
                verification_capture_ms += _elapsed_ms(verification_capture_started)
                source_sha256 = current_frame.source_sha256
                capture_completed_at = perf_counter()
                detected = source_sha256 != baseline_sha256 and capture_completed_at <= deadline
                if detected:
                    verified_frame = current_frame
                    break
                if capture_completed_at >= deadline:
                    verified_frame = current_frame
                    timeout_reached = True
                    break
            signal_result = _aggregate_signal_result(
                last_signal_result,
                last_detected_signal_result=last_detected_signal_result,
                detected=signal_detected,
                wait_ms=signal_wait_ms,
            )
        poll_ms = 0.0
        if uses_polling and baseline_sha256 is not None:
            poll_started = perf_counter()
            while True:
                attempts += 1
                current_frame = await _capture_source_frame(
                    request,
                    options=options,
                    region=region,
                )
                source_sha256 = current_frame.source_sha256
                capture_completed_at = perf_counter()
                detected = source_sha256 != baseline_sha256 and capture_completed_at <= deadline
                if detected or capture_completed_at >= deadline:
                    verified_frame = current_frame
                    timeout_reached = not detected and capture_completed_at >= deadline
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
        if verified_frame is not None:
            shot = await asyncio.to_thread(
                _encode_verified_screenshot,
                verified_frame,
                options,
            )
        else:

            async def operation():
                return await request.app.state.backend.screenshot_bytes(
                    options,
                    prefer_native_png=True,
                )

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
        "verification_capture_ms": verification_capture_ms,
        "poll_ms": poll_ms,
        "screenshot_ms": screenshot_ms,
        "total_ms": _elapsed_ms(started),
    }
    change_result = {
        "detected": detected,
        "attempts": attempts,
        "timeout_reached": timeout_reached,
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


@dataclass(frozen=True)
class _CapturedSourceFrame:
    source_sha256: str
    raw: CapturedRawScreenshot


async def _capture_source_frame(
    request: Request,
    *,
    options: ScreenshotOptions,
    region: Region | None,
) -> _CapturedSourceFrame:
    async def raw_operation():
        if options.show_cursor:
            return None
        return await request.app.state.backend.screenshot_raw_pixels(region=None)

    raw = await run_screenshot_capture(request, raw_operation)
    if raw is not None:
        source_sha256 = (
            raw.sha256
            if region is None
            else await asyncio.to_thread(_raw_detection_sha256, raw, region=region)
        )
        return _CapturedSourceFrame(
            source_sha256=source_sha256,
            raw=raw,
        )

    async def operation():
        canonical_options = ScreenshotOptions(
            format="png",
            quality=100,
            scale=1.0,
            show_cursor=options.show_cursor,
        )
        return await request.app.state.backend.screenshot_bytes(
            canonical_options,
            region=None,
            prefer_native_png=True,
        )

    shot = await run_screenshot_capture(request, operation)
    canonical_raw = await asyncio.to_thread(
        _decode_canonical_screenshot,
        shot,
    )
    source_sha256 = (
        canonical_raw.sha256
        if region is None
        else await asyncio.to_thread(_raw_detection_sha256, canonical_raw, region=region)
    )
    return _CapturedSourceFrame(source_sha256=source_sha256, raw=canonical_raw)


def _raw_detection_sha256(raw: CapturedRawScreenshot, *, region: Region) -> str:
    image = Image.frombytes("RGB", (raw.width, raw.height), raw.rgb)
    box = _detection_box(raw.coordinate_space, region)
    return sha256_bytes(image.crop(box).tobytes())


def _decode_canonical_screenshot(shot: CapturedScreenshot) -> CapturedRawScreenshot:
    with Image.open(BytesIO(shot.data)) as encoded:
        image = encoded.convert("RGB")
    rgb = image.tobytes()
    return CapturedRawScreenshot(
        width=image.width,
        height=image.height,
        rgb=rgb,
        sha256=sha256_bytes(rgb),
        captured_at=shot.captured_at,
        coordinate_space=shot.coordinate_space,
        cursor_visible=shot.cursor_visible,
        capture_backend=shot.capture_backend,
        timings_ms=shot.timings_ms,
    )


def _detection_box(
    coordinate_space: CoordinateSpace,
    region: Region,
) -> tuple[int, int, int, int]:
    top_left = coordinate_space.to_image(Point(x=region.x, y=region.y))
    bottom_right = coordinate_space.to_image(Point(x=region.right, y=region.bottom))
    return top_left.x, top_left.y, bottom_right.x, bottom_right.y


def _aggregate_signal_result(
    last_result: XDamageWaitResult | None,
    *,
    last_detected_signal_result: XDamageWaitResult | None,
    detected: bool,
    wait_ms: float,
) -> XDamageWaitResult | None:
    if last_result is None:
        return None
    detected_result = last_detected_signal_result or last_result
    return XDamageWaitResult(
        available=last_result.available,
        detected=detected,
        wait_ms=wait_ms,
        reason=last_result.reason,
        version=last_result.version or detected_result.version,
        dirty_rect=detected_result.dirty_rect,
        dirty_rects=detected_result.dirty_rects,
    )


def _encode_verified_screenshot(
    frame: _CapturedSourceFrame,
    options: ScreenshotOptions,
) -> CapturedScreenshot:
    raw = frame.raw
    encode_started = perf_counter()
    image = Image.frombytes("RGB", (raw.width, raw.height), raw.rgb)
    image_width = max(1, round(raw.width * options.scale))
    image_height = max(1, round(raw.height * options.scale))
    if options.scale != 1.0:
        image = image.resize((image_width, image_height))
    data = encode_image(image, options.format, options.quality)
    coordinate_space = CoordinateSpace.from_dimensions(
        desktop_width=raw.coordinate_space.desktop_width,
        desktop_height=raw.coordinate_space.desktop_height,
        image_width=image_width,
        image_height=image_height,
        source_region=raw.coordinate_space.source_region,
    )
    return CapturedScreenshot(
        format=options.format,
        width=image_width,
        height=image_height,
        data=data,
        sha256=sha256_bytes(data),
        captured_at=raw.captured_at,
        coordinate_space=coordinate_space,
        cursor_visible=raw.cursor_visible,
        capture_backend=raw.capture_backend,
        timings_ms={**raw.timings_ms, "encode_ms": _elapsed_ms(encode_started)},
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


def _json_header(payload: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode(
        "ascii"
    )


def _elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1000
