from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .actions import normalize_actions
from .models import ActionBatchResult, ComputerAction, ScreenshotOptions
from .transports import (
    AsyncHotSessionBinaryResult,
    AsyncHotSessionTransport,
    HotSessionBinaryResult,
    HotSessionTransport,
)


class HotSessionClient:
    """Synchronous SDK facade over the daemon hot-session protocol."""

    def __init__(self, transport: HotSessionTransport) -> None:
        self.transport = transport

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> HotSessionClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def ping(self) -> dict[str, Any]:
        return self.transport.ping()

    def run_actions(
        self,
        actions: Iterable[ComputerAction | dict[str, Any]],
        *,
        source: str = "sdk-hot-session",
        continue_on_error: bool = False,
    ) -> ActionBatchResult:
        payload = {
            "actions": [action.model_dump(mode="json") for action in normalize_actions(actions)],
            "source": source,
            "continue_on_error": continue_on_error,
        }
        return ActionBatchResult.model_validate(self.transport.request("run_actions", payload))

    def run_actions_with_raw_screenshot(
        self,
        actions: Iterable[ComputerAction | dict[str, Any]],
        *,
        screenshot_options: ScreenshotOptions | Mapping[str, Any] | None = None,
        source: str = "sdk-hot-session",
        continue_on_error: bool = False,
    ) -> HotSessionBinaryResult:
        options = _screenshot_options_payload(screenshot_options)
        payload = {
            "actions": [action.model_dump(mode="json") for action in normalize_actions(actions)],
            "screenshot_after": True,
            "screenshot_options": options,
            "source": source,
            "continue_on_error": continue_on_error,
        }
        return self.transport.request_binary("run_raw_screenshot", payload)

    def screenshot_raw(
        self,
        options: ScreenshotOptions | Mapping[str, Any] | None = None,
    ) -> HotSessionBinaryResult:
        return self.transport.request_binary("screenshot_raw", _screenshot_options_payload(options))


class AsyncHotSessionClient:
    """Native async SDK facade over the daemon hot-session protocol."""

    def __init__(self, transport: AsyncHotSessionTransport) -> None:
        self.transport = transport
        self._closed = False

    async def aclose(self) -> None:
        if self._closed:
            return
        await self.transport.aclose()
        self._closed = True

    async def __aenter__(self) -> AsyncHotSessionClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def ping(self) -> dict[str, Any]:
        return await self.transport.ping()

    async def run_actions(
        self,
        actions: Iterable[ComputerAction | dict[str, Any]],
        *,
        source: str = "sdk-hot-session",
        continue_on_error: bool = False,
    ) -> ActionBatchResult:
        payload = {
            "actions": [action.model_dump(mode="json") for action in normalize_actions(actions)],
            "source": source,
            "continue_on_error": continue_on_error,
        }
        return ActionBatchResult.model_validate(
            await self.transport.request("run_actions", payload)
        )

    async def run_actions_with_raw_screenshot(
        self,
        actions: Iterable[ComputerAction | dict[str, Any]],
        *,
        screenshot_options: ScreenshotOptions | Mapping[str, Any] | None = None,
        source: str = "sdk-hot-session",
        continue_on_error: bool = False,
    ) -> AsyncHotSessionBinaryResult:
        payload = {
            "actions": [action.model_dump(mode="json") for action in normalize_actions(actions)],
            "screenshot_after": True,
            "screenshot_options": _screenshot_options_payload(screenshot_options),
            "source": source,
            "continue_on_error": continue_on_error,
        }
        return await self.transport.request_binary("run_raw_screenshot", payload)

    async def screenshot_raw(
        self,
        options: ScreenshotOptions | Mapping[str, Any] | None = None,
    ) -> AsyncHotSessionBinaryResult:
        return await self.transport.request_binary(
            "screenshot_raw", _screenshot_options_payload(options)
        )


def _screenshot_options_payload(
    options: ScreenshotOptions | Mapping[str, Any] | None,
) -> dict[str, Any]:
    if options is None:
        return ScreenshotOptions(show_cursor=False).model_dump(mode="json")
    if isinstance(options, ScreenshotOptions):
        return options.model_dump(mode="json")
    return dict(options)
