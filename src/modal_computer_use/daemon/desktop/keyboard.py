from __future__ import annotations

import asyncio
import contextlib
import subprocess
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from modal_computer_use.actions import KEY_ALIASES, normalize_key, normalize_key_combo
from modal_computer_use.models import ActionResult

from .xtest import (
    KeyEvent,
    X11InputInjectionError,
    X11InputSession,
    X11InputUnavailableError,
)

RunCommand = Callable[..., Awaitable[subprocess.CompletedProcess[str]]]
ClipboardGet = Callable[[], Awaitable[str]]
ClipboardSet = Callable[[str], Awaitable[ActionResult]]
InputBackend = Literal["auto", "xtest", "xdotool"]
TypingMethod = Literal["auto", "keystrokes", "xdotool", "clipboard"]

_LOCK_MASK = 1 << 1
_KEYSYM_NAMES = {
    "alt": "Alt_L",
    "BackSpace": "BackSpace",
    "ctrl": "Control_L",
    "Delete": "Delete",
    "Down": "Down",
    "Escape": "Escape",
    "Left": "Left",
    "Page_Down": "Next",
    "Page_Up": "Prior",
    "Return": "Return",
    "Right": "Right",
    "shift": "Shift_L",
    "space": "space",
    "super": "Super_L",
    "Tab": "Tab",
    "Up": "Up",
}
_CHARACTER_KEYSYM_NAMES = {
    "\t": "Tab",
    "\n": "Return",
    "\r": "Return",
    " ": "space",
    "!": "exclam",
    '"': "quotedbl",
    "#": "numbersign",
    "$": "dollar",
    "%": "percent",
    "&": "ampersand",
    "'": "apostrophe",
    "(": "parenleft",
    ")": "parenright",
    "*": "asterisk",
    "+": "plus",
    ",": "comma",
    "-": "minus",
    ".": "period",
    "/": "slash",
    ":": "colon",
    ";": "semicolon",
    "<": "less",
    "=": "equal",
    ">": "greater",
    "?": "question",
    "@": "at",
    "[": "bracketleft",
    "\\": "backslash",
    "]": "bracketright",
    "^": "asciicircum",
    "_": "underscore",
    "`": "grave",
    "{": "braceleft",
    "|": "bar",
    "}": "braceright",
    "~": "asciitilde",
}


