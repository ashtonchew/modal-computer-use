from __future__ import annotations

import asyncio
import contextlib
import subprocess
from collections.abc import Awaitable, Callable, Sequence

from modal_computer_use.actions import KEY_ALIASES, normalize_key, normalize_key_combo
from modal_computer_use.models import ActionResult

RunCommand = Callable[..., Awaitable[subprocess.CompletedProcess[str]]]
ClipboardGet = Callable[[], Awaitable[str]]
ClipboardSet = Callable[[str], Awaitable[ActionResult]]


class X11KeyboardController:
    def __init__(
        self,
        *,
        run: RunCommand,
        type_state: Callable[..., Awaitable[ActionResult]],
        press_state: Callable[..., Awaitable[ActionResult]],
        hotkey_state: Callable[..., Awaitable[ActionResult]],
        key_down_state: Callable[[str], Awaitable[None]],
        key_up_state: Callable[[str], Awaitable[None]],
        clipboard_get: ClipboardGet,
        clipboard_set: ClipboardSet,
    ) -> None:
        self._run = run
        self._type_state = type_state
        self._press_state = press_state
        self._hotkey_state = hotkey_state
        self._key_down_state = key_down_state
        self._key_up_state = key_up_state
        self._clipboard_get = clipboard_get
        self._clipboard_set = clipboard_set

    async def type_text(
        self, text: str, delay_ms: int = 10, method: str = "auto"
    ) -> ActionResult:
        use_clipboard = method == "clipboard" or (
            method == "auto" and (len(text) > 80 or not text.isascii())
        )
        if use_clipboard:
            previous = await self._clipboard_get()
            try:
                await self._clipboard_set(text)
                await self.hotkey(["ctrl", "v"])
            finally:
                await self._clipboard_set(previous)
        else:
            await self._run("xdotool", "type", "--delay", str(delay_ms), text)
        return await self._type_state(text, delay_ms=delay_ms, method=method)

    async def press(
        self, key: str, modifiers: Sequence[str] = (), duration_ms: int = 0
    ) -> ActionResult:
        normalized_key = normalize_key(key)
        modifier_keys = [normalize_key(modifier) for modifier in modifiers]
        for modifier in modifier_keys:
            await self.down(modifier)
        try:
            if duration_ms > 0:
                await self.down(normalized_key)
                await asyncio.sleep(duration_ms / 1000)
                await self.up(normalized_key)
            else:
                await self._run("xdotool", "key", normalized_key)
        finally:
            for modifier in reversed(modifier_keys):
                with contextlib.suppress(Exception):
                    await self.up(modifier)
        return await self._press_state(key, modifiers=modifiers, duration_ms=duration_ms)

    async def hotkey(self, keys: Sequence[str], duration_ms: int = 0) -> ActionResult:
        combo = "+".join(normalize_key_combo(keys))
        await self._run("xdotool", "key", combo)
        return await self._hotkey_state(keys, duration_ms=duration_ms)

    async def down(self, key: str) -> None:
        await self._run("xdotool", "keydown", normalize_key(key))
        await self._key_down_state(key)

    async def up(self, key: str) -> None:
        await self._run("xdotool", "keyup", normalize_key(key))
        await self._key_up_state(key)


__all__ = ["KEY_ALIASES", "X11KeyboardController", "normalize_key", "normalize_key_combo"]
