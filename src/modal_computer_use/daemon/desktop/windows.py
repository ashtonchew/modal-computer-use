from __future__ import annotations

import subprocess
from collections.abc import Awaitable, Callable

from modal_computer_use.models import ActionResult, X11Window

RunCommand = Callable[..., Awaitable[subprocess.CompletedProcess[str]]]


class X11WindowController:
    def __init__(
        self,
        *,
        run: RunCommand,
        fallback_windows: Callable[[], Awaitable[list[X11Window]]],
    ) -> None:
        self._run = run
        self._fallback_windows = fallback_windows

    async def list(self) -> list[X11Window]:
        result = await self._run("wmctrl", "-lpGx", timeout=2, check=False)
        if result.returncode != 0:
            return await self._fallback_windows()
        active = await self._run("xdotool", "getactivewindow", timeout=2, check=False)
        active_id = normalize_window_id(active.stdout)
        windows: list[X11Window] = []
        for line in result.stdout.splitlines():
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            window_id, _desktop, pid, x, y, width, height, class_name, title = parts
            try:
                windows.append(
                    X11Window(
                        id=window_id,
                        title=title,
                        class_name=class_name,
                        pid=int(pid) if pid != "0" else None,
                        x=int(x),
                        y=int(y),
                        width=int(width),
                        height=int(height),
                        is_active=normalize_window_id(window_id) == active_id,
                    )
                )
            except ValueError:
                continue
        return windows

    async def active(self) -> X11Window | None:
        windows = await self.list()
        for window in windows:
            if window.is_active:
                return window
        return windows[0] if windows else None

    async def activate(self, window_id: str) -> ActionResult:
        result = await self._run("wmctrl", "-ia", window_id, timeout=5, check=False)
        return ActionResult(
            ok=result.returncode == 0,
            message=None if result.returncode == 0 else "failed to activate window",
            output={"window_id": window_id},
        )

    async def close(self, window_id: str) -> ActionResult:
        result = await self._run("wmctrl", "-ic", window_id, timeout=5, check=False)
        return ActionResult(
            ok=result.returncode == 0,
            message=None if result.returncode == 0 else "failed to close window",
            output={"window_id": window_id},
        )


def normalize_window_id(value: str) -> str:
    raw = value.strip().lower()
    if not raw:
        return ""
    try:
        return f"0x{int(raw, 0):08x}"
    except ValueError:
        return raw
