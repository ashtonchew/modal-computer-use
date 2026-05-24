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
        delta_mode: Literal["auto", "off"] | None = None,
        delta_max_ratio: float | None = None,
        keyframe_interval: int | None = None,
        tile_size: int | None = None,
    ) -> None:
        self.transport = transport
        self.payload = _observation_payload(
            options,
            fps=fps,
            max_frames=max_frames,
            idle_timeout_ms=idle_timeout_ms,
            send_unchanged=send_unchanged,
            delta_mode=delta_mode,
            delta_max_ratio=delta_max_ratio,
            keyframe_interval=keyframe_interval,
            tile_size=tile_size,
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

    def configure(self, **payload: Any) -> None:
        self.transport.configure(payload)


def _observation_payload(
    options: ScreenshotOptions | Mapping[str, Any] | None,
    *,
    fps: float,
    max_frames: int | None,
    idle_timeout_ms: int | None,
    send_unchanged: bool,
    delta_mode: Literal["auto", "off"] | None,
    delta_max_ratio: float | None,
    keyframe_interval: int | None,
    tile_size: int | None,
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
    return payload
