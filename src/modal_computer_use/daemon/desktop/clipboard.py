from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable, Callable

from modal_computer_use.models import ActionResult

RunCommand = Callable[..., Awaitable[subprocess.CompletedProcess[str]]]
SpawnOwner = Callable[[str], Awaitable[subprocess.Popen[str]]]


class X11ClipboardController:
    def __init__(
        self,
        *,
        run: RunCommand,
        spawn_owner: SpawnOwner,
        get_state: Callable[[], Awaitable[str]],
        set_state: Callable[[str], Awaitable[ActionResult]],
        clear_state: Callable[[], Awaitable[ActionResult]],
    ) -> None:
        self._run = run
        self._spawn_owner = spawn_owner
        self._get_state = get_state
        self._set_state = set_state
        self._clear_state = clear_state
        self._owner: subprocess.Popen[str] | None = None
        self._owner_lock = asyncio.Lock()

    async def get(self) -> str:
        result = await self._run("xclip", "-selection", "clipboard", "-o", check=False)
        if result.returncode != 0:
            await self._set_state("")
            return ""
        await self._set_state(result.stdout)
        return await self._get_state()

    async def set(self, text: str) -> ActionResult:
        async with self._owner_lock:
            owner = await self._spawn_owner(text)
            try:
                await self._wait_until_owned(text, owner)
            except BaseException:
                self._stop_owner(owner)
                raise
            previous = self._owner
            self._owner = owner
            self._stop_owner(previous)
            return await self._set_state(text)

    async def clear(self) -> ActionResult:
        async with self._owner_lock:
            owner = await self._spawn_owner("")
            try:
                await self._wait_until_owned("", owner)
            except BaseException:
                self._stop_owner(owner)
                raise
            previous = self._owner
            self._owner = owner
            self._stop_owner(previous)
            return await self._clear_state()

    def close(self) -> None:
        owner = self._owner
        self._owner = None
        self._stop_owner(owner)

    async def _wait_until_owned(
        self,
        expected: str,
        owner: subprocess.Popen[str],
    ) -> None:
        for _attempt in range(50):
            if owner.poll() is not None:
                raise RuntimeError("clipboard owner exited before selection was established")
            result = await self._run(
                "xclip",
                "-selection",
                "clipboard",
                "-o",
                timeout=0.5,
                check=False,
            )
            if result.returncode == 0 and result.stdout == expected:
                return
            await asyncio.sleep(0.01)
        raise TimeoutError("clipboard selection was not established")

    @staticmethod
    def _stop_owner(owner: subprocess.Popen[str] | None) -> None:
        if owner is None or owner.poll() is not None:
            return
        owner.terminate()
        try:
            owner.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            owner.kill()
            owner.wait(timeout=0.2)
