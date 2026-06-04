from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .models import ScreenshotOptions
from .transports.observation import ObservationFrame, ObservationStreamTransport

SDK_AUTO_REGION_RADIUS = 64


@dataclass(frozen=True)
class ActionObservationResult:
    """Result for one causal action plus observation stream command."""

    frame: ObservationFrame

    @property
    def action_result(self) -> dict[str, Any] | None:
        value = self.frame.metadata.get("action_result")
        return value if isinstance(value, dict) else None

    @property
    def action_id(self) -> str | None:
        value = self.frame.metadata.get("action_id")
        return value if isinstance(value, str) else None

    @property
    def timings(self) -> dict[str, Any]:
        return {
            "change_stage_timing_ms": self.frame.metadata.get("change_stage_timing_ms"),
            "screenshot_daemon_timing_ms": self.frame.metadata.get("timing_ms"),
            "transport_timing": self.frame.transport_timing,
        }

    @property
    def change_detected(self) -> bool | None:
        value = self.frame.metadata.get("change_detected")
        return value if isinstance(value, bool) else None

    @property
    def change_timeout_reached(self) -> bool | None:
        value = self.frame.metadata.get("change_timeout_reached")
        return value if isinstance(value, bool) else None


class ObservationClient:
    """Synchronous SDK facade over the daemon observation stream protocol."""

    def __init__(
        self,
        transport: ObservationStreamTransport,
        *,
        options: ScreenshotOptions | Mapping[str, Any] | None = None,
        fps: float = 5.0,
        max_frames: int | None = None,
        idle_timeout_ms: int | None = None,
        send_unchanged: bool = False,
        delivery: Literal["latest", "reliable"] | None = None,
        delta_mode: Literal["auto", "off"] | None = None,
        delta_max_ratio: float | None = None,
        keyframe_interval: int | None = None,
        tile_size: int | None = None,
        max_patch_rects: int | None = None,
        multi_rect_min_savings: float | None = None,
        transport_timing: bool = False,
        frame_encoding: Literal["json-binary", "binary-envelope"] | None = "binary-envelope",
    ) -> None:
        self.transport = transport
        self.payload = _observation_payload(
            options,
            fps=fps,
            max_frames=max_frames,
            idle_timeout_ms=idle_timeout_ms,
            send_unchanged=send_unchanged,
            delivery=delivery,
            delta_mode=delta_mode,
            delta_max_ratio=delta_max_ratio,
            keyframe_interval=keyframe_interval,
            tile_size=tile_size,
            max_patch_rects=max_patch_rects,
            multi_rect_min_savings=multi_rect_min_savings,
            transport_timing=transport_timing,
            frame_encoding=frame_encoding,
        )
        self._started = False

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> ObservationClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def frames(self) -> Iterator[ObservationFrame]:
        if self._started:
            while True:
                try:
                    yield self.transport.receive_frame(
                        transport_timing=bool(self.payload.get("transport_timing"))
                    )
                except StopIteration:
                    self._started = False
                    return
        else:
            self._started = True
            yield from self.transport.frames(self.payload)

    def start(self, *, drain_initial_frame: bool = False) -> ObservationFrame | None:
        if self._started:
            return None
        self.transport.start(self.payload)
        self._started = True
        if drain_initial_frame:
            return self.transport.receive_frame(
                transport_timing=bool(self.payload.get("transport_timing"))
            )
        return None

    def pause(self) -> None:
        self.transport.pause()

    def resume(self) -> None:
        self.transport.resume()

    def request_frame(self) -> None:
        self.transport.request_frame()

    def run_actions_capture(self, **payload: Any) -> None:
        self.transport.run_actions_capture(payload)

    def run_actions_observe_change(self, **payload: Any) -> None:
        self.transport.run_actions_observe_change(payload)

    def act_and_observe(
        self,
        *,
        actions: list[Mapping[str, Any]],
        source: str = "sdk",
        capture_delay_ms: int = 0,
        change_timeout_ms: int = 100,
        poll_interval_ms: int = 8,
        poll_strategy: Literal["fixed", "adaptive"] = "adaptive",
        change_detection: Literal["auto", "full", "auto_region"] = "auto",
        change_signal: Literal["poll", "xdamage", "auto"] = "auto",
        dirty_frame_producer: Literal["auto", "off"] = "auto",
        full_frame_fallback: bool | None = None,
        frame_encoding: Literal["json-binary", "binary-envelope"] | None = None,
        change_detection_region: Mapping[str, Any] | None = None,
        change_region_radius: int | None = None,
        continue_on_error: bool = False,
    ) -> ActionObservationResult:
        self.start(drain_initial_frame=True)
        action_payloads = [dict(action) for action in actions]
        resolved_change_detection = _resolve_action_change_detection(
            action_payloads,
            requested=change_detection,
            change_detection_region=change_detection_region,
        )
        payload: dict[str, Any] = {
            "actions": action_payloads,
            "source": source,
            "capture_delay_ms": capture_delay_ms,
            "change_timeout_ms": change_timeout_ms,
            "poll_interval_ms": poll_interval_ms,
            "poll_strategy": poll_strategy,
            "change_detection": resolved_change_detection,
            "change_signal": change_signal,
            "dirty_frame_producer": dirty_frame_producer,
            "full_frame_fallback": _resolve_full_frame_fallback(
                resolved_change_detection,
                requested=full_frame_fallback,
            ),
        }
        if frame_encoding is not None:
            payload["frame_encoding"] = frame_encoding
        if continue_on_error:
            payload["continue_on_error"] = True
        if change_detection_region is not None:
            payload["change_detection_region"] = dict(change_detection_region)
        resolved_change_region_radius = _resolve_change_region_radius(
            resolved_change_detection,
            requested=change_region_radius,
        )
        if resolved_change_region_radius is not None:
            payload["change_region_radius"] = resolved_change_region_radius
        frame = self.transport.run_actions_observe_change_and_recv(
            payload,
            transport_timing=bool(self.payload.get("transport_timing")),
        )
        return ActionObservationResult(frame=frame)

    def configure(self, **payload: Any) -> None:
        self.transport.configure(payload)


