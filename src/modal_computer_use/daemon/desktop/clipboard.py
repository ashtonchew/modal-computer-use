from __future__ import annotations

import subprocess
from collections.abc import Awaitable, Callable

from modal_computer_use.models import ActionResult

RunCommand = Callable[..., Awaitable[subprocess.CompletedProcess[str]]]


class X11ClipboardController:
    def __init__(
        self,
        *,
        run: RunCommand,
        get_state: Callable[[], Awaitable[str]],
        set_state: Callable[[str], Awaitable[ActionResult]],
        clear_state: Callable[[], Awaitable[ActionResult]],
    ) -> None:
        self._run = run
        self._get_state = get_state
        self._set_state = set_state
        self._clear_state = clear_state

    async def get(self) -> str:
        result = await self._run("xclip", "-selection", "clipboard", "-o", check=False)
        if result.returncode != 0:
            await self._set_state("")
            return ""
        await self._set_state(result.stdout)
        return await self._get_state()

    async def set(self, text: str) -> ActionResult:
        await self._run(
            "xclip",
            "-selection",
            "clipboard",
            input_text=text,
            capture_output=False,
        )
        return await self._set_state(text)

    async def clear(self) -> ActionResult:
        await self._run(
            "xclip",
            "-selection",
            "clipboard",
            input_text="",
            capture_output=False,
        )
        return await self._clear_state()
