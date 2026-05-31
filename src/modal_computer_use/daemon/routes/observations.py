from __future__ import annotations

import asyncio
import contextlib
import json
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from io import BytesIO
from time import perf_counter
from typing import Any

from fastapi import APIRouter, Response, WebSocket, WebSocketDisconnect
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
    dirty_rects_from_tiles,
    native_hash_available,
    tile_hashes_rgb,
)
from modal_computer_use.daemon.desktop.xdamage import XDamageWaitResult, XDamageWatcher
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
    ObservationTransportProbeRequest,
)
from modal_computer_use.models import ActionBatchRequest, Region, ScreenshotOptions
from modal_computer_use.redaction import sanitize_payload, sanitize_text

router = APIRouter(prefix="/v1/observations")

PROTOCOL = "computer-use.observation-stream.v1"
FRAME_ENVELOPE_MAGIC = b"MCUO\x01"
CONTROL_DRAIN_TIMEOUT_S = 0.001


@dataclass
class _StreamState:
    request: ObservationStreamRequest | None = None
    stream_id: str | None = None
    paused: bool = False
    seq: int = 0
    emit_version: int = 0
    source_version: int = 0
    last_sha256: str | None = None
    last_source_sha256: str | None = None
    last_image: Image.Image | None = None
    last_tile_hashes: dict[tuple[int, int], bytes] | None = None
    last_frame_seq: int | None = None
    emitted_frames: int = 0
    started_at: float = 0.0
    next_frame_at: float = 0.0
    xdamage_watcher: XDamageWatcher | None = None
    xdamage_display: str | None = None
    dirty_frame_producer: _DirtyFrameProducer | None = None
    dirty_frame_display: str | None = None


@dataclass
class _PreparedChangeSignal:
    requested: str
    active: str
    watcher: XDamageWatcher | None = None
    unavailable_reason: str | None = None
    prearmed: bool = False


@dataclass(frozen=True)
class _DirtyFrameProducerResult:
    raw: CapturedRawScreenshot
    produced_at: float
    generation: int
    wait_result: XDamageWaitResult
    wait_wall_ms: float
    capture_ms: float


class _DirtyFrameProducer:
    def __init__(
        self,
        *,
        capture_raw: Callable[[Region | None], Awaitable[CapturedRawScreenshot | None]],
        display: str,
    ) -> None:
        self._capture_raw = capture_raw
        self._display = display
        self._watcher = XDamageWatcher(display=display)
        self._generation = 0
        self._latest: _DirtyFrameProducerResult | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.failure: str | None = None

    async def arm(self, request: ObservationStreamRequest, *, timeout_ms: int) -> None:
        if self._closed:
            raise RuntimeError("dirty-frame producer is closed")
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._generation += 1
        generation = self._generation
        self._latest = None
        try:
            await asyncio.to_thread(self._watcher.arm)
        except Exception as exc:
            self.failure = getattr(self._watcher, "failure", None) or str(exc)
            raise
        self.failure = None
        self._task = asyncio.create_task(self._produce_once(request, generation, timeout_ms))

    async def wait_for_change(
        self,
        *,
        baseline_source_sha256: str | None,
        timeout_ms: int,
    ) -> _DirtyFrameProducerResult | None:
        deadline = perf_counter() + timeout_ms / 1000
        while True:
            latest = self._latest
            if (
                latest is not None
                and latest.generation == self._generation
                and latest.raw.sha256 != baseline_source_sha256
            ):
                return latest
            task = self._task
            if task is None:
                return None
            remaining = deadline - perf_counter()
            if remaining <= 0:
                return None
            done, _ = await asyncio.wait({task}, timeout=remaining)
            if not done:
                return None
            if task.cancelled():
                return None
            exc = task.exception()
            if exc is not None:
                self.failure = str(exc)
                return None

    async def close(self) -> None:
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await asyncio.to_thread(self._watcher.close)

    def close_sync(self) -> None:
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._watcher.close()

    async def _produce_once(
        self,
        request: ObservationStreamRequest,
        generation: int,
        timeout_ms: int,
    ) -> None:
        wait_started = perf_counter()
        wait_result = await asyncio.to_thread(self._watcher.wait, timeout_ms)
        wait_wall_ms = _elapsed_ms(wait_started)
        if not wait_result.detected:
            self._latest = None
            return
        capture_started = perf_counter()
        raw = await self._capture_raw(request.region)
        capture_ms = _elapsed_ms(capture_started)
        if raw is None:
            return
        self._latest = _DirtyFrameProducerResult(
            raw=raw,
            produced_at=perf_counter(),
            generation=generation,
            wait_result=wait_result,
            wait_wall_ms=wait_wall_ms,
            capture_ms=capture_ms,
        )


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
                    message = await _receive_observation_message(
                        websocket,
                        timeout=CONTROL_DRAIN_TIMEOUT_S,
                    )
                    if message is not None:
                        await _handle_observation_message(websocket, state, message)
                        continue
                    await _send_next_frame(websocket, state, trigger="scheduled")
                    continue
                timeout = None if state.request is None else max(state.next_frame_at - now, 0.0)
            else:
                timeout = None
            message = await _receive_observation_message(websocket, timeout=timeout)
            if message is None:
                continue
            await _handle_observation_message(websocket, state, message)
    except WebSocketDisconnect:
        return
    finally:
        _close_stream_resources(state)


