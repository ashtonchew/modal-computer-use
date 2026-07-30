from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .client import AsyncDaemonClient, DaemonClient
    from .hot_session import AsyncHotSessionClient, HotSessionClient
    from .latency import SessionStartupTiming
    from .models import Screenshot
    from .namespaces import (
        ActionsNamespace,
        AppsNamespace,
        ArtifactsNamespace,
        AsyncActionsNamespace,
        AsyncAppsNamespace,
        AsyncArtifactsNamespace,
        AsyncBrowserNamespace,
        AsyncClipboardNamespace,
        AsyncCommandsNamespace,
        AsyncDisplayNamespace,
        AsyncInputNamespace,
        AsyncKeyboardNamespace,
        AsyncMouseNamespace,
        AsyncRecordingsNamespace,
        AsyncScreenshotsNamespace,
        AsyncWindowsNamespace,
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
    from .observations import AsyncObservationClient, ObservationClient
    from .session_lease import AsyncSessionLeaseCoordinator, SessionLeaseCoordinator


class BorrowedComputer:
    """Lifecycle-restricted synchronous daemon primitives for one lease."""

    __slots__ = (
        "__active",
        "__base_url",
        "__client",
        "__coordinator",
        "__http2",
        "__token",
    )

    def __init__(
        self,
        client: DaemonClient,
        coordinator: SessionLeaseCoordinator,
        *,
        base_url: str,
        token: str | None,
        http2: bool,
    ) -> None:
        self.__client = client
        self.__coordinator = coordinator
        self.__active = True
        self.__base_url = base_url
        self.__token = token
        self.__http2 = http2

    def __repr__(self) -> str:
        return "BorrowedComputer()"

    def _invalidate(self) -> None:
        self.__active = False
        self.__base_url = ""
        self.__token = None

    def __ensure_active(self) -> None:
        if not self.__active:
            from .errors import SessionLeaseLostError

            raise SessionLeaseLostError()

    @property
    def actions(self) -> ActionsNamespace:
        from .namespaces import ActionsNamespace

        return ActionsNamespace(self.__client)

    @property
    def apps(self) -> AppsNamespace:
        from .namespaces import AppsNamespace

        return AppsNamespace(self.__client)

    @property
    def artifacts(self) -> ArtifactsNamespace:
        from .namespaces import ArtifactsNamespace

        return ArtifactsNamespace(self.__client)

    @property
    def browser(self) -> BrowserNamespace:
        from .namespaces import BrowserNamespace

        return BrowserNamespace(self.__client)

    @property
    def clipboard(self) -> ClipboardNamespace:
        from .namespaces import ClipboardNamespace

        return ClipboardNamespace(self.__client)

    @property
    def commands(self) -> CommandsNamespace:
        from .namespaces import CommandsNamespace

        return CommandsNamespace(self.__client)

    @property
    def display(self) -> DisplayNamespace:
        from .namespaces import DisplayNamespace

        return DisplayNamespace(self.__client)

    @property
    def input(self) -> InputNamespace:
        from .namespaces import InputNamespace

        return InputNamespace(self.__client)

    @property
    def keyboard(self) -> KeyboardNamespace:
        from .namespaces import KeyboardNamespace

        return KeyboardNamespace(self.__client)

    @property
    def mouse(self) -> MouseNamespace:
        from .namespaces import MouseNamespace

        return MouseNamespace(self.__client)

    @property
    def recordings(self) -> RecordingsNamespace:
        from .namespaces import RecordingsNamespace

        return RecordingsNamespace(self.__client)

    @property
    def screenshots(self) -> ScreenshotsNamespace:
        from .namespaces import ScreenshotsNamespace

        return ScreenshotsNamespace(self.__client)

    @property
    def windows(self) -> WindowsNamespace:
        from .namespaces import WindowsNamespace

        return WindowsNamespace(self.__client)

    def hot_session(self, *, timeout: float = 30.0) -> HotSessionClient:
        from .hot_session import HotSessionClient
        from .transports import HotSessionTransport

        self.__ensure_active()
        self.__coordinator.ensure_open()
        transport = HotSessionTransport(
            self.__base_url,
            token=self.__token,
            timeout=timeout,
            _metadata_headers=self.__coordinator.metadata_headers,
            _mutation_executor=self.__coordinator.execute,
        )
        return self.__coordinator.track(HotSessionClient(transport))

    def observe_after_result_loss(self) -> Screenshot:
        """Capture one fixed full inline PNG after a proven completed operation."""
        from .namespaces import ScreenshotsNamespace

        self.__ensure_active()
        return self.__coordinator.observe_after_result_loss(
            lambda: ScreenshotsNamespace(self.__client).full(
                format="png",
                processing="daemon",
                storage="inline",
            )
        )

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
        from .observations import ObservationClient
        from .transports import ObservationStreamTransport

        self.__ensure_active()
        self.__coordinator.ensure_open()
        transport = ObservationStreamTransport(
            self.__base_url,
            token=self.__token,
            timeout=timeout,
            _metadata_headers=self.__coordinator.metadata_headers,
            _mutation_executor=self.__coordinator.execute,
        )
        client = ObservationClient(
            transport,
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
            startup_timing=timing,
        )
        return self.__coordinator.track(client)


class AsyncBorrowedComputer:
    """Lifecycle-restricted native-async daemon primitives for one lease."""

    __slots__ = (
        "__active",
        "__base_url",
        "__client",
        "__coordinator",
        "__http2",
        "__token",
    )

    def __init__(
        self,
        client: AsyncDaemonClient,
        coordinator: AsyncSessionLeaseCoordinator,
        *,
        base_url: str,
        token: str | None,
        http2: bool,
    ) -> None:
        self.__client = client
        self.__coordinator = coordinator
        self.__active = True
        self.__base_url = base_url
        self.__token = token
        self.__http2 = http2

    def __repr__(self) -> str:
        return "AsyncBorrowedComputer()"

    def _invalidate(self) -> None:
        self.__active = False
        self.__base_url = ""
        self.__token = None

    def __ensure_active(self) -> None:
        if not self.__active:
            from .errors import SessionLeaseLostError

            raise SessionLeaseLostError()

    @property
    def actions(self) -> AsyncActionsNamespace:
        from .namespaces import AsyncActionsNamespace

        return AsyncActionsNamespace(self.__client)

    @property
    def apps(self) -> AsyncAppsNamespace:
        from .namespaces import AsyncAppsNamespace

        return AsyncAppsNamespace(self.__client)

    @property
    def artifacts(self) -> AsyncArtifactsNamespace:
        from .namespaces import AsyncArtifactsNamespace

        return AsyncArtifactsNamespace(self.__client)

    @property
    def browser(self) -> AsyncBrowserNamespace:
        from .namespaces import AsyncBrowserNamespace

        return AsyncBrowserNamespace(self.__client)

    @property
    def clipboard(self) -> AsyncClipboardNamespace:
        from .namespaces import AsyncClipboardNamespace

        return AsyncClipboardNamespace(self.__client)

    @property
    def commands(self) -> AsyncCommandsNamespace:
        from .namespaces import AsyncCommandsNamespace

        return AsyncCommandsNamespace(self.__client)

    @property
    def display(self) -> AsyncDisplayNamespace:
        from .namespaces import AsyncDisplayNamespace

        return AsyncDisplayNamespace(self.__client)

    @property
    def input(self) -> AsyncInputNamespace:
        from .namespaces import AsyncInputNamespace

        return AsyncInputNamespace(self.__client)

    @property
    def keyboard(self) -> AsyncKeyboardNamespace:
        from .namespaces import AsyncKeyboardNamespace

        return AsyncKeyboardNamespace(self.__client)

    @property
    def mouse(self) -> AsyncMouseNamespace:
        from .namespaces import AsyncMouseNamespace

        return AsyncMouseNamespace(self.__client)

    @property
    def recordings(self) -> AsyncRecordingsNamespace:
        from .namespaces import AsyncRecordingsNamespace

        return AsyncRecordingsNamespace(self.__client)

    @property
    def screenshots(self) -> AsyncScreenshotsNamespace:
        from .namespaces import AsyncScreenshotsNamespace

        return AsyncScreenshotsNamespace(self.__client)

    @property
    def windows(self) -> AsyncWindowsNamespace:
        from .namespaces import AsyncWindowsNamespace

        return AsyncWindowsNamespace(self.__client)

    def hot_session(self, *, timeout: float = 30.0) -> AsyncHotSessionClient:
        from .hot_session import AsyncHotSessionClient
        from .transports import AsyncHotSessionTransport

        self.__ensure_active()
        self.__coordinator.ensure_open()
        transport = AsyncHotSessionTransport(
            self.__base_url,
            token=self.__token,
            timeout=timeout,
            _metadata_headers=self.__coordinator.metadata_headers,
            _mutation_executor=self.__coordinator.execute,
        )
        return self.__coordinator.track(AsyncHotSessionClient(transport))

    async def observe_after_result_loss(self) -> Screenshot:
        """Capture one fixed full inline PNG after a proven completed operation."""
        from .namespaces import AsyncScreenshotsNamespace

        self.__ensure_active()

        async def observe() -> Screenshot:
            return await AsyncScreenshotsNamespace(self.__client).full(
                format="png",
                processing="daemon",
                storage="inline",
            )

        return await self.__coordinator.observe_after_result_loss(observe)

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
    ) -> AsyncObservationClient:
        from .observations import AsyncObservationClient
        from .transports import AsyncObservationStreamTransport

        self.__ensure_active()
        self.__coordinator.ensure_open()
        transport = AsyncObservationStreamTransport(
            self.__base_url,
            token=self.__token,
            timeout=timeout,
            _metadata_headers=self.__coordinator.metadata_headers,
            _mutation_executor=self.__coordinator.execute,
        )
        client = AsyncObservationClient(
            transport,
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
            startup_timing=timing,
        )
        return self.__coordinator.track(client)