class NativeKeyboardSession(Protocol):
    @property
    def failure(self) -> str | None: ...

    def available(self) -> bool: ...

    def resolve_keysym(self, name: str) -> int: ...

    def keysym_to_keycode(self, keysym: int) -> int: ...

    def resolve_keycode(self, name: str) -> int: ...

    def keycode_to_keysym(self, keycode: int, group: int, level: int) -> int: ...

    def keyboard_group(self) -> int: ...

    def modifier_state(self) -> int: ...

    def pressed_keycodes(self, keycodes: Iterable[int] | None = None) -> frozenset[int]: ...

    def emit(
        self,
        events: Sequence[KeyEvent],
        *,
        preserve_pressed_keycodes: Iterable[int] = (),
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _KeyStroke:
    keycode: int
    modifiers: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class _HeldKey:
    keycode: int | None
    owned: bool
    owned_modifiers: tuple[int, ...] = ()


class XkbKeymapResolver:
    """Resolve semantic keys and text against the X server's active XKB layout."""

    def __init__(self, session: NativeKeyboardSession) -> None:
        self._session = session

    def action_key(self, key: str) -> _KeyStroke:
        normalized = normalize_key(key)
        if len(normalized) == 1:
            stroke = self.character(normalized)
            if stroke is not None:
                return stroke
            raise X11InputUnavailableError(
                f"key is not mapped in the active XKB group: {key!r}"
            )
        keycode = self._session.resolve_keycode(_KEYSYM_NAMES.get(normalized, normalized))
        if keycode <= 0:
            raise X11InputUnavailableError(f"key is not mapped by the X server: {key!r}")
        return _KeyStroke(keycode)

    def text(self, text: str) -> list[_KeyStroke] | None:
        strokes: list[_KeyStroke] = []
        for character in text:
            stroke = self.character(character)
            if stroke is None:
                return None
            strokes.append(stroke)
        return strokes

    def character(self, character: str) -> _KeyStroke | None:
        keysym = self._session.resolve_keysym(_character_keysym_name(character))
        if keysym <= 0:
            return None
        keycode = self._session.keysym_to_keycode(keysym)
        if keycode <= 0:
            return None

        group = self._session.keyboard_group()
        matched_level = next(
            (
                level
                for level in range(4)
                if self._session.keycode_to_keysym(keycode, group, level) == keysym
            ),
            None,
        )
        if matched_level is None:
            return None

        modifiers = list(self._level_modifiers(matched_level))
        if self._caps_lock_inverts(character, keycode, group):
            shift = self._modifier_keycode("Shift_L")
            if shift in modifiers:
                modifiers.remove(shift)
            else:
                modifiers.insert(0, shift)
        return _KeyStroke(keycode=keycode, modifiers=tuple(modifiers))

    def _level_modifiers(self, level: int) -> tuple[int, ...]:
        if level == 0:
            return ()
        shift = self._modifier_keycode("Shift_L")
        if level == 1:
            return (shift,)
        level_three = self._modifier_keycode("ISO_Level3_Shift")
        if level == 2:
            return (level_three,)
        return (shift, level_three)

    def _modifier_keycode(self, keysym_name: str) -> int:
        keycode = self._session.resolve_keycode(keysym_name)
        if keycode <= 0:
            raise X11InputUnavailableError(
                f"required keyboard modifier is not mapped: {keysym_name}"
            )
        return keycode

    def _caps_lock_inverts(self, character: str, keycode: int, group: int) -> bool:
        if not self._session.modifier_state() & _LOCK_MASK:
            return False
        lower = character.lower()
        upper = character.upper()
        if lower == upper or len(lower) != 1 or len(upper) != 1:
            return False
        try:
            lower_keysym = self._session.resolve_keysym(_character_keysym_name(lower))
            upper_keysym = self._session.resolve_keysym(_character_keysym_name(upper))
        except X11InputUnavailableError:
            return False
        level_zero = self._session.keycode_to_keysym(keycode, group, 0)
        level_one = self._session.keycode_to_keysym(keycode, group, 1)
        return {level_zero, level_one} == {lower_keysym, upper_keysym}


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
        input_backend: InputBackend = "auto",
        xtest: NativeKeyboardSession | None = None,
    ) -> None:
        self._run = run
        self._type_state = type_state
        self._press_state = press_state
        self._hotkey_state = hotkey_state
        self._key_down_state = key_down_state
        self._key_up_state = key_up_state
        self._clipboard_get = clipboard_get
        self._clipboard_set = clipboard_set
        self._configured_backend = input_backend
        self._xtest = xtest or X11InputSession(display=":99")
        self._keymap = XkbKeymapResolver(self._xtest)
        self._held_keys: dict[str, _HeldKey] = {}
        self._operation_lock = asyncio.Lock()
        self._active_backend = "xdotool"

    @property
    def backend_name(self) -> str:
        return self._active_backend

    async def type_text(
        self, text: str, delay_ms: int = 10, method: TypingMethod = "auto"
    ) -> ActionResult:
        async with self._operation_lock:
            selected_method = await self._type_text_unlocked(text, delay_ms, method)
            return await self._type_state(
                text,
                delay_ms=delay_ms,
                method=selected_method,
            )

    async def press(
        self, key: str, modifiers: Sequence[str] = (), duration_ms: int = 0
    ) -> ActionResult:
        async with self._operation_lock:
            normalized_key = normalize_key(key)
            normalized_modifiers = [normalize_key(modifier) for modifier in modifiers]
            if self._native_enabled():
                try:
                    target = self._keymap.action_key(normalized_key)
                    modifier_strokes = [
                        self._keymap.action_key(modifier) for modifier in normalized_modifiers
                    ]
                    keycodes = _deduplicate(
                        [
                            *(stroke.keycode for stroke in modifier_strokes),
                            *target.modifiers,
                            target.keycode,
                        ]
                    )
                    await self._native_chord(keycodes, duration_ms)
                except X11InputUnavailableError:
                    if self._configured_backend != "auto":
                        raise
                    await self._xdotool_press(
                        normalized_key,
                        normalized_modifiers,
                        duration_ms,
                    )
            else:
                await self._xdotool_press(
                    normalized_key,
                    normalized_modifiers,
                    duration_ms,
                )
            return await self._press_state(
                key,
                modifiers=modifiers,
                duration_ms=duration_ms,
            )

    async def hotkey(self, keys: Sequence[str], duration_ms: int = 0) -> ActionResult:
        async with self._operation_lock:
            normalized = normalize_key_combo(keys)
            await self._hotkey_unlocked(normalized, duration_ms=duration_ms)
            return await self._hotkey_state(keys, duration_ms=duration_ms)

    async def down(self, key: str) -> None:
        async with self._operation_lock:
            normalized = normalize_key(key)
            if normalized in self._held_keys:
                return
            if self._native_enabled():
                try:
                    stroke = self._keymap.action_key(normalized)
                    held = await self._native_key_down(stroke)
                except X11InputUnavailableError:
                    if self._configured_backend != "auto":
                        raise
                    self._active_backend = "xdotool"
                    await self._run("xdotool", "keydown", normalized)
                    held = _HeldKey(keycode=None, owned=True)
            else:
                self._active_backend = "xdotool"
                await self._run("xdotool", "keydown", normalized)
                held = _HeldKey(keycode=None, owned=True)
            self._held_keys[normalized] = held
            await self._key_down_state(key)

    async def up(self, key: str) -> None:
        async with self._operation_lock:
            await self._up_unlocked(key)

    async def release_all(self, keys: Iterable[str] = ()) -> None:
        """Best-effort release and reconcile every key owned by this controller."""
        async with self._operation_lock:
            targets = {
                *(normalize_key(key) for key in keys),
                *self._held_keys,
            }
            for normalized in reversed(sorted(targets)):
                tracked = self._held_keys.get(normalized)
                try:
                    await self._up_unlocked(normalized)
                except Exception:
                    # A release is idempotent, so one direct retry is safe even when
                    # the first adapter may have delivered the event before failing.
                    with contextlib.suppress(Exception):
                        await self._retry_release_unlocked(normalized, tracked)
                    self._held_keys.pop(normalized, None)
                    with contextlib.suppress(Exception):
                        await self._key_up_state(normalized)

    async def _up_unlocked(self, key: str) -> None:
        normalized = normalize_key(key)
        tracked = self._held_keys.get(normalized)
        if tracked is not None and tracked.keycode is not None:
            if tracked.owned:
                try:
                    self._xtest.emit(
                        [
                            KeyEvent(tracked.keycode, False),
                            *(
                                KeyEvent(keycode, False)
                                for keycode in reversed(tracked.owned_modifiers)
                            ),
                        ]
                    )
                    self._active_backend = "xtest"
                except X11InputUnavailableError:
                    if self._configured_backend != "auto":
                        raise
                    self._active_backend = "xdotool"
                    await self._run("xdotool", "keyup", normalized)
        elif tracked is not None:
            self._active_backend = "xdotool"
            await self._run("xdotool", "keyup", normalized)
        elif self._native_enabled():
            try:
                stroke = self._keymap.action_key(normalized)
                self._xtest.emit([KeyEvent(stroke.keycode, False)])
                self._active_backend = "xtest"
            except X11InputUnavailableError:
                if self._configured_backend != "auto":
                    raise
                self._active_backend = "xdotool"
                await self._run("xdotool", "keyup", normalized)
        else:
            self._active_backend = "xdotool"
            await self._run("xdotool", "keyup", normalized)
        self._held_keys.pop(normalized, None)
        await self._key_up_state(key)

    async def _retry_release_unlocked(
        self,
        normalized: str,
        tracked: _HeldKey | None,
    ) -> None:
        if tracked is not None and not tracked.owned:
            return
        if tracked is not None and tracked.keycode is not None:
            try:
                self._xtest.emit(
                    [
                        KeyEvent(tracked.keycode, False),
                        *(
                            KeyEvent(keycode, False)
                            for keycode in reversed(tracked.owned_modifiers)
                        ),
                    ]
                )
                self._active_backend = "xtest"
                return
            except (X11InputInjectionError, X11InputUnavailableError):
                if self._configured_backend != "auto":
                    raise
        self._active_backend = "xdotool"
        await self._run("xdotool", "keyup", normalized)

    async def _type_text_unlocked(
        self,
        text: str,
        delay_ms: int,
        method: TypingMethod,
    ) -> Literal["keystrokes", "xdotool", "clipboard"]:
        if method == "clipboard":
            await self._type_via_clipboard(text)
            return "clipboard"
        if method == "xdotool":
            await self._type_via_xdotool(text, delay_ms)
            return "xdotool"

        if self._native_enabled():
            try:
                strokes = self._keymap.text(text)
            except X11InputUnavailableError:
                if self._configured_backend != "auto":
                    raise
                strokes = None
                native_unavailable = True
            else:
                native_unavailable = False
            if strokes is not None:
                if method == "auto" and len(text) > 80:
                    await self._type_via_clipboard(text)
                    return "clipboard"
                try:
                    await self._type_via_native(strokes, delay_ms)
                except X11InputUnavailableError:
                    if self._configured_backend != "auto":
                        raise
                    native_unavailable = True
                else:
                    return "keystrokes"
            if method == "keystrokes" and not native_unavailable:
                raise ValueError("text contains characters not mapped by the active XKB layout")
        elif method == "keystrokes" and self._configured_backend == "xtest":
            raise X11InputUnavailableError(self._xtest.failure or "XTest unavailable")

        if method == "auto" and (len(text) > 80 or not text.isascii()):
            await self._type_via_clipboard(text)
            return "clipboard"
        await self._type_via_xdotool(text, delay_ms)
        return "xdotool"

    async def _type_via_native(self, strokes: Sequence[_KeyStroke], delay_ms: int) -> None:
        if delay_ms == 0:
            events: list[KeyEvent] = []
            keycodes: list[int] = []
            for stroke in strokes:
                events.extend(_stroke_events(stroke))
                keycodes.extend((*stroke.modifiers, stroke.keycode))
            self._emit_taps(events, keycodes)
            return
        emitted = False
        for index, stroke in enumerate(strokes):
            try:
                self._emit_taps(
                    _stroke_events(stroke),
                    (*stroke.modifiers, stroke.keycode),
                )
            except X11InputUnavailableError as exc:
                if emitted:
                    raise X11InputInjectionError(
                        "typing may have been partially injected"
                    ) from exc
                raise
            emitted = True
            if index + 1 < len(strokes):
                await asyncio.sleep(delay_ms / 1000)

    async def _type_via_xdotool(self, text: str, delay_ms: int) -> None:
        self._active_backend = "xdotool"
        await self._run("xdotool", "type", "--delay", str(delay_ms), text)

    async def _type_via_clipboard(self, text: str) -> None:
        previous = await self._clipboard_get()
        try:
            await self._clipboard_set(text)
            await self._hotkey_unlocked(["ctrl", "v"], duration_ms=0)
        finally:
            await self._clipboard_set(previous)

    async def _hotkey_unlocked(self, keys: Sequence[str], *, duration_ms: int) -> None:
        if self._native_enabled():
            try:
                strokes = [self._keymap.action_key(key) for key in keys]
                keycodes = _deduplicate(
                    [
                        keycode
                        for stroke in strokes
                        for keycode in (*stroke.modifiers, stroke.keycode)
                    ]
                )
                await self._native_chord(keycodes, duration_ms)
            except X11InputUnavailableError:
                if self._configured_backend != "auto":
                    raise
            else:
                return
        await self._xdotool_chord(keys, duration_ms)

    async def _native_key_down(self, stroke: _KeyStroke) -> _HeldKey:
        pressed = self._xtest.pressed_keycodes((*stroke.modifiers, stroke.keycode))
        target_owned = stroke.keycode not in pressed
        if not target_owned:
            self._active_backend = "xtest"
            return _HeldKey(keycode=stroke.keycode, owned=False)
        owned_modifiers = [keycode for keycode in stroke.modifiers if keycode not in pressed]
        events = [
            *(KeyEvent(keycode, True) for keycode in owned_modifiers),
            KeyEvent(stroke.keycode, True),
        ]
        try:
            self._xtest.emit(
                events,
                preserve_pressed_keycodes=(*stroke.modifiers, stroke.keycode),
            )
            self._active_backend = "xtest"
        except X11InputInjectionError:
            cleanup = [
                KeyEvent(stroke.keycode, False),
                *(KeyEvent(keycode, False) for keycode in reversed(owned_modifiers)),
            ]
            with contextlib.suppress(Exception):
                self._xtest.emit(cleanup)
            raise
        return _HeldKey(
            keycode=stroke.keycode,
            owned=True,
            owned_modifiers=tuple(owned_modifiers),
        )

    async def _native_chord(self, keycodes: Sequence[int], duration_ms: int) -> None:
        pressed = self._xtest.pressed_keycodes(keycodes)
        owned = [keycode for keycode in keycodes if keycode not in pressed]
        if not owned:
            self._active_backend = "xtest"
            return
        downs = [KeyEvent(keycode, True) for keycode in owned]
        ups = [KeyEvent(keycode, False) for keycode in reversed(owned)]
        if duration_ms <= 0:
            try:
                self._xtest.emit(
                    [*downs, *ups],
                    preserve_pressed_keycodes=keycodes,
                )
                self._active_backend = "xtest"
            except X11InputInjectionError:
                with contextlib.suppress(Exception):
                    self._xtest.emit(ups)
                raise
            return
        try:
            self._xtest.emit(downs, preserve_pressed_keycodes=keycodes)
            self._active_backend = "xtest"
            await asyncio.sleep(duration_ms / 1000)
        except BaseException:
            with contextlib.suppress(Exception):
                self._xtest.emit(ups)
            raise
        else:
            self._xtest.emit(ups)

    def _emit_taps(self, events: Sequence[KeyEvent], keycodes: Iterable[int]) -> None:
        unique_keycodes = _deduplicate(keycodes)
        initially_pressed = self._xtest.pressed_keycodes(unique_keycodes)
        owned = [keycode for keycode in unique_keycodes if keycode not in initially_pressed]
        try:
            self._xtest.emit(
                events,
                preserve_pressed_keycodes=unique_keycodes,
            )
            self._active_backend = "xtest"
        except X11InputInjectionError:
            with contextlib.suppress(Exception):
                self._xtest.emit(
                    [KeyEvent(keycode, False) for keycode in reversed(owned)]
                )
            raise

    async def _xdotool_press(
        self,
        key: str,
        modifiers: Sequence[str],
        duration_ms: int,
    ) -> None:
        self._active_backend = "xdotool"
        transient = [
            modifier for modifier in _deduplicate(modifiers) if modifier not in self._held_keys
        ]
        for modifier in transient:
            await self._run("xdotool", "keydown", modifier)
        try:
            if duration_ms > 0:
                await self._run("xdotool", "keydown", key)
                try:
                    await asyncio.sleep(duration_ms / 1000)
                finally:
                    await self._run("xdotool", "keyup", key)
            else:
                await self._run("xdotool", "key", key)
        finally:
            for modifier in reversed(transient):
                with contextlib.suppress(Exception):
                    await self._run("xdotool", "keyup", modifier)

    async def _xdotool_chord(self, keys: Sequence[str], duration_ms: int) -> None:
        self._active_backend = "xdotool"
        if duration_ms <= 0 and not any(key in self._held_keys for key in keys):
            await self._run("xdotool", "key", "+".join(keys))
            return
        transient = [key for key in _deduplicate(keys) if key not in self._held_keys]
        pressed: list[str] = []
        try:
            for key in transient:
                await self._run("xdotool", "keydown", key)
                pressed.append(key)
            if duration_ms > 0:
                await asyncio.sleep(duration_ms / 1000)
        finally:
            for key in reversed(pressed):
                with contextlib.suppress(Exception):
                    await self._run("xdotool", "keyup", key)

    def _native_enabled(self) -> bool:
        if self._configured_backend == "xdotool":
            return False
        if self._xtest.available():
            return True
        if self._configured_backend == "xtest":
            raise X11InputUnavailableError(self._xtest.failure or "XTest unavailable")
        return False


def _stroke_events(stroke: _KeyStroke) -> list[KeyEvent]:
    return [
        *(KeyEvent(keycode, True) for keycode in stroke.modifiers),
        KeyEvent(stroke.keycode, True),
        KeyEvent(stroke.keycode, False),
        *(KeyEvent(keycode, False) for keycode in reversed(stroke.modifiers)),
    ]


def _character_keysym_name(character: str) -> str:
    named = _CHARACTER_KEYSYM_NAMES.get(character)
    if named is not None:
        return named
    return character if character.isascii() else f"U{ord(character):04X}"


def _deduplicate[T](values: Iterable[T]) -> list[T]:
    return list(dict.fromkeys(values))


__all__ = [
    "KEY_ALIASES",
    "TypingMethod",
    "X11KeyboardController",
    "XkbKeymapResolver",
    "normalize_key",
    "normalize_key_combo",
]
