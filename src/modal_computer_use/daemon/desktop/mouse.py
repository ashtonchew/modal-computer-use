from __future__ import annotations

import asyncio
import contextlib
import subprocess
from collections.abc import Awaitable, Callable, Sequence

from modal_computer_use.actions import normalize_key
from modal_computer_use.models import ActionResult, Point

RunCommand = Callable[..., Awaitable[subprocess.CompletedProcess[str]]]
KeyAction = Callable[[str], Awaitable[None]]

BUTTON_NUMBERS = {"left": "1", "middle": "2", "right": "3"}
SCROLL_BUTTONS = {"up": "4", "down": "5", "left": "6", "right": "7"}


class X11MouseController:
    def __init__(
        self,
        *,
        run: RunCommand,
        move_state: Callable[[int, int], Awaitable[Point]],
        position_state: Callable[[], Awaitable[Point]],
        click_state: Callable[..., Awaitable[Point]],
        drag_state: Callable[..., Awaitable[Point]],
        scroll_state: Callable[..., Awaitable[ActionResult]],
        button_down_state: Callable[..., Awaitable[ActionResult]],
        button_up_state: Callable[..., Awaitable[ActionResult]],
        key_down: KeyAction,
        key_up: KeyAction,
    ) -> None:
        self._run = run
        self._move_state = move_state
        self._position_state = position_state
        self._click_state = click_state
        self._drag_state = drag_state
        self._scroll_state = scroll_state
        self._button_down_state = button_down_state
        self._button_up_state = button_up_state
        self._key_down = key_down
        self._key_up = key_up

    async def move(self, x: int, y: int) -> Point:
        await self._run("xdotool", "mousemove", str(x), str(y))
        return await self._move_state(x, y)

    async def click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        button: str = "left",
        count: int = 1,
        modifiers: Sequence[str] = (),
    ) -> Point:
        button_number = BUTTON_NUMBERS[button]
        modifier_keys = [normalize_key(modifier) for modifier in modifiers]
        if x is not None and y is not None and not modifier_keys:
            click_args = ["click", "--repeat", str(count), button_number]
            if count == 1:
                click_args = ["click", "--delay", "0", "--repeat", "1", button_number]
            await self._run(
                "xdotool",
                "mousemove",
                str(x),
                str(y),
                *click_args,
            )
            return await self._click_state(x, y, button=button, count=count, modifiers=modifiers)
        if x is not None and y is not None:
            await self.move(x, y)
        for modifier in modifier_keys:
            await self._key_down(modifier)
        try:
            await self._run("xdotool", "click", "--repeat", str(count), button_number)
        finally:
            for modifier in reversed(modifier_keys):
                with contextlib.suppress(Exception):
                    await self._key_up(modifier)
        return await self._click_state(x, y, button=button, count=count, modifiers=modifiers)

    async def drag(
        self,
        *,
        start: Point | None = None,
        end: Point | None = None,
        path: Sequence[Point] | None = None,
        button: str = "left",
        duration_ms: int = 500,
        modifiers: Sequence[str] = (),
    ) -> Point:
        points = list(path or [])
        moved_to_path_start = False
        if points and start is None:
            start = points[0]
            await self.move(start.x, start.y)
            points = points[1:]
            moved_to_path_start = True
        if not points:
            if start is not None:
                if not moved_to_path_start:
                    await self.move(start.x, start.y)
            elif end is None:
                start = await self.position()
            if end is not None:
                points = [end]

        interval_ms = duration_ms // max(len(points), 1) if points else 0
        modifier_keys = [normalize_key(modifier) for modifier in modifiers]
        for modifier in modifier_keys:
            await self._key_down(modifier)
        try:
            await self.down(button)
            for point in points:
                await self._run("xdotool", "mousemove", str(point.x), str(point.y))
                if interval_ms > 0:
                    await asyncio.sleep(interval_ms / 1000)
        finally:
            with contextlib.suppress(Exception):
                await self.up(button)
            for modifier in reversed(modifier_keys):
                with contextlib.suppress(Exception):
                    await self._key_up(modifier)
        return await self._drag_state(
            start=start,
            end=end,
            path=path,
            button=button,
            duration_ms=duration_ms,
            modifiers=modifiers,
        )

    async def scroll(
        self,
        direction: str,
        amount: int = 1,
        x: int | None = None,
        y: int | None = None,
    ) -> ActionResult:
        if x is not None and y is not None:
            await self._run(
                "xdotool",
                "mousemove",
                str(x),
                str(y),
                "click",
                "--repeat",
                str(amount),
                SCROLL_BUTTONS[direction],
            )
        else:
            await self._run("xdotool", "click", "--repeat", str(amount), SCROLL_BUTTONS[direction])
        return await self._scroll_state(direction, amount=amount, x=x, y=y)

    async def down(
        self, button: str = "left", x: int | None = None, y: int | None = None
    ) -> ActionResult:
        if x is not None and y is not None:
            await self._run(
                "xdotool",
                "mousemove",
                str(x),
                str(y),
                "mousedown",
                BUTTON_NUMBERS[button],
            )
        else:
            await self._run("xdotool", "mousedown", BUTTON_NUMBERS[button])
        return await self._button_down_state(button, x=x, y=y)

    async def up(
        self, button: str = "left", x: int | None = None, y: int | None = None
    ) -> ActionResult:
        if x is not None and y is not None:
            await self._run(
                "xdotool",
                "mousemove",
                str(x),
                str(y),
                "mouseup",
                BUTTON_NUMBERS[button],
            )
        else:
            await self._run("xdotool", "mouseup", BUTTON_NUMBERS[button])
        return await self._button_up_state(button, x=x, y=y)

    async def position(self) -> Point:
        result = await self._run("xdotool", "getmouselocation", "--shell")
        values: dict[str, int] = {}
        for line in result.stdout.splitlines():
            key, _, value = line.partition("=")
            if key in {"X", "Y"} and value.isdigit():
                values[key] = int(value)
        if "X" not in values or "Y" not in values:
            return await self._position_state()
        return await self._move_state(values["X"], values["Y"])
