from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import BytesIO
from time import perf_counter
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from PIL import Image, ImageChops
from pydantic import ValidationError

from modal_computer_use.daemon.actions import ActionBatchContext
from modal_computer_use.daemon.actions import run as run_batch
from modal_computer_use.daemon.desktop.screenshots import (
    CapturedRawScreenshot,
    encode_image,
    encode_rgb_png,
)
from modal_computer_use.daemon.desktop.tile_diff import (
    crop_rgb,
    dirty_rect_from_tiles,
    native_hash_available,
    tile_hashes_rgb,
)
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.routes.execution import run_screenshot_capture
from modal_computer_use.daemon.routes.screenshots import (
    enforce_screenshot_options_pixels,
)
from modal_computer_use.daemon.routes.validation import validate_region
from modal_computer_use.daemon.routes.websocket_auth import daemon_websocket_auth_error
from modal_computer_use.daemon.schemas import (
    ObservationActionCaptureRequest,
    ObservationActionObserveChangeRequest,
    ObservationStreamRequest,
)
from modal_computer_use.models import ActionBatchRequest, Region, ScreenshotOptions
from modal_computer_use.redaction import sanitize_payload, sanitize_text

router = APIRouter(prefix="/v1/observations")

PROTOCOL = "computer-use.observation-stream.v1"


@dataclass
class _StreamState:
    request: ObservationStreamRequest | None = None
    stream_id: str | None = None
    paused: bool = False
    seq: int = 0
    last_sha256: str | None = None
    last_source_sha256: str | None = None
    last_image: Image.Image | None = None
    last_tile_hashes: dict[tuple[int, int], bytes] | None = None
    last_frame_seq: int | None = None
    emitted_frames: int = 0
    started_at: float = 0.0
    next_frame_at: float = 0.0


@router.websocket("/stream")
async def observation_stream(websocket: WebSocket) -> None:
    auth_error = daemon_websocket_auth_error(websocket)
    if auth_error is not None:
        await websocket.close(code=1008, reason=auth_error)
        return
    await websocket.accept()
    await websocket.send_json({"type": "ready", "protocol": PROTOCOL})
    state = _StreamState()
    try:
        while True:
            if state.request is not None and not state.paused:
                now = perf_counter()
                if state.next_frame_at <= now:
                    await _send_next_frame(websocket, state, trigger="scheduled")
                    now = perf_counter()
                timeout = None if state.request is None else max(state.next_frame_at - now, 0.0)
            else:
                timeout = None
            try:
                message = await asyncio.wait_for(websocket.receive_json(), timeout=timeout)
            except TimeoutError:
                continue
            await _handle_observation_message(websocket, state, message)
    except WebSocketDisconnect:
        return


