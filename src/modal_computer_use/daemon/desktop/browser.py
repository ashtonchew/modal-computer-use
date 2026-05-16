from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from modal_computer_use.models import ActionResult, X11Window


class X11BrowserController:
    def __init__(
        self,
        *,
        browser: str | None,
        launch: Callable[[str, list[str]], Awaitable[ActionResult]],
        windows: Callable[[], Awaitable[list[X11Window]]],
    ) -> None:
        self.browser = browser
        self._launch = launch
        self._windows = windows

    async def open_url(self, url: str, wait_for_window: bool = True) -> ActionResult:
        before = len(await self._windows()) if wait_for_window else None
        result = await self._launch(self.browser or "xdg-open", [url])
        if not result.ok or not wait_for_window:
            return result
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            windows = await self._windows()
            if before is None or len(windows) > before:
                result.output["windows"] = len(windows)
                return result
            await asyncio.sleep(0.2)
        result.output["windows"] = before
        result.output["wait_for_window_timed_out"] = True
        return result