@router.post("/transport-probe")
async def observation_transport_probe(payload: ObservationTransportProbeRequest) -> Response:
    probe_payload = _transport_probe_payload(payload.size_bytes)
    emit_started = perf_counter()
    headers = {
        "x-computer-use-size-bytes": str(payload.size_bytes),
        "x-computer-use-transport-timing-ms": json.dumps(
            {"emit_total_ms": _elapsed_ms(emit_started)},
            separators=(",", ":"),
        ),
    }
    return Response(
        content=probe_payload,
        media_type="application/octet-stream",
        headers=headers,
    )


async def _receive_observation_message(
    websocket: WebSocket,
    *,
    timeout: float | None,
) -> Any | None:
    try:
        return await asyncio.wait_for(websocket.receive_json(), timeout=timeout)
    except TimeoutError:
        return None


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
        elif op == "transport_probe":
            await _send_transport_probe(websocket, request_id, payload)
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
            observe_started = perf_counter()
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
                        "change_signal",
                        "dirty_frame_producer",
                        "change_detection_region",
                        "change_region_radius",
                    },
                )
            )
            region = _resolve_change_detection_region(websocket, stream_request)
            baseline_source_version = state.source_version
            baseline_source_sha256 = state.last_source_sha256
            region_baseline_sha256 = None
            region_baseline_ms = 0.0
            if region is not None:
                region_baseline_started = perf_counter()
                region_baseline_sha256 = await _capture_region_source_sha256(
                    websocket,
                    region=region,
                )
                region_baseline_ms = _elapsed_ms(region_baseline_started)
            signal_prepare_started = perf_counter()
            dirty_producer = await _prepare_dirty_frame_producer(
                websocket,
                state,
                stream_request,
            )
            change_signal = (
                _PreparedChangeSignal(
                    requested=stream_request.change_signal,
                    active="xdamage",
                    prearmed=True,
                )
                if dirty_producer is not None
                else _prepare_change_signal(websocket, state, stream_request.change_signal)
            )
            signal_prepare_ms = _elapsed_ms(signal_prepare_started)
            action_started = perf_counter()
            action_result = await run_batch(action_request, ActionBatchContext(websocket.app.state))
            action_wall_ms = _elapsed_ms(action_started)
            capture_delay_wall_ms = 0.0
            if stream_request.capture_delay_ms > 0:
                capture_delay_started = perf_counter()
                await asyncio.sleep(stream_request.capture_delay_ms / 1000)
                capture_delay_wall_ms = _elapsed_ms(capture_delay_started)
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
                change_signal=change_signal,
                dirty_producer=dirty_producer,
                baseline_source_sha256=baseline_source_sha256,
                observe_started=observe_started,
                stage_timing_ms={
                    "signal_prepare_ms": signal_prepare_ms,
                    "region_baseline_ms": region_baseline_ms,
                    "action_wall_ms": action_wall_ms,
                    "capture_delay_wall_ms": capture_delay_wall_ms,
                },
                extra_metadata={
                    "action_result": action_result.model_dump(mode="json"),
                    "action_id": request_id,
                    "causal_frame": True,
                    "capture_delay_ms": stream_request.capture_delay_ms,
                    "change_timeout_ms": stream_request.change_timeout_ms,
                    "poll_interval_ms": stream_request.poll_interval_ms,
                    "poll_strategy": stream_request.poll_strategy,
                    "change_detection": stream_request.change_detection,
                    "change_signal": stream_request.change_signal,
                    "dirty_frame_producer_policy": stream_request.dirty_frame_producer,
                    "baseline_source_version": baseline_source_version,
                    "baseline_source_sha256": baseline_source_sha256,
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
    state.emit_version = 0
    state.source_version = 0
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
    coalesced_scheduled_frames = (
        _coalesced_scheduled_frames(state, request) if trigger == "scheduled" else 0
    )
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
        extra_metadata=_with_backpressure_metadata(
            extra_metadata,
            coalesced_scheduled_frames=coalesced_scheduled_frames,
        ),
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
    change_signal: _PreparedChangeSignal,
    dirty_producer: _DirtyFrameProducer | None,
    baseline_source_sha256: str | None,
    observe_started: float,
    stage_timing_ms: dict[str, float],
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
    signal_result: XDamageWaitResult | None = None
    signal_wait_wall_ms = 0.0
    dirty_producer_wait_ms = 0.0
    dirty_producer_capture_ms = 0.0
    dirty_frame_age_ms: float | None = None
    dirty_producer_used = False
    dirty_producer_fallback_reason: str | None = None
    region_poll_ms = 0.0
    frame_poll_ms = 0.0
    last_metadata: dict[str, Any] | None = None
    last_payload = b""
    change_detected = False
    try:
        if dirty_producer is not None:
            producer_wait_started = perf_counter()
            producer_result = await dirty_producer.wait_for_change(
                baseline_source_sha256=baseline_source_sha256,
                timeout_ms=timeout_ms,
            )
            dirty_producer_wait_ms = _elapsed_ms(producer_wait_started)
            if producer_result is not None:
                signal_result = producer_result.wait_result
                signal_wait_wall_ms = producer_result.wait_wall_ms
                dirty_producer_capture_ms = producer_result.capture_ms
                dirty_frame_age_ms = max((perf_counter() - producer_result.produced_at) * 1000, 0.0)
                metadata, payload = _capture_raw_delta_frame(
                    raw=producer_result.raw,
                    request=request,
                    options=_stream_screenshot_options(request),
                    seq=seq,
                    last_source_sha256=state.last_source_sha256,
                    previous_tile_hashes=state.last_tile_hashes,
                    previous_seq=state.last_frame_seq,
                    stream_id=state.stream_id or "unknown",
                    captured_started=observe_started,
                )
                last_metadata = metadata
                last_payload = payload
                change_detected = metadata["source_sha256"] != state.last_source_sha256
                dirty_producer_used = change_detected
                attempts = 1
            if not dirty_producer_used:
                dirty_producer_fallback_reason = dirty_producer.failure or "no_changed_frame"
        if not dirty_producer_used and change_signal.watcher is not None:
            signal_wait_started = perf_counter()
            signal_result = await asyncio.to_thread(
                change_signal.watcher.wait,
                timeout_ms,
            )
            signal_wait_wall_ms = _elapsed_ms(signal_wait_started)
        if (
            not dirty_producer_used
            and used_region
            and not (signal_result is not None and signal_result.detected)
        ):
            region_poll_started = perf_counter()
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
            region_poll_ms = _elapsed_ms(region_poll_started)
        if not dirty_producer_used:
            frame_poll_started = perf_counter()
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
            frame_poll_ms = _elapsed_ms(frame_poll_started)
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
    stage_timing = {
        **stage_timing_ms,
        "signal_wait_wall_ms": signal_wait_wall_ms,
        "dirty_producer_wait_ms": dirty_producer_wait_ms,
        "dirty_producer_capture_ms": dirty_producer_capture_ms,
        "region_poll_ms": region_poll_ms,
        "frame_poll_ms": frame_poll_ms,
        "change_wait_ms": wait_ms,
        "server_pre_emit_ms": _elapsed_ms(observe_started),
    }
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
            "change_signal_requested": change_signal.requested,
            "change_signal_active": change_signal.active,
            "change_signal_available": _change_signal_available(signal_result, change_signal),
            "change_signal_detected": None if signal_result is None else signal_result.detected,
            "change_signal_wait_ms": None if signal_result is None else signal_result.wait_ms,
            "change_signal_reason": _change_signal_reason(signal_result, change_signal),
            "change_signal_version": None if signal_result is None else signal_result.version,
            "dirty_frame_producer": dirty_producer is not None,
            "dirty_frame_producer_used": dirty_producer_used,
            "dirty_frame_producer_fallback_reason": dirty_producer_fallback_reason,
            "dirty_frame_age_ms": dirty_frame_age_ms,
            "change_stage_timing_ms": stage_timing,
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


async def _prepare_dirty_frame_producer(
    websocket: WebSocket,
    state: _StreamState,
    request: ObservationActionObserveChangeRequest,
) -> _DirtyFrameProducer | None:
    if request.dirty_frame_producer == "off":
        return None
    if request.change_signal == "poll":
        return None
    if state.request is None:
        return None
    stream_request = state.request
    options = _stream_screenshot_options(stream_request)
    if not _can_use_raw_observation_path(stream_request, options):
        return None
    display = getattr(websocket.app.state.backend, "display", None)
    if not isinstance(display, str) or not display:
        return None

    async def capture_raw(region: Region | None) -> CapturedRawScreenshot | None:
        async def operation():
            return await websocket.app.state.backend.screenshot_raw_pixels(region=region)

        return await run_screenshot_capture(websocket, operation)

    if state.dirty_frame_producer is None or state.dirty_frame_display != display:
        if state.dirty_frame_producer is not None:
            state.dirty_frame_producer.close_sync()
        state.dirty_frame_producer = _DirtyFrameProducer(capture_raw=capture_raw, display=display)
        state.dirty_frame_display = display
    producer = state.dirty_frame_producer
    try:
        await producer.arm(stream_request, timeout_ms=request.change_timeout_ms)
    except Exception:
        if request.change_signal == "xdamage":
            return None
        return None
    return producer


def _prepare_change_signal(
    websocket: WebSocket,
    state: _StreamState,
    requested: str,
) -> _PreparedChangeSignal:
    if requested == "poll":
        return _PreparedChangeSignal(requested=requested, active="poll")
    display = getattr(websocket.app.state.backend, "display", None)
    if not isinstance(display, str) or not display:
        return _PreparedChangeSignal(
            requested=requested,
            active="poll",
            unavailable_reason="backend has no X11 display",
        )
    if state.xdamage_watcher is None or state.xdamage_display != display:
        if state.xdamage_watcher is not None:
            state.xdamage_watcher.close()
        state.xdamage_watcher = XDamageWatcher(display=display)
        state.xdamage_display = display
    watcher = state.xdamage_watcher
    try:
        watcher.arm()
    except Exception:
        if requested == "auto":
            return _PreparedChangeSignal(
                requested=requested,
                active="poll",
                unavailable_reason=watcher.failure or "XDamage unavailable",
            )
        return _PreparedChangeSignal(
            requested=requested,
            active="xdamage",
            watcher=watcher,
            unavailable_reason=watcher.failure or "XDamage unavailable",
        )
    return _PreparedChangeSignal(
        requested=requested,
        active="xdamage",
        watcher=watcher,
        prearmed=True,
    )


def _change_signal_available(
    result: XDamageWaitResult | None,
    signal: _PreparedChangeSignal,
) -> bool | None:
    if signal.active == "poll":
        return False if signal.requested != "poll" else None
    if result is None:
        return None
    return result.available


def _change_signal_reason(
    result: XDamageWaitResult | None,
    signal: _PreparedChangeSignal,
) -> str | None:
    if result is not None:
        return result.reason
    return signal.unavailable_reason


def _change_poll_sleep_ms(
    *,
    attempt: int,
    poll_interval_ms: int,
    poll_strategy: str,
) -> int:
    if poll_strategy != "adaptive":
        return poll_interval_ms
    return min(4 * (2 ** max(attempt - 1, 0)), poll_interval_ms)


def _coalesced_scheduled_frames(
    state: _StreamState,
    request: ObservationStreamRequest,
    *,
    now: float | None = None,
) -> int:
    if request.delivery != "latest" or state.next_frame_at <= 0:
        return 0
    interval = 1 / request.fps
    overdue_s = (perf_counter() if now is None else now) - state.next_frame_at
    if overdue_s <= interval:
        return 0
    return int(overdue_s // interval)


def _with_backpressure_metadata(
    metadata: dict[str, Any] | None,
    *,
    coalesced_scheduled_frames: int,
) -> dict[str, Any] | None:
    if coalesced_scheduled_frames <= 0:
        return metadata
    return {
        **(metadata or {}),
        "coalesced_scheduled_frames": coalesced_scheduled_frames,
    }


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
    previous_source_sha256 = state.last_source_sha256
    previous_source_version = state.source_version
    if metadata["source_sha256"] != previous_source_sha256:
        state.source_version += 1
    state.emit_version += 1
    metadata["source_version"] = state.source_version
    metadata["previous_source_version"] = previous_source_version
    metadata["emit_version"] = state.emit_version
    metadata["delivery"] = request.delivery
    metadata["frame_encoding"] = request.frame_encoding
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
    emit_started = perf_counter()
    metadata_send_started = perf_counter()
    if request.frame_encoding == "binary-envelope":
        envelope_payload = b"" if should_suppress_payload else payload
        await websocket.send_bytes(_encode_frame_envelope(metadata, envelope_payload))
        metadata_send_ms = _elapsed_ms(metadata_send_started)
        payload_send_ms = 0.0
    else:
        if should_suppress_payload:
            await websocket.send_json(metadata)
            metadata_send_ms = _elapsed_ms(metadata_send_started)
            payload_send_ms = 0.0
        else:
            await websocket.send_json(metadata)
            metadata_send_ms = _elapsed_ms(metadata_send_started)
            payload_send_started = perf_counter()
            await websocket.send_bytes(payload)
            payload_send_ms = _elapsed_ms(payload_send_started)
    if request.transport_timing:
        await websocket.send_json(
            {
                "type": "transport_timing",
                "stream_id": state.stream_id,
                "seq": metadata["seq"],
                "trigger": trigger,
                "server_emit_timing_ms": {
                    "metadata_send_ms": metadata_send_ms,
                    "payload_send_ms": payload_send_ms,
                    "emit_total_ms": _elapsed_ms(emit_started),
                },
            }
        )
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


def _encode_frame_envelope(metadata: dict[str, Any], payload: bytes) -> bytes:
    metadata_payload = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
    return (
        FRAME_ENVELOPE_MAGIC
        + struct.pack(">I", len(metadata_payload))
        + metadata_payload
        + payload
    )


async def _send_transport_probe(
    websocket: WebSocket,
    request_id: str,
    payload: dict[str, Any],
) -> None:
    try:
        request = ObservationTransportProbeRequest.model_validate(payload)
    except ValidationError as exc:
        await _send_observation_error(
            websocket,
            request_id,
            "validation_error",
            "request validation failed",
            details={"errors": exc.errors(include_input=False)},
        )
        return
    probe_payload = _transport_probe_payload(request.size_bytes)
    emit_started = perf_counter()
    metadata_send_started = perf_counter()
    metadata = {
        "type": "transport_probe",
        "id": request_id,
        "ok": True,
        "size_bytes": request.size_bytes,
        "frame_encoding": request.frame_encoding,
    }
    if request.frame_encoding == "binary-envelope":
        await websocket.send_bytes(_encode_frame_envelope(metadata, probe_payload))
    else:
        await websocket.send_json(metadata)
        metadata_send_ms = _elapsed_ms(metadata_send_started)
        payload_send_started = perf_counter()
        await websocket.send_bytes(probe_payload)
        payload_send_ms = _elapsed_ms(payload_send_started)
    if request.frame_encoding == "binary-envelope":
        metadata_send_ms = _elapsed_ms(metadata_send_started)
        payload_send_ms = 0.0
    await websocket.send_json(
        {
            "type": "transport_timing",
            "id": request_id,
            "seq": None,
            "server_emit_timing_ms": {
                "metadata_send_ms": metadata_send_ms,
                "payload_send_ms": payload_send_ms,
                "emit_total_ms": _elapsed_ms(emit_started),
            },
        }
    )


def _transport_probe_payload(size_bytes: int) -> bytes:
    return b"\0" * size_bytes


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

        patch_rects: list[dict[str, int]] = []
        patch_dirty_ratio = dirty_ratio
        if dirty_rect is not None and not force_keyframe:
            patch_rects = _select_patch_rects(
                current=current_tile_hashes,
                previous=previous_tile_hashes,
                dirty_rect=dirty_rect,
                width=raw.width,
                height=raw.height,
                tile_size=request.tile_size,
                max_patch_rects=request.max_patch_rects,
                min_savings=request.multi_rect_min_savings,
            )
            patch_dirty_ratio = sum(
                rect["width"] * rect["height"] for rect in patch_rects
            ) / (raw.width * raw.height)

        if dirty_rect is None and not force_keyframe:
            payload = b""
            kind = "delta-suppressed"
            previous = previous_seq
            full_size_bytes = None
        elif force_keyframe or patch_dirty_ratio > request.delta_max_ratio:
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
            if len(patch_rects) > 1:
                payload, patch_sizes = _encode_patch_bundle(
                    raw=raw,
                    rects=patch_rects,
                )
                kind = "patches"
                previous = previous_seq
                full_size_bytes = None
                timing = {
                    "diff_ms": tile_diff_ms,
                    "tile_diff_ms": tile_diff_ms,
                    "patch_encode_ms": _elapsed_ms(encode_started),
                    "patch_count": float(len(patch_rects)),
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
                    dirty_ratio=patch_dirty_ratio,
                    previous_seq=previous,
                    timing=timing,
                    captured_started=captured_started,
                    current_tile_hashes=current_tile_hashes,
                    patch_rects=patch_rects,
                    patch_sizes=patch_sizes,
                )
            single_rect = patch_rects[0] if patch_rects else dirty_rect
            left = single_rect["x"]
            top = single_rect["y"]
            width = single_rect["width"]
            height = single_rect["height"]
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
                patch_rects=[single_rect],
                patch_sizes=[len(payload)],
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
    patch_rects: list[dict[str, int]] | None = None,
    patch_sizes: list[int] | None = None,
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
        "patch_rects": patch_rects,
        "patch_count": 0 if patch_rects is None else len(patch_rects),
        "patch_sizes_bytes": patch_sizes,
        "dirty_ratio": dirty_ratio,
        "previous_seq": previous_seq,
        "dropped_frames": 0,
        "tile_size": request.tile_size,
        "tile_hash_backend": "xxh3" if native_hash_available() else "blake2b",
        "_current_image": None,
        "_current_tile_hashes": current_tile_hashes,
    }
    return metadata, payload


def _select_patch_rects(
    *,
    current: dict[tuple[int, int], bytes],
    previous: dict[tuple[int, int], bytes] | None,
    dirty_rect: dict[str, int],
    width: int,
    height: int,
    tile_size: int,
    max_patch_rects: int,
    min_savings: float,
) -> list[dict[str, int]]:
    if previous is None or max_patch_rects <= 1:
        return [dirty_rect]
    rects = dirty_rects_from_tiles(
        current=current,
        previous=previous,
        width=width,
        height=height,
        tile_size=tile_size,
        max_rects=max_patch_rects,
    )
    if len(rects) <= 1:
        return [dirty_rect]
    single_area = dirty_rect["width"] * dirty_rect["height"]
    rect_area = sum(rect["width"] * rect["height"] for rect in rects)
    if single_area <= 0 or rect_area >= single_area:
        return [dirty_rect]
    savings = 1 - rect_area / single_area
    if savings < min_savings:
        return [dirty_rect]
    return rects


def _encode_patch_bundle(
    *,
    raw: CapturedRawScreenshot,
    rects: list[dict[str, int]],
) -> tuple[bytes, list[int]]:
    chunks: list[bytes] = []
    patch_sizes: list[int] = []
    manifest: list[dict[str, int]] = []
    for rect in rects:
        patch_rgb = crop_rgb(
            raw.rgb,
            raw.width,
            rect["x"],
            rect["y"],
            rect["width"],
            rect["height"],
        )
        payload = encode_rgb_png(patch_rgb, (rect["width"], rect["height"]))
        chunks.append(payload)
        patch_sizes.append(len(payload))
        manifest.append({**rect, "size_bytes": len(payload)})
    manifest_bytes = json.dumps({"patches": manifest}, separators=(",", ":")).encode()
    return struct.pack(">I", len(manifest_bytes)) + manifest_bytes + b"".join(chunks), patch_sizes


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
                "transport_timing",
                "frame_encoding",
                "keyframe_interval",
                "delta_mode",
                "delta_max_ratio",
                "tile_size",
                "delivery",
                "max_patch_rects",
                "multi_rect_min_savings",
            }
        )
    )


def _require_started(state: _StreamState) -> None:
    if state.request is None:
        raise DaemonError("observation stream is not started", code="stream_not_started")


def _clear_stream(state: _StreamState) -> None:
    _close_stream_resources(state)
    state.request = None
    state.stream_id = None
    state.paused = False
    state.emit_version = 0
    state.source_version = 0
    state.last_sha256 = None
    state.last_source_sha256 = None
    state.last_image = None
    state.last_tile_hashes = None
    state.last_frame_seq = None
    state.emitted_frames = 0
    state.next_frame_at = 0.0


def _close_stream_resources(state: _StreamState) -> None:
    if state.dirty_frame_producer is not None:
        state.dirty_frame_producer.close_sync()
    state.dirty_frame_producer = None
    state.dirty_frame_display = None
    if state.xdamage_watcher is not None:
        state.xdamage_watcher.close()
    state.xdamage_watcher = None
    state.xdamage_display = None


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