async def _handle_observation_message(
    websocket: WebSocket,
    state: _StreamState,
    message: Any,
) -> None:
    if not isinstance(message, dict):
        await _send_observation_error(
            websocket,
            None,
            "invalid_message",
            "message must be a JSON object",
        )
        return
    request_id = message.get("id")
    if not isinstance(request_id, str) or not request_id:
        await _send_observation_error(websocket, None, "invalid_message", "message id is required")
        return
    op = message.get("op")
    payload = message.get("payload") or {}
    if not isinstance(payload, dict):
        await _send_observation_error(
            websocket,
            request_id,
            "invalid_payload",
            "payload must be an object",
        )
        return
    try:
        if op == "ping":
            await _send_empty_result(websocket, request_id)
        elif op == "start":
            await _start_stream(websocket, state, request_id, payload)
        elif op == "pause":
            _require_started(state)
            state.paused = True
            await _send_empty_result(websocket, request_id)
        elif op == "resume":
            _require_started(state)
            state.paused = False
            await _send_empty_result(websocket, request_id)
        elif op == "configure":
            await _configure_stream(websocket, state, request_id, payload)
        elif op == "request_keyframe":
            _require_started(state)
            state.last_sha256 = None
            state.last_source_sha256 = None
            state.last_image = None
            state.last_tile_hashes = None
            state.last_frame_seq = None
            await _send_empty_result(websocket, request_id)
        elif op == "capture_now":
            _require_started(state)
            await _send_next_frame(
                websocket,
                state,
                trigger="capture_now",
                request_id=request_id,
            )
        elif op == "run_actions_capture":
            _require_started(state)
            stream_request = ObservationActionCaptureRequest.model_validate(payload)
            action_request = ActionBatchRequest.model_validate(
                stream_request.model_dump(mode="json", exclude={"capture_delay_ms"})
            )
            action_result = await run_batch(action_request, ActionBatchContext(websocket.app.state))
            if stream_request.capture_delay_ms > 0:
                await asyncio.sleep(stream_request.capture_delay_ms / 1000)
            await _send_next_frame(
                websocket,
                state,
                trigger="run_actions_capture",
                request_id=request_id,
                extra_metadata={
                    "action_result": action_result.model_dump(mode="json"),
                    "capture_delay_ms": stream_request.capture_delay_ms,
                },
            )
        elif op == "run_actions_observe_change":
            _require_started(state)
            stream_request = ObservationActionObserveChangeRequest.model_validate(payload)
            action_request = ActionBatchRequest.model_validate(
                stream_request.model_dump(
                    mode="json",
                    exclude={
                        "capture_delay_ms",
                        "change_timeout_ms",
                        "poll_interval_ms",
                        "poll_strategy",
                        "change_detection",
                        "change_detection_region",
                        "change_region_radius",
                    },
                )
            )
            region = _resolve_change_detection_region(websocket, stream_request)
            region_baseline_sha256 = None
            if region is not None:
                region_baseline_sha256 = await _capture_region_source_sha256(
                    websocket,
                    region=region,
                )
            action_result = await run_batch(action_request, ActionBatchContext(websocket.app.state))
            if stream_request.capture_delay_ms > 0:
                await asyncio.sleep(stream_request.capture_delay_ms / 1000)
            await _send_changed_frame(
                websocket,
                state,
                trigger="run_actions_observe_change",
                request_id=request_id,
                timeout_ms=stream_request.change_timeout_ms,
                poll_interval_ms=stream_request.poll_interval_ms,
                poll_strategy=stream_request.poll_strategy,
                region=region,
                region_baseline_sha256=region_baseline_sha256,
                extra_metadata={
                    "action_result": action_result.model_dump(mode="json"),
                    "capture_delay_ms": stream_request.capture_delay_ms,
                    "change_timeout_ms": stream_request.change_timeout_ms,
                    "poll_interval_ms": stream_request.poll_interval_ms,
                    "poll_strategy": stream_request.poll_strategy,
                    "change_detection": stream_request.change_detection,
                    "change_detection_region": region.model_dump(mode="json")
                    if region is not None
                    else None,
                },
            )
        elif op == "stop":
            _clear_stream(state)
            await _send_empty_result(websocket, request_id)
        else:
            await _send_observation_error(
                websocket,
                request_id,
                "unsupported_op",
                f"unsupported op: {op}",
            )
    except ValidationError as exc:
        await _send_observation_error(
            websocket,
            request_id,
            "validation_error",
            "request validation failed",
            details={"errors": exc.errors(include_input=False)},
        )
    except DaemonError as exc:
        await _send_observation_error(
            websocket,
            request_id,
            exc.code,
            sanitize_text(exc.message),
            details=sanitize_payload(exc.details),
        )
    except Exception as exc:
        await _send_observation_error(
            websocket,
            request_id,
            "internal_error",
            "internal server error",
            details={"type": type(exc).__name__},
        )


async def _start_stream(
    websocket: WebSocket,
    state: _StreamState,
    request_id: str,
    payload: dict[str, Any],
) -> None:
    if state.request is not None:
        raise DaemonError("observation stream already started", code="stream_already_started")
    request = ObservationStreamRequest.model_validate(payload)
    _validate_stream_request(websocket, request)
    stream_id = f"obs_{id(state):x}"
    state.request = request
    state.stream_id = stream_id
    state.paused = False
    state.seq = 0
    state.last_sha256 = None
    state.last_source_sha256 = None
    state.last_image = None
    state.last_tile_hashes = None
    state.last_frame_seq = None
    state.emitted_frames = 0
    state.started_at = perf_counter()
    state.next_frame_at = state.started_at
    await websocket.send_json(
        {
            "type": "started",
            "id": request_id,
            "ok": True,
            "stream_id": stream_id,
            "protocol": PROTOCOL,
            "request": request.model_dump(mode="json"),
        }
    )
    await _send_next_frame(websocket, state, trigger="start")