def _observation_payload(
    options: ScreenshotOptions | Mapping[str, Any] | None,
    *,
    fps: float,
    max_frames: int | None,
    idle_timeout_ms: int | None,
    send_unchanged: bool,
    delivery: Literal["latest", "reliable"] | None,
    delta_mode: Literal["auto", "off"] | None,
    delta_max_ratio: float | None,
    keyframe_interval: int | None,
    tile_size: int | None,
    max_patch_rects: int | None,
    multi_rect_min_savings: float | None,
    transport_timing: bool,
    frame_encoding: Literal["json-binary", "binary-envelope"] | None,
) -> dict[str, Any]:
    if options is None:
        payload = ScreenshotOptions(format="png", show_cursor=False).model_dump(mode="json")
    elif isinstance(options, ScreenshotOptions):
        payload = options.model_dump(mode="json")
    else:
        payload = dict(options)
    payload.update(
        {
            "fps": fps,
            "send_unchanged": send_unchanged,
        }
    )
    if transport_timing:
        payload["transport_timing"] = True
    if frame_encoding is not None:
        payload["frame_encoding"] = frame_encoding
    if delivery is not None:
        payload["delivery"] = delivery
    if max_frames is not None:
        payload["max_frames"] = max_frames
    if idle_timeout_ms is not None:
        payload["idle_timeout_ms"] = idle_timeout_ms
    if delta_mode is not None:
        payload["delta_mode"] = delta_mode
    if delta_max_ratio is not None:
        payload["delta_max_ratio"] = delta_max_ratio
    if keyframe_interval is not None:
        payload["keyframe_interval"] = keyframe_interval
    if tile_size is not None:
        payload["tile_size"] = tile_size
    if max_patch_rects is not None:
        payload["max_patch_rects"] = max_patch_rects
    if multi_rect_min_savings is not None:
        payload["multi_rect_min_savings"] = multi_rect_min_savings
    return payload


def _resolve_action_change_detection(
    actions: list[Mapping[str, Any]],
    *,
    requested: Literal["auto", "full", "auto_region"],
    change_detection_region: Mapping[str, Any] | None,
) -> Literal["full", "auto_region"]:
    if requested != "auto":
        return requested
    if change_detection_region is not None:
        return "auto_region"
    for action in reversed(actions):
        if _action_is_neutral_for_region_policy(action):
            continue
        if _action_has_observation_point(action):
            return "auto_region"
        return "full"
    return "full"


def _resolve_full_frame_fallback(
    change_detection: Literal["full", "auto_region"],
    *,
    requested: bool | None,
) -> bool:
    if requested is not None:
        return requested
    return change_detection == "full"


def _resolve_change_region_radius(
    change_detection: Literal["full", "auto_region"],
    *,
    requested: int | None,
) -> int | None:
    if requested is not None:
        return requested
    if change_detection == "auto_region":
        return SDK_AUTO_REGION_RADIUS
    return None


def _action_has_observation_point(action: Mapping[str, Any]) -> bool:
    return _has_int_pair(action, "x", "y") or _has_int_pair(action, "end_x", "end_y")


def _action_is_neutral_for_region_policy(action: Mapping[str, Any]) -> bool:
    return action.get("type") == "wait"


def _has_int_pair(action: Mapping[str, Any], x_key: str, y_key: str) -> bool:
    x = action.get(x_key)
    y = action.get(y_key)
    return type(x) is int and type(y) is int
