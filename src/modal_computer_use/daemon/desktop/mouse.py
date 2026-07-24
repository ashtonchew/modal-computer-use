from __future__ import annotations

import asyncio
import contextlib
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from typing import Literal

from modal_computer_use.actions import normalize_key
from modal_computer_use.daemon.desktop.xtest import (
    ButtonEvent,
    KeyEvent,
    MotionEvent,
    X11InputInjectionError,
    X11InputSession,
    X11InputUnavailableError,
)
from modal_computer_use.models import ActionResult, Point

RunCommand = Callable[..., Awaitable[subprocess.CompletedProcess[str]]]
KeyAction = Callable[[str], Awaitable[None]]

BUTTON_NUMBERS = {"left": "1", "middle": "2", "right": "3"}
SCROLL_BUTTONS = {"up": "4", "down": "5", "left": "6", "right": "7"}
MODIFIER_KEYSYMS = {
    "alt": "Alt_L",
    "alt_l": "Alt_L",
    "alt_r": "Alt_R",
    "control": "Control_L",
    "control_l": "Control_L",
    "control_r": "Control_R",
    "ctrl": "Control_L",
    "shift": "Shift_L",
    "shift_l": "Shift_L",
    "shift_r": "Shift_R",
    "super": "Super_L",
    "super_l": "Super_L",
    "super_r": "Super_R",
}


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
        input_backend: Literal["auto", "xtest", "xdotool"] = "auto",
        xtest: X11InputSession | None = None,
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
        self._configured_backend = input_backend
        self._xtest = xtest
        self._active_backend = "xdotool"

    @property
    def backend_name(self) -> str:
        return self._active_backend

    def probe_backend(self) -> tuple[bool, str | None]:
        if self._configured_backend == "xdotool":
            self._active_backend = "xdotool"
            return True, None
        if self._xtest is not None and self._xtest.available():
            self._active_backend = "xtest"
            return True, None
        self._active_backend = "xdotool"
        if self._configured_backend == "xtest":
            reason = self._xtest.failure if self._xtest is not None else "XTest backend not created"
            return False, reason or "XTest backend unavailable"
        return True, None

    async def move(self, x: int, y: int) -> Point:
        if self._try_xtest_move(x, y):
            return await self._move_state(x, y)
        await self._run("xdotool", "mousemove", str(x), str(y))
        self._active_backend = "xdotool"
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
        modifier_keys = _normalized_modifiers(modifiers)
        if self._try_xtest_click(
            int(button_number),
            count=count,
            x=x,
            y=y,
            modifiers=modifier_keys,
        ):
            return await self._click_state(x, y, button=button, count=count, modifiers=modifiers)
        if not modifier_keys:
            if x is not None and y is not None:
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
            else:
                await self._run("xdotool", "click", "--repeat", str(count), button_number)
            self._active_backend = "xdotool"
            return await self._click_state(x, y, button=button, count=count, modifiers=modifiers)
        if x is not None and y is not None:
            await self.move(x, y)
        for modifier in modifier_keys:
            await self._key_down(modifier)
        try:
            await self._run("xdotool", "click", "--repeat", str(count), button_number)
            self._active_backend = "xdotool"
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
                await self.move(point.x, point.y)
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
        if self._try_xtest_scroll(
            int(SCROLL_BUTTONS[direction]),
            amount=amount,
            x=x,
            y=y,
        ):
            return await self._scroll_state(direction, amount=amount, x=x, y=y)
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
        self._active_backend = "xdotool"
        return await self._scroll_state(direction, amount=amount, x=x, y=y)

    async def down(
        self, button: str = "left", x: int | None = None, y: int | None = None
    ) -> ActionResult:
        if self._try_xtest_down(int(BUTTON_NUMBERS[button]), x=x, y=y):
            return await self._button_down_state(button, x=x, y=y)
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
        self._active_backend = "xdotool"
        return await self._button_down_state(button, x=x, y=y)

    async def up(
        self, button: str = "left", x: int | None = None, y: int | None = None
    ) -> ActionResult:
        if self._try_xtest_up(int(BUTTON_NUMBERS[button]), x=x, y=y):
            return await self._button_up_state(button, x=x, y=y)
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
        self._active_backend = "xdotool"
        return await self._button_up_state(button, x=x, y=y)

    async def position(self) -> Point:
        if self._can_use_xtest() and self._xtest is not None:
            try:
                x, y = self._xtest.pointer_position()
            except X11InputUnavailableError as exc:
                self._handle_xtest_failure(exc)
            else:
                self._active_backend = "xtest"
                return await self._move_state(x, y)
        result = await self._run("xdotool", "getmouselocation", "--shell")
        values: dict[str, int] = {}
        for line in result.stdout.splitlines():
            key, _, value = line.partition("=")
            if key in {"X", "Y"} and value.isdigit():
                values[key] = int(value)
        if "X" not in values or "Y" not in values:
            return await self._position_state()
        return await self._move_state(values["X"], values["Y"])

    def _can_use_xtest(self) -> bool:
        if self._configured_backend == "xdotool" or self._xtest is None:
            return False
        if self._configured_backend == "xtest":
            return True
        return self._xtest.available()

    def _handle_xtest_failure(self, exc: X11InputUnavailableError) -> bool:
        if self._configured_backend == "xtest":
            raise exc
        self._active_backend = "xdotool"
        return False

    def _try_xtest_move(self, x: int, y: int) -> bool:
        if not self._can_use_xtest() or self._xtest is None:
            return False
        try:
            self._xtest.emit((MotionEvent(x, y),))
        except X11InputUnavailableError as exc:
            return self._handle_xtest_failure(exc)
        self._active_backend = "xtest"
        return True

    def _try_xtest_click(
        self,
        button: int,
        *,
        count: int,
        x: int | None,
        y: int | None,
        modifiers: Sequence[str],
    ) -> bool:
        if not self._can_use_xtest() or self._xtest is None:
            return False
        try:
            modifier_keycodes = tuple(
                dict.fromkeys(
                    self._xtest.resolve_keycode(_modifier_keysym(modifier))
                    for modifier in modifiers
                )
            )
            events: list[MotionEvent | KeyEvent | ButtonEvent] = []
            if x is not None and y is not None:
                events.append(MotionEvent(x, y))
            events.extend(KeyEvent(keycode, True) for keycode in modifier_keycodes)
            for _ in range(count):
                events.extend((ButtonEvent(button, True), ButtonEvent(button, False)))
            events.extend(KeyEvent(keycode, False) for keycode in reversed(modifier_keycodes))
            self._xtest.emit(
                events,
                preserve_pressed_keycodes=modifier_keycodes,
            )
        except X11InputUnavailableError as exc:
            return self._handle_xtest_failure(exc)
        except X11InputInjectionError:
            raise
        self._active_backend = "xtest"
        return True

    def _try_xtest_scroll(
        self,
        button: int,
        *,
        amount: int,
        x: int | None,
        y: int | None,
    ) -> bool:
        if not self._can_use_xtest() or self._xtest is None:
            return False
        try:
            events: list[MotionEvent | ButtonEvent] = []
            if x is not None and y is not None:
                events.append(MotionEvent(x, y))
            for _ in range(amount):
                events.extend((ButtonEvent(button, True), ButtonEvent(button, False)))
            self._xtest.emit(events)
        except X11InputUnavailableError as exc:
            return self._handle_xtest_failure(exc)
        self._active_backend = "xtest"
        return True

    def _try_xtest_down(self, button: int, *, x: int | None, y: int | None) -> bool:
        if not self._can_use_xtest() or self._xtest is None:
            return False
        try:
            events: list[MotionEvent | ButtonEvent] = []
            if x is not None and y is not None:
                events.append(MotionEvent(x, y))
            events.append(ButtonEvent(button, True))
            self._xtest.emit(events)
        except X11InputUnavailableError as exc:
            return self._handle_xtest_failure(exc)
        self._active_backend = "xtest"
        return True

    def _try_xtest_up(self, button: int, *, x: int | None, y: int | None) -> bool:
        if not self._can_use_xtest() or self._xtest is None:
            return False
        try:
            events: list[MotionEvent | ButtonEvent] = []
            if x is not None and y is not None:
                events.append(MotionEvent(x, y))
            events.append(ButtonEvent(button, False))
            self._xtest.emit(events)
        except X11InputUnavailableError as exc:
            return self._handle_xtest_failure(exc)
        self._active_backend = "xtest"
        return True


def _normalized_modifiers(modifiers: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    for modifier in modifiers:
        key = normalize_key(modifier)
        if key not in normalized:
            normalized.append(key)
    return normalized


def _modifier_keysym(modifier: str) -> str:
    return MODIFIER_KEYSYMS.get(modifier.lower(), modifier)