async def _configure_stream(
    websocket: WebSocket,
    state: _StreamState,
    request_id: str,
    payload: dict[str, Any],
) -> None:
    _require_started(state)
    current = state.request.model_dump(mode="json")
    current.update(payload)
    request = ObservationStreamRequest.model_validate(current)
    _validate_stream_request(websocket, request)
    state.request = request
    await websocket.send_json(
        {
            "type": "result",
            "id": request_id,
            "ok": True,
            "result": {"request": request.model_dump(mode="json")},
        }
    )


async def _send_next_frame(
    websocket: WebSocket,
    state: _StreamState,
    *,
    trigger: str,
    request_id: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> None:
    request = state.request
    if request is None:
        return
    if await _stop_if_idle_timeout(websocket, state, request):
        return
    state.seq += 1
    try:
        metadata, payload = await _capture_frame(
            websocket,
            request,
            state.seq,
            state.last_sha256,
            last_source_sha256=state.last_source_sha256,
            previous_image=state.last_image,
            previous_tile_hashes=state.last_tile_hashes,
            previous_seq=state.last_frame_seq,
            stream_id=state.stream_id or "unknown",
        )
    except DaemonError as exc:
        await _send_observation_error(
            websocket,
            None,
            exc.code,
            sanitize_text(exc.message),
            details=sanitize_payload(exc.details),
        )
        _clear_stream(state)
        return
    except Exception as exc:
        await _send_observation_error(
            websocket,
            None,
            "internal_error",
            "internal server error",
            details={"type": type(exc).__name__},
        )
        _clear_stream(state)
        return
    await _emit_frame(
        websocket,
        state,
        request,
        metadata,
        payload,
        trigger=trigger,
        request_id=request_id,
        extra_metadata=extra_metadata,
    )


async def _send_changed_frame(
    websocket: WebSocket,
    state: _StreamState,
    *,
    trigger: str,
    request_id: str,
    timeout_ms: int,
    poll_interval_ms: int,
    poll_strategy: str,
    region: Region | None,
    region_baseline_sha256: str | None,
    extra_metadata: dict[str, Any],
) -> None:
    request = state.request
    if request is None:
        return
    if await _stop_if_idle_timeout(websocket, state, request):
        return
    state.seq += 1
    seq = state.seq
    started = perf_counter()
    deadline = started + timeout_ms / 1000
    attempts = 0
    region_attempts = 0
    region_detected = False
    used_region = region is not None and region_baseline_sha256 is not None
    last_metadata: dict[str, Any] | None = None
    last_payload = b""
    change_detected = False
    try:
        if used_region:
            while True:
                region_attempts += 1
                region_sha256 = await _capture_region_source_sha256(websocket, region=region)
                region_detected = region_sha256 != region_baseline_sha256
                if region_detected or perf_counter() >= deadline:
                    break
                await asyncio.sleep(
                    _change_poll_sleep_ms(
                        attempt=region_attempts,
                        poll_interval_ms=poll_interval_ms,
                        poll_strategy=poll_strategy,
                    )
                    / 1000
                )
        while True:
            attempts += 1
            metadata, payload = await _capture_frame(
                websocket,
                request,
                seq,
                state.last_sha256,
                last_source_sha256=state.last_source_sha256,
                previous_image=state.last_image,
                previous_tile_hashes=state.last_tile_hashes,
                previous_seq=state.last_frame_seq,
                stream_id=state.stream_id or "unknown",
            )
            last_metadata = metadata
            last_payload = payload
            change_detected = metadata["source_sha256"] != state.last_source_sha256
            if change_detected or region_detected or perf_counter() >= deadline:
                break
            await asyncio.sleep(
                _change_poll_sleep_ms(
                    attempt=attempts,
                    poll_interval_ms=poll_interval_ms,
                    poll_strategy=poll_strategy,
                )
                / 1000
            )
    except DaemonError as exc:
        await _send_observation_error(
            websocket,
            None,
            exc.code,
            sanitize_text(exc.message),
            details=sanitize_payload(exc.details),
        )
        _clear_stream(state)
        return
    except Exception as exc:
        await _send_observation_error(
            websocket,
            None,
            "internal_error",
            "internal server error",
            details={"type": type(exc).__name__},
        )
        _clear_stream(state)
        return
    wait_ms = _elapsed_ms(started)
    await _emit_frame(
        websocket,
        state,
        request,
        last_metadata,
        last_payload,
        trigger=trigger,
        request_id=request_id,
        extra_metadata={
            **extra_metadata,
            "change_detected": change_detected,
            "change_attempts": attempts,
            "change_region_attempts": region_attempts,
            "change_region_detected": region_detected,
            "change_wait_ms": wait_ms,
            "change_timeout_reached": not (change_detected or region_detected),
        },
    )


async def _capture_region_source_sha256(websocket: WebSocket, *, region: Region) -> str | None:
    validate_region(websocket, region, field="change_detection_region")
    options = ScreenshotOptions(format="png", show_cursor=False)
    raw = await _capture_raw_frame(
        websocket,
        ObservationStreamRequest(
            format="png",
            show_cursor=False,
            region=region,
        ),
        options,
    )
    return None if raw is None else raw.sha256


def _change_poll_sleep_ms(
    *,
    attempt: int,
    poll_interval_ms: int,
    poll_strategy: str,
) -> int:
    if poll_strategy != "adaptive":
        return poll_interval_ms
    return min(4 * (2 ** max(attempt - 1, 0)), poll_interval_ms)


def _resolve_change_detection_region(
    websocket: WebSocket,
    request: ObservationActionObserveChangeRequest,
) -> Region | None:
    if request.change_detection == "full":
        return None
    region = request.change_detection_region
    if region is None and request.change_detection == "auto_region":
        region = _auto_change_region(
            request.actions,
            width=websocket.app.state.backend.width,
            height=websocket.app.state.backend.height,
            radius=request.change_region_radius,
        )
    if region is None:
        return None
    validate_region(websocket, region, field="change_detection_region")
    return region


def _auto_change_region(
    actions: list[Any],
    *,
    width: int,
    height: int,
    radius: int,
) -> Region | None:
    for action in reversed(actions):
        point = _action_observation_point(action)
        if point is None:
            continue
        x, y = point
        left = max(x - radius, 0)
        top = max(y - radius, 0)
        right = min(x + radius, width)
        bottom = min(y + radius, height)
        if right <= left or bottom <= top:
            return None
        return Region(x=left, y=top, width=right - left, height=bottom - top)
    return None


def _action_observation_point(action: Any) -> tuple[int, int] | None:
    x = getattr(action, "x", None)
    y = getattr(action, "y", None)
    if isinstance(x, int) and isinstance(y, int):
        return x, y
    end_x = getattr(action, "end_x", None)
    end_y = getattr(action, "end_y", None)
    if isinstance(end_x, int) and isinstance(end_y, int):
        return end_x, end_y
    return None


async def _stop_if_idle_timeout(
    websocket: WebSocket,
    state: _StreamState,
    request: ObservationStreamRequest,
) -> bool:
    if request.idle_timeout_ms is None:
        return False
    elapsed_ms = (perf_counter() - state.started_at) * 1000
    if elapsed_ms < request.idle_timeout_ms:
        return False
    await websocket.send_json(
        {"type": "stopped", "stream_id": state.stream_id, "reason": "idle_timeout"}
    )
    _clear_stream(state)
    return True


async def _emit_frame(
    websocket: WebSocket,
    state: _StreamState,
    request: ObservationStreamRequest,
    metadata: dict[str, Any],
    payload: bytes,
    *,
    trigger: str,
    request_id: str | None,
    extra_metadata: dict[str, Any] | None,
) -> None:
    metadata["trigger"] = trigger
    if request_id is not None:
        metadata["id"] = request_id
    if extra_metadata:
        metadata.update(extra_metadata)
    state.last_sha256 = metadata["sha256"]
    state.last_source_sha256 = metadata["source_sha256"]
    current_image = metadata.pop("_current_image")
    current_tile_hashes = metadata.pop("_current_tile_hashes")
    if isinstance(current_image, Image.Image):
        state.last_image = current_image
        state.last_tile_hashes = None
        state.last_frame_seq = state.seq
    elif isinstance(current_tile_hashes, dict):
        state.last_image = None
        state.last_tile_hashes = current_tile_hashes
        state.last_frame_seq = state.seq
    should_suppress_payload = (
        metadata["unchanged"]
        and metadata["kind"] == "delta-suppressed"
        and not request.send_unchanged
    )
    if should_suppress_payload:
        await websocket.send_json(metadata)
    else:
        await websocket.send_json(metadata)
        await websocket.send_bytes(payload)
    state.emitted_frames += 1
    if request.max_frames is not None and state.emitted_frames >= request.max_frames:
        await websocket.send_json(
            {
                "type": "stopped",
                "stream_id": state.stream_id,
                "reason": "max_frames",
                "frames_sent": state.emitted_frames,
            }
        )
        _clear_stream(state)
        return
    state.next_frame_at = perf_counter() + 1 / request.fps


async def _capture_frame(
    websocket: WebSocket,
    request: ObservationStreamRequest,
    seq: int,
    last_sha256: str | None,
    *,
    last_source_sha256: str | None,
    previous_image: Image.Image | None,
    previous_tile_hashes: dict[tuple[int, int], bytes] | None,
    previous_seq: int | None,
    stream_id: str,
) -> tuple[dict[str, Any], bytes]:
    options = _stream_screenshot_options(request)

    async def operation():
        return await websocket.app.state.backend.screenshot_bytes(
            options,
            region=request.region,
            prefer_native_png=True,
        )

    captured_started = perf_counter()
    raw = await _capture_raw_frame(websocket, request, options)
    if raw is not None:
        metadata, payload = _capture_raw_delta_frame(
            raw=raw,
            request=request,
            options=options,
            seq=seq,
            last_source_sha256=last_source_sha256,
            previous_tile_hashes=previous_tile_hashes,
            previous_seq=previous_seq,
            stream_id=stream_id,
            captured_started=captured_started,
        )
        return metadata, payload

    shot = await run_screenshot_capture(websocket, operation)
    unchanged = shot.sha256 == last_sha256
    force_keyframe = _should_force_keyframe(
        previous_image=previous_image,
        request=request,
        seq=seq,
    )
    if unchanged and previous_image is not None:
        current_image = previous_image
        delta = _build_unchanged_delta(
            previous_seq=previous_seq,
            fallback_payload=shot.data,
            force_keyframe=force_keyframe,
        )
    else:
        current_image = _decode_image(shot.data) if request.delta_mode != "off" else None
        delta = _build_delta_payload(
            current_image=current_image,
            previous_image=previous_image,
            previous_seq=previous_seq,
            request=request,
            options=options,
            seq=seq,
            fallback_payload=shot.data,
            force_keyframe=force_keyframe,
        )
    observation_ms = (perf_counter() - captured_started) * 1000
    metadata = {
        "type": "unchanged"
        if delta["kind"] == "delta-suppressed" and not request.send_unchanged
        else "frame",
        "stream_id": stream_id,
        "seq": seq,
        "kind": delta["kind"],
        "content_type": f"image/{options.format}",
        "format": options.format,
        "width": shot.width,
        "height": shot.height,
        "size_bytes": len(delta["payload"]),
        "full_size_bytes": len(shot.data),
        "sha256": shot.sha256,
        "captured_at": shot.captured_at.isoformat(),
        "coordinate_space": shot.coordinate_space.model_dump(mode="json"),
        "cursor_visible": shot.cursor_visible,
        "capture_backend": shot.capture_backend or "unknown",
        "timing_ms": {
            **dict(shot.timings_ms),
            **delta["timing_ms"],
            "observation_total_ms": observation_ms,
        },
        "unchanged": unchanged,
        "dirty_rect": delta["dirty_rect"],
        "dirty_ratio": delta["dirty_ratio"],
        "previous_seq": delta["previous_seq"],
        "dropped_frames": 0,
        "source_sha256": shot.sha256,
        "_current_image": current_image,
        "_current_tile_hashes": None,
    }
    return metadata, delta["payload"]


async def _capture_raw_frame(
    websocket: WebSocket,
    request: ObservationStreamRequest,
    options: ScreenshotOptions,
) -> CapturedRawScreenshot | None:
    if not _can_use_raw_observation_path(request, options):
        return None

    async def operation():
        return await websocket.app.state.backend.screenshot_raw_pixels(region=request.region)

    raw = await run_screenshot_capture(websocket, operation)
    if raw is None:
        return None
    return raw


def _can_use_raw_observation_path(
    request: ObservationStreamRequest,
    options: ScreenshotOptions,
) -> bool:
    return (
        options.format == "png"
        and options.scale == 1.0
        and not options.show_cursor
        and request.delta_mode != "off"
    )


def _capture_raw_delta_frame(
    *,
    raw: CapturedRawScreenshot,
    request: ObservationStreamRequest,
    options: ScreenshotOptions,
    seq: int,
    last_source_sha256: str | None,
    previous_tile_hashes: dict[tuple[int, int], bytes] | None,
    previous_seq: int | None,
    stream_id: str,
    captured_started: float,
) -> tuple[dict[str, Any], bytes]:
    unchanged = raw.sha256 == last_source_sha256
    force_keyframe = (
        previous_tile_hashes is None
        or seq % request.keyframe_interval == 0
        or request.delta_mode == "off"
    )
    delta_started = perf_counter()
    current_tile_hashes = previous_tile_hashes if unchanged else None
    if unchanged and previous_tile_hashes is not None and not force_keyframe:
        payload = b""
        kind = "delta-suppressed"
        dirty_rect = None
        dirty_ratio = 0.0
        previous = previous_seq
        timing = {"diff_ms": 0.0, "tile_diff_ms": 0.0}
        full_size_bytes = None
        payload_sha256 = last_source_sha256 or raw.sha256
    else:
        current_tile_hashes = tile_hashes_rgb(raw.rgb, raw.width, raw.height, request.tile_size)
        tile_diff_ms = _elapsed_ms(delta_started)
        dirty_rect = dirty_rect_from_tiles(
            current=current_tile_hashes,
            previous=previous_tile_hashes,
            width=raw.width,
            height=raw.height,
            tile_size=request.tile_size,
        )
        if force_keyframe:
            dirty_ratio = 1.0 if previous_tile_hashes is None else 0.0
        elif dirty_rect is None:
            dirty_ratio = 0.0
        else:
            dirty_ratio = (dirty_rect["width"] * dirty_rect["height"]) / (raw.width * raw.height)

        if dirty_rect is None and not force_keyframe:
            payload = b""
            kind = "delta-suppressed"
            previous = previous_seq
            full_size_bytes = None
        elif force_keyframe or dirty_ratio > request.delta_max_ratio:
            encode_started = perf_counter()
            payload = encode_rgb_png(raw.rgb, (raw.width, raw.height))
            kind = "keyframe"
            previous = None
            full_size_bytes = len(payload)
            timing = {
                "diff_ms": tile_diff_ms,
                "tile_diff_ms": tile_diff_ms,
                "encode_ms": _elapsed_ms(encode_started),
            }
            payload_sha256 = raw.sha256
            return _raw_metadata(
                raw=raw,
                request=request,
                options=options,
                stream_id=stream_id,
                seq=seq,
                kind=kind,
                payload=payload,
                payload_sha256=payload_sha256,
                full_size_bytes=full_size_bytes,
                unchanged=unchanged,
                dirty_rect=dirty_rect,
                dirty_ratio=dirty_ratio,
                previous_seq=previous,
                timing=timing,
                captured_started=captured_started,
                current_tile_hashes=current_tile_hashes,
            )
        else:
            encode_started = perf_counter()
            if dirty_rect is None:
                raise RuntimeError("patch frame requires dirty rectangle")
            left = dirty_rect["x"]
            top = dirty_rect["y"]
            width = dirty_rect["width"]
            height = dirty_rect["height"]
            patch_rgb = crop_rgb(raw.rgb, raw.width, left, top, width, height)
            payload = encode_rgb_png(patch_rgb, (width, height))
            kind = "patch"
            previous = previous_seq
            full_size_bytes = None
            timing = {
                "diff_ms": tile_diff_ms,
                "tile_diff_ms": tile_diff_ms,
                "patch_encode_ms": _elapsed_ms(encode_started),
            }
            payload_sha256 = raw.sha256
            return _raw_metadata(
                raw=raw,
                request=request,
                options=options,
                stream_id=stream_id,
                seq=seq,
                kind=kind,
                payload=payload,
                payload_sha256=payload_sha256,
                full_size_bytes=full_size_bytes,
                unchanged=unchanged,
                dirty_rect=dirty_rect,
                dirty_ratio=dirty_ratio,
                previous_seq=previous,
                timing=timing,
                captured_started=captured_started,
                current_tile_hashes=current_tile_hashes,
            )
        timing = {"diff_ms": tile_diff_ms, "tile_diff_ms": tile_diff_ms}
        payload_sha256 = raw.sha256

    return _raw_metadata(
        raw=raw,
        request=request,
        options=options,
        stream_id=stream_id,
        seq=seq,
        kind=kind,
        payload=payload,
        payload_sha256=payload_sha256,
        full_size_bytes=full_size_bytes,
        unchanged=unchanged,
        dirty_rect=dirty_rect,
        dirty_ratio=dirty_ratio,
        previous_seq=previous,
        timing=timing,
        captured_started=captured_started,
        current_tile_hashes=current_tile_hashes,
    )


def _raw_metadata(
    *,
    raw: CapturedRawScreenshot,
    request: ObservationStreamRequest,
    options: ScreenshotOptions,
    stream_id: str,
    seq: int,
    kind: str,
    payload: bytes,
    payload_sha256: str,
    full_size_bytes: int | None,
    unchanged: bool,
    dirty_rect: dict[str, int] | None,
    dirty_ratio: float,
    previous_seq: int | None,
    timing: dict[str, float],
    captured_started: float,
    current_tile_hashes: dict[tuple[int, int], bytes] | None,
) -> tuple[dict[str, Any], bytes]:
    metadata = {
        "type": "unchanged"
        if kind == "delta-suppressed" and not request.send_unchanged
        else "frame",
        "stream_id": stream_id,
        "seq": seq,
        "kind": kind,
        "content_type": f"image/{options.format}",
        "format": options.format,
        "width": raw.width,
        "height": raw.height,
        "size_bytes": len(payload),
        "full_size_bytes": full_size_bytes,
        "sha256": payload_sha256,
        "source_sha256": raw.sha256,
        "captured_at": raw.captured_at.isoformat(),
        "coordinate_space": raw.coordinate_space.model_dump(mode="json"),
        "cursor_visible": raw.cursor_visible,
        "capture_backend": raw.capture_backend or "unknown",
        "timing_ms": {
            **dict(raw.timings_ms),
            **timing,
            "observation_total_ms": _elapsed_ms(captured_started),
        },
        "unchanged": unchanged,
        "dirty_rect": dirty_rect,
        "dirty_ratio": dirty_ratio,
        "previous_seq": previous_seq,
        "dropped_frames": 0,
        "tile_size": request.tile_size,
        "tile_hash_backend": "xxh3" if native_hash_available() else "blake2b",
        "_current_image": None,
        "_current_tile_hashes": current_tile_hashes,
    }
    return metadata, payload


def _build_delta_payload(
    *,
    current_image: Image.Image | None,
    previous_image: Image.Image | None,
    previous_seq: int | None,
    request: ObservationStreamRequest,
    options: ScreenshotOptions,
    seq: int,
    fallback_payload: bytes,
    force_keyframe: bool,
) -> dict[str, Any]:
    started = perf_counter()
    if force_keyframe:
        return {
            "kind": "keyframe",
            "payload": fallback_payload,
            "dirty_rect": None,
            "dirty_ratio": 1.0,
            "previous_seq": None,
            "timing_ms": {"diff_ms": _elapsed_ms(started)},
        }
    if current_image is None:
        raise RuntimeError("delta frame requires decoded current image")
    diff = ImageChops.difference(previous_image, current_image)
    bbox = diff.getbbox()
    diff_ms = _elapsed_ms(started)
    if bbox is None:
        return {
            "kind": "delta-suppressed",
            "payload": b"",
            "dirty_rect": None,
            "dirty_ratio": 0.0,
            "previous_seq": previous_seq,
            "timing_ms": {"diff_ms": diff_ms},
        }
    left, top, right, bottom = bbox
    width = right - left
    height = bottom - top
    dirty_ratio = (width * height) / (current_image.width * current_image.height)
    if dirty_ratio > request.delta_max_ratio:
        return {
            "kind": "keyframe",
            "payload": fallback_payload,
            "dirty_rect": {"x": left, "y": top, "width": width, "height": height},
            "dirty_ratio": dirty_ratio,
            "previous_seq": None,
            "timing_ms": {"diff_ms": diff_ms},
        }
    encode_started = perf_counter()
    patch = current_image.crop(bbox)
    payload = encode_image(patch, options.format, options.quality)
    return {
        "kind": "patch",
        "payload": payload,
        "dirty_rect": {"x": left, "y": top, "width": width, "height": height},
        "dirty_ratio": dirty_ratio,
        "previous_seq": previous_seq,
        "timing_ms": {"diff_ms": diff_ms, "patch_encode_ms": _elapsed_ms(encode_started)},
    }


def _build_unchanged_delta(
    *,
    previous_seq: int | None,
    fallback_payload: bytes,
    force_keyframe: bool,
) -> dict[str, Any]:
    if force_keyframe:
        return {
            "kind": "keyframe",
            "payload": fallback_payload,
            "dirty_rect": None,
            "dirty_ratio": 0.0,
            "previous_seq": None,
            "timing_ms": {"diff_ms": 0.0},
        }
    return {
        "kind": "delta-suppressed",
        "payload": b"",
        "dirty_rect": None,
        "dirty_ratio": 0.0,
        "previous_seq": previous_seq,
        "timing_ms": {"diff_ms": 0.0},
    }


def _should_force_keyframe(
    *,
    previous_image: Image.Image | None,
    request: ObservationStreamRequest,
    seq: int,
) -> bool:
    return (
        previous_image is None
        or seq % request.keyframe_interval == 0
        or request.delta_mode == "off"
    )


def _decode_image(data: bytes) -> Image.Image:
    image = Image.open(BytesIO(data))
    image.load()
    return image.convert("RGB")


def _elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1000


def _validate_stream_request(websocket: WebSocket, request: ObservationStreamRequest) -> None:
    _stream_screenshot_options(request)
    if request.region is not None:
        validate_region(websocket, request.region)
        source_width = request.region.width
        source_height = request.region.height
    else:
        source_width = websocket.app.state.backend.width
        source_height = websocket.app.state.backend.height
    enforce_screenshot_options_pixels(
        websocket,
        source_width=source_width,
        source_height=source_height,
        scale=request.scale,
    )


def _stream_screenshot_options(request: ObservationStreamRequest) -> ScreenshotOptions:
    if request.storage != "inline":
        raise DaemonError(
            "raw observation stream requires inline storage",
            status_code=422,
            code="invalid_screenshot_storage",
            details={"storage": request.storage},
        )
    return ScreenshotOptions.model_validate(
        request.model_dump(
            exclude={
                "region",
                "fps",
                "max_frames",
                "idle_timeout_ms",
                "send_unchanged",
                "keyframe_interval",
                "delta_mode",
                "delta_max_ratio",
                "tile_size",
            }
        )
    )


def _require_started(state: _StreamState) -> None:
    if state.request is None:
        raise DaemonError("observation stream is not started", code="stream_not_started")


def _clear_stream(state: _StreamState) -> None:
    state.request = None
    state.stream_id = None
    state.paused = False
    state.last_sha256 = None
    state.last_image = None
    state.last_frame_seq = None


async def _send_observation_error(
    websocket: WebSocket,
    request_id: str | None,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> None:
    await websocket.send_json(
        {
            "type": "error",
            "id": request_id,
            "ok": False,
            "error": {
                "code": code,
                "message": sanitize_text(message),
                "details": sanitize_payload(details or {}),
            },
        }
    )


async def _send_empty_result(websocket: WebSocket, request_id: str) -> None:
    await websocket.send_json({"type": "result", "id": request_id, "ok": True, "result": {}})
