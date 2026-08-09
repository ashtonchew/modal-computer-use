from __future__ import annotations

import asyncio
import contextlib
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from typing import Literal

import anyio

from modal_computer_use.actions import normalize_key
from modal_computer_use.daemon.desktop.xtest import (
    ButtonEvent,
    KeyEvent,
    MotionEvent,
    X11InputInjectionError,
    X11InputReleaseError,
    X11InputSession,
    X11InputStateConflictError,
    X11InputUnavailableError,
)
from modal_computer_use.models import ActionResult, Point

RunCommand = Callable[..., Awaitable[subprocess.CompletedProcess[str]]]
KeyAcquire = Callable[[str], Awaitable[bool]]
KeyRelease = Callable[[str], Awaitable[None]]

BUTTON_NUMBERS = {
    "left": "1",
    "middle": "2",
    "right": "3",
    "back": "8",
    "forward": "9",
    "scroll_up": "4",
    "scroll_down": "5",
    "scroll_left": "6",
    "scroll_right": "7",
}
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
        key_down: KeyAcquire,
        key_up: KeyRelease,
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
        self._release_attempt_backend: Literal["xtest", "xdotool"] | None = None
        self._held_buttons: set[str] = set()

    @property
    def backend_name(self) -> str:
        return self._active_backend

    def invalidate_display_generation(self) -> None:
        """Forget held buttons after the X server generation changes."""
        self._held_buttons.clear()
        self._release_attempt_backend = None

    @property
    def release_attempt_backend(self) -> Literal["xtest", "xdotool"] | None:
        return self._release_attempt_backend

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
        await self._run_xdotool_emission("xdotool", "mousemove", str(x), str(y))
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
        if button in self._held_buttons:
            raise X11InputStateConflictError("mouse button is already held")
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
                await self._run_xdotool_click(
                    button,
                    "xdotool",
                    "mousemove",
                    str(x),
                    str(y),
                    *click_args,
                )
            else:
                await self._run_xdotool_click(
                    button,
                    "xdotool",
                    "click",
                    "--repeat",
                    str(count),
                    button_number,
                )
            self._active_backend = "xdotool"
            return await self._click_state(x, y, button=button, count=count, modifiers=modifiers)
        emission_started = False
        if x is not None and y is not None:
            await self._run_xdotool_emission("xdotool", "mousemove", str(x), str(y))
            self._active_backend = "xdotool"
            emission_started = True
        acquired_modifiers: list[str] = []
        try:
            for modifier in modifier_keys:
                if await self._key_down(modifier):
                    acquired_modifiers.append(modifier)
                    emission_started = True
            await self._run_xdotool_click(
                button,
                "xdotool",
                "click",
                "--repeat",
                str(count),
                button_number,
            )
            self._active_backend = "xdotool"
        except X11InputUnavailableError as exc:
            if emission_started:
                raise X11InputInjectionError(
                    "xdotool click may have been partially applied",
                    input_backend="xdotool",
                ) from exc
            raise
        except X11InputInjectionError:
            raise
        except Exception as exc:
            raise X11InputInjectionError(
                "xdotool click may have been partially applied",
                input_backend="xdotool",
            ) from exc
        finally:
            for modifier in reversed(acquired_modifiers):
                await _shielded_cleanup(self._key_up(modifier))
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
        if button in self._held_buttons:
            raise X11InputStateConflictError("mouse button is already held")
        points = list(path or [])
        moved_to_path_start = False
        modifier_keys = _normalized_modifiers(modifiers)
        acquired_modifiers: list[str] = []
        button_acquired = False
        emission_started = False
        try:
            if points and start is None:
                start = points[0]
                await self.move(start.x, start.y)
                emission_started = True
                points = points[1:]
                moved_to_path_start = True
            if not points:
                if start is not None:
                    if not moved_to_path_start:
                        await self.move(start.x, start.y)
                        emission_started = True
                elif end is None:
                    start = await self.position()
                if end is not None:
                    points = [end]

            interval_ms = duration_ms // max(len(points), 1) if points else 0
            for modifier in modifier_keys:
                if await self._key_down(modifier):
                    acquired_modifiers.append(modifier)
                    emission_started = True
            await self.down(button)
            button_acquired = True
            emission_started = True
            for point in points:
                await self.move(point.x, point.y)
                emission_started = True
                if interval_ms > 0:
                    await asyncio.sleep(interval_ms / 1000)
        except X11InputUnavailableError as exc:
            if emission_started:
                raise X11InputInjectionError(
                    "drag may have been partially applied",
                    input_backend=exc.input_backend,
                ) from exc
            raise
        finally:
            if button_acquired:
                await _shielded_cleanup(self.up(button))
            for modifier in reversed(acquired_modifiers):
                await _shielded_cleanup(self._key_up(modifier))
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
            await self._run_xdotool_click(
                f"scroll_{direction}",
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
            await self._run_xdotool_click(
                f"scroll_{direction}",
                "xdotool",
                "click",
                "--repeat",
                str(amount),
                SCROLL_BUTTONS[direction],
            )
        self._active_backend = "xdotool"
        return await self._scroll_state(direction, amount=amount, x=x, y=y)

    async def down(
        self, button: str = "left", x: int | None = None, y: int | None = None
    ) -> ActionResult:
        if button in self._held_buttons:
            raise X11InputStateConflictError("mouse button is already held")
        try:
            native = self._try_xtest_down(int(BUTTON_NUMBERS[button]), x=x, y=y)
        except X11InputInjectionError:
            await _shielded_cleanup(self._release_interrupted_down(button, "xtest"))
            raise
        if native:
            try:
                result = await self._button_down_state(button, x=x, y=y)
                self._held_buttons.add(button)
                return result
            except BaseException:
                await _shielded_cleanup(self._release_interrupted_down(button, "xtest"))
                raise
        if x is not None and y is not None:
            command = (
                "xdotool",
                "mousemove",
                str(x),
                str(y),
                "mousedown",
                BUTTON_NUMBERS[button],
            )
        else:
            command = ("xdotool", "mousedown", BUTTON_NUMBERS[button])
        self._active_backend = "xdotool"
        try:
            await self._run_xdotool_emission(*command)
        except X11InputUnavailableError:
            raise
        except X11InputInjectionError:
            await _shielded_cleanup(self._release_interrupted_down(button, "xdotool"))
            raise
        except BaseException:
            await _shielded_cleanup(self._release_interrupted_down(button, "xdotool"))
            raise
        try:
            result = await self._button_down_state(button, x=x, y=y)
            self._held_buttons.add(button)
            return result
        except Exception as exc:
            await _shielded_cleanup(self._release_interrupted_down(button, "xdotool"))
            raise X11InputInjectionError(
                "xdotool button down may have been partially applied",
                input_backend="xdotool",
            ) from exc
        except BaseException:
            await _shielded_cleanup(self._release_interrupted_down(button, "xdotool"))
            raise

    async def _release_interrupted_down(
        self,
        button: str,
        backend: Literal["xtest", "xdotool"],
    ) -> None:
        button_number = int(BUTTON_NUMBERS[button])
        try:
            if backend == "xtest":
                if self._xtest is None:
                    await self._preserve_possible_button_ownership(button)
                    return
                self._xtest.emit((ButtonEvent(button_number, False),))
            else:
                await self._run_xdotool_release(
                    "xdotool",
                    "mouseup",
                    str(button_number),
                )
        except BaseException:
            await self._preserve_possible_button_ownership(button)
            return
        await self._button_up_state(button)
        self._held_buttons.discard(button)

    async def _preserve_possible_button_ownership(self, button: str) -> None:
        self._held_buttons.add(button)
        with contextlib.suppress(BaseException):
            await self._button_down_state(button)

    async def up(
        self, button: str = "left", x: int | None = None, y: int | None = None
    ) -> ActionResult:
        try:
            result = await self._release_button(button, x=x, y=y)
            self._held_buttons.discard(button)
            return result
        except X11InputReleaseError:
            raise
        except X11InputUnavailableError:
            raise
        except Exception as exc:
            raise X11InputReleaseError(
                "button release outcome is indeterminate",
                input_backend=self._release_attempt_backend or self._active_backend,
            ) from exc

    async def _release_button(
        self, button: str = "left", x: int | None = None, y: int | None = None
    ) -> ActionResult:
        self._release_attempt_backend = None
        if self._try_xtest_up(int(BUTTON_NUMBERS[button]), x=x, y=y):
            return await self._button_up_state(button, x=x, y=y)
        self._release_attempt_backend = "xdotool"
        if x is not None and y is not None:
            await self._run_xdotool_release(
                "xdotool",
                "mousemove",
                str(x),
                str(y),
                "mouseup",
                BUTTON_NUMBERS[button],
            )
        else:
            await self._run_xdotool_release(
                "xdotool",
                "mouseup",
                BUTTON_NUMBERS[button],
            )
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

    async def _run_xdotool_emission(
        self,
        *args: str,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return await self._run(*args)
        except X11InputUnavailableError as exc:
            raise X11InputUnavailableError(
                "xdotool is unavailable before input emission",
                input_backend="xdotool",
            ) from exc
        except (FileNotFoundError, PermissionError) as exc:
            raise X11InputUnavailableError(
                "xdotool could not start before input emission",
                input_backend="xdotool",
            ) from exc
        except X11InputInjectionError:
            raise
        except Exception as exc:
            raise X11InputInjectionError(
                "xdotool input may have been partially applied",
                input_backend="xdotool",
            ) from exc

    async def _run_xdotool_click(
        self,
        button: str,
        *args: str,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return await self._run_xdotool_emission(*args)
        except X11InputInjectionError:
            await _shielded_cleanup(self._release_interrupted_down(button, "xdotool"))
            raise

    async def _run_xdotool_release(
        self,
        *args: str,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return await self._run(*args)
        except (FileNotFoundError, PermissionError) as exc:
            raise X11InputUnavailableError(
                "xdotool could not start before input release",
                input_backend="xdotool",
            ) from exc
        except X11InputReleaseError:
            raise
        except Exception as exc:
            raise X11InputReleaseError(
                "xdotool release outcome is indeterminate",
                input_backend="xdotool",
            ) from exc

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
            if any(keycode <= 0 for keycode in modifier_keycodes):
                raise X11InputUnavailableError(
                    "one or more click modifiers are not mapped by the X server"
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
        self._release_attempt_backend = "xtest"
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


async def _shielded_cleanup(cleanup: Awaitable[object]) -> None:
    with anyio.CancelScope(shield=True):
        with contextlib.suppress(BaseException):
            await cleanup
