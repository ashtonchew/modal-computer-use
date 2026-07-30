from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .hot_session import HotSessionClient
    from .latency import SessionStartupTiming
    from .namespaces import (
        ActionsNamespace,
        AppsNamespace,
        ArtifactsNamespace,
        BrowserNamespace,
        ClipboardNamespace,
        CommandsNamespace,
        DisplayNamespace,
        InputNamespace,
        KeyboardNamespace,
        MouseNamespace,
        RecordingsNamespace,
        ScreenshotsNamespace,
        WindowsNamespace,
    )
    from .observations import ObservationClient
    from .sandbox import ComputerSandbox


class BorrowedComputer:
    """Lifecycle-restricted daemon capabilities for a borrowed desktop.

    This wrapper shapes authority for trusted caller code. It is not a security
    boundary for malicious Python code running in the same process.
    """

    __slots__ = ("__computer",)

    def __init__(self, computer: ComputerSandbox) -> None:
        self.__computer = computer

    def __repr__(self) -> str:
        return "BorrowedComputer()"

    @property
    def actions(self) -> ActionsNamespace:
        return self.__computer.actions

    @property
    def apps(self) -> AppsNamespace:
        return self.__computer.apps

    @property
    def artifacts(self) -> ArtifactsNamespace:
        return self.__computer.artifacts

    @property
    def browser(self) -> BrowserNamespace:
        return self.__computer.browser

    @property
    def clipboard(self) -> ClipboardNamespace:
        return self.__computer.clipboard

    @property
    def commands(self) -> CommandsNamespace:
        return self.__computer.commands

    @property
    def display(self) -> DisplayNamespace:
        return self.__computer.display

    @property
    def input(self) -> InputNamespace:
        return self.__computer.input

    @property
    def keyboard(self) -> KeyboardNamespace:
        return self.__computer.keyboard

    @property
    def mouse(self) -> MouseNamespace:
        return self.__computer.mouse

    @property
    def recordings(self) -> RecordingsNamespace:
        return self.__computer.recordings

    @property
    def screenshots(self) -> ScreenshotsNamespace:
        return self.__computer.screenshots

    @property
    def windows(self) -> WindowsNamespace:
        return self.__computer.windows

    def hot_session(self, *, timeout: float = 30.0) -> HotSessionClient:
        return self.__computer.hot_session(timeout=timeout)

    def observation_stream(
        self,
        *,
        options: dict[str, Any] | None = None,
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
        frame_encoding: Literal["json-binary", "binary-envelope"] | None = "binary-envelope",
        timeout: float = 30.0,
        timing: SessionStartupTiming | None = None,
    ) -> ObservationClient:
        return self.__computer.observation_stream(
            options=options,
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
            frame_encoding=frame_encoding,
            timeout=timeout,
            timing=timing,
        )
