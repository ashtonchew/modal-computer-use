from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, Literal

from .models import ScreenshotOptions
from .transports.observation import ObservationFrame, ObservationStreamTransport


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
        frame_encoding: Literal["json-binary", "binary-envelope"] | None = None,
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

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> ObservationClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def frames(self) -> Iterator[ObservationFrame]:
        yield from self.transport.frames(self.payload)

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
