from __future__ import annotations

import asyncio
import contextlib
import subprocess
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import anyio

from modal_computer_use.actions import KEY_ALIASES, normalize_key, normalize_key_combo
from modal_computer_use.models import ActionResult

from .xtest import (
    KeyEvent,
    X11EmissionResult,
    X11InputInjectionError,
    X11InputReleaseError,
    X11InputSession,
    X11InputStateConflictError,
    X11InputUnavailableError,
    X11KeyboardState,
)

RunCommand = Callable[..., Awaitable[subprocess.CompletedProcess[str]]]
ClipboardGet = Callable[[], Awaitable[str]]
ClipboardSet = Callable[[str], Awaitable[ActionResult]]
InputBackend = Literal["auto", "xtest", "xdotool"]
TypingMethod = Literal["auto", "keystrokes", "xdotool", "clipboard"]

_LOCK_MASK = 1 << 1
_NATIVE_TYPE_CHUNK_SIZE = 4096
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

    def keyboard_mapping(
        self,
        group: int,
        *,
        levels: int = 4,
    ) -> tuple[tuple[int, tuple[int, ...]], ...]: ...

    def keyboard_state(self) -> X11KeyboardState: ...

    def pressed_keycodes(self, keycodes: Iterable[int] | None = None) -> frozenset[int]: ...

    def emit(
        self,
        events: Sequence[KeyEvent],
        *,
        preserve_pressed_keycodes: Iterable[int] = (),
        reject_pressed_keycodes: Iterable[int] = (),
    ) -> X11EmissionResult: ...


@dataclass(frozen=True, slots=True)
class _KeyStroke:
    keycode: int
    modifiers: tuple[int, ...] = ()
    modifier_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _HeldKey:
    keycode: int | None
    owned: bool
    owned_modifiers: tuple[int, ...] = ()
    owned_modifier_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class KeyReleaseFailure:
    key: str
    input_backend: Literal["xtest", "xdotool"] | None


@dataclass(frozen=True, slots=True)
class KeyboardReleaseOutcome:
    released: tuple[str, ...]
    failures: tuple[KeyReleaseFailure, ...]


type _KeyMapping = tuple[tuple[int, tuple[int, ...]], ...]
type _KeysymMatch = tuple[int, int, tuple[int, ...]]


@dataclass(frozen=True, slots=True)
class _XkbSnapshot:
    group: int
    modifier_state: int
    mapping: _KeyMapping
    by_keysym: dict[int, _KeysymMatch]


class XkbKeymapResolver:
    """Resolve semantic keys and text against the X server's active XKB layout."""

    def __init__(self, session: NativeKeyboardSession) -> None:
        self._session = session
        self._keysym_cache: dict[str, int] = {}

    def action_key(self, key: str) -> _KeyStroke:
        return self.action_keys((key,))[0]

    def action_keys(self, keys: Sequence[str]) -> list[_KeyStroke]:
        snapshot = self._snapshot()
        return [self._action_key(key, snapshot) for key in keys]

    def _action_key(self, key: str, snapshot: _XkbSnapshot) -> _KeyStroke:
        normalized = normalize_key(key)
        if len(normalized) == 1:
            stroke = self._character(normalized, snapshot)
            if stroke is not None:
                return stroke
            raise X11InputUnavailableError(f"key is not mapped in the active XKB group: {key!r}")
        keysym = self._resolve_keysym(_KEYSYM_NAMES.get(normalized, normalized))
        if keysym <= 0:
            raise X11InputUnavailableError(f"key is not mapped by the X server: {key!r}")
        match = snapshot.by_keysym.get(keysym)
        if match is None:
            raise X11InputUnavailableError(f"key is not mapped by the X server: {key!r}")
        keycode, level, _keysyms = match
        modifiers = () if normalized in _KEYSYM_NAMES else self._level_modifiers(level, snapshot)
        return _KeyStroke(keycode, modifiers)

    def text(self, text: str) -> list[_KeyStroke] | None:
        snapshot = self._snapshot()
        strokes: list[_KeyStroke] = []
        for character in text:
            stroke = self._character(character, snapshot)
            if stroke is None:
                return None
            strokes.append(stroke)
        return strokes

    def text_chunks(
        self,
        text: str,
        *,
        chunk_size: int,
    ) -> Iterator[list[_KeyStroke]] | None:
        """Preflight the full text, then resolve it in bounded chunks."""
        snapshot = self._snapshot()
        for character in text:
            if self._character(character, snapshot) is None:
                return None

        def chunks() -> Iterator[list[_KeyStroke]]:
            for start in range(0, len(text), chunk_size):
                strokes: list[_KeyStroke] = []
                for character in text[start : start + chunk_size]:
                    stroke = self._character(character, snapshot)
                    if stroke is None:  # pragma: no cover - the snapshot was preflighted.
                        raise RuntimeError("preflighted XKB mapping changed unexpectedly")
                    strokes.append(stroke)
                yield strokes

        return chunks()

    def character(self, character: str) -> _KeyStroke | None:
        return self._character(character, self._snapshot())

    def _character(
        self,
        character: str,
        snapshot: _XkbSnapshot,
    ) -> _KeyStroke | None:
        keysym = self._resolve_keysym(_character_keysym_name(character))
        if keysym <= 0:
            return None
        match = snapshot.by_keysym.get(keysym)
        if match is None:
            return None
        keycode, matched_level, keysyms = match

        modifiers = list(self._level_modifiers(matched_level, snapshot))
        if self._caps_lock_inverts(character, keysyms, snapshot.modifier_state):
            shift = self._modifier_keycode("Shift_L", snapshot)
            if shift in modifiers:
                modifiers.remove(shift)
            else:
                modifiers.insert(0, shift)
        return _KeyStroke(
            keycode=keycode,
            modifiers=tuple(modifiers),
            modifier_names=tuple(self._modifier_name(modifier, snapshot) for modifier in modifiers),
        )

    def _snapshot(self) -> _XkbSnapshot:
        state = self._session.keyboard_state()
        mapping = self._session.keyboard_mapping(state.group)
        by_keysym: dict[int, _KeysymMatch] = {}
        for keycode, keysyms in mapping:
            for level, keysym in enumerate(keysyms):
                if keysym > 0:
                    by_keysym.setdefault(keysym, (keycode, level, keysyms))
        return _XkbSnapshot(
            group=state.group,
            modifier_state=state.modifiers,
            mapping=mapping,
            by_keysym=by_keysym,
        )

    def _level_modifiers(
        self,
        level: int,
        snapshot: _XkbSnapshot,
    ) -> tuple[int, ...]:
        if level == 0:
            return ()
        shift = self._modifier_keycode("Shift_L", snapshot)
        if level == 1:
            return (shift,)
        level_three = self._modifier_keycode("ISO_Level3_Shift", snapshot)
        if level == 2:
            return (level_three,)
        return (shift, level_three)

    def _modifier_keycode(
        self,
        keysym_name: str,
        snapshot: _XkbSnapshot,
    ) -> int:
        keysym = self._resolve_keysym(keysym_name)
        match = snapshot.by_keysym.get(keysym)
        if match is None:
            raise X11InputUnavailableError(
                f"required keyboard modifier is not mapped: {keysym_name}"
            )
        return match[0]

    def _modifier_name(self, keycode: int, snapshot: _XkbSnapshot) -> str:
        if keycode == self._modifier_keycode("Shift_L", snapshot):
            return "shift"
        if keycode == self._modifier_keycode("ISO_Level3_Shift", snapshot):
            return "ISO_Level3_Shift"
        raise X11InputUnavailableError("required keyboard modifier is not mapped")

    def _caps_lock_inverts(
        self,
        character: str,
        keysyms: Sequence[int],
        modifier_state: int,
    ) -> bool:
        if not modifier_state & _LOCK_MASK or len(keysyms) < 2:
            return False
        lower = character.lower()
        upper = character.upper()
        if lower == upper or len(lower) != 1 or len(upper) != 1:
            return False
        try:
            lower_keysym = self._resolve_keysym(_character_keysym_name(lower))
            upper_keysym = self._resolve_keysym(_character_keysym_name(upper))
        except X11InputUnavailableError:
            return False
        return {keysyms[0], keysyms[1]} == {lower_keysym, upper_keysym}

    def _resolve_keysym(self, name: str) -> int:
        cached = self._keysym_cache.get(name)
        if cached is not None:
            return cached
        keysym = self._session.resolve_keysym(name)
        if keysym > 0:
            self._keysym_cache[name] = keysym
        return keysym


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
        self._release_attempt_backend: Literal["xtest", "xdotool"] | None = None

    @property
    def backend_name(self) -> str:
        return self._active_backend

    def invalidate_display_generation(self) -> None:
        """Forget held keys after the X server generation changes."""
        self._held_keys.clear()
        self._release_attempt_backend = None

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
                    target, *modifier_strokes = self._keymap.action_keys(
                        (normalized_key, *normalized_modifiers)
                    )
                    keycodes = _deduplicate(
                        [
                            *(stroke.keycode for stroke in modifier_strokes),
                            *target.modifiers,
                            target.keycode,
                        ]
                    )
                    await self._native_chord(
                        keycodes,
                        duration_ms,
                        target_keycodes=(target.keycode,),
                    )
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
        await self.acquire(key)

    async def acquire(self, key: str) -> bool:
        """Hold a key and report whether this caller acquired temporary tracking."""

        async with self._operation_lock:
            normalized = normalize_key(key)
            if normalized in self._held_keys:
                return False
            if self._native_enabled():
                try:
                    stroke = self._keymap.action_key(normalized)
                    held = await self._native_key_down(stroke)
                except X11InputInjectionError as exc:
                    possible_hold = getattr(exc, "_possible_held_key", None)
                    if possible_hold is not None:
                        self._held_keys[normalized] = possible_hold
                    raise
                except X11InputUnavailableError:
                    if self._configured_backend != "auto":
                        raise
                    self._active_backend = "xdotool"
                    try:
                        await self._run_xdotool_emission("xdotool", "keydown", normalized)
                    except X11InputUnavailableError as exc:
                        raise X11InputUnavailableError(
                            "xdotool is unavailable before input emission",
                            input_backend="xdotool",
                        ) from exc
                    except BaseException:
                        self._held_keys[normalized] = _HeldKey(keycode=None, owned=True)
                        await _shielded_cleanup(self._up_unlocked(normalized))
                        raise
                    held = _HeldKey(keycode=None, owned=True)
            else:
                self._active_backend = "xdotool"
                try:
                    await self._run_xdotool_emission("xdotool", "keydown", normalized)
                except X11InputUnavailableError as exc:
                    raise X11InputUnavailableError(
                        "xdotool is unavailable before input emission",
                        input_backend="xdotool",
                    ) from exc
                except BaseException:
                    self._held_keys[normalized] = _HeldKey(keycode=None, owned=True)
                    await _shielded_cleanup(self._up_unlocked(normalized))
                    raise
                held = _HeldKey(keycode=None, owned=True)
            self._held_keys[normalized] = held
            try:
                await self._key_down_state(key)
            except BaseException:
                await _shielded_cleanup(self._up_unlocked(normalized))
                raise
            return True

    async def up(self, key: str) -> None:
        async with self._operation_lock:
            await self._up_unlocked(key)

    async def release_all(self, keys: Iterable[str] = ()) -> KeyboardReleaseOutcome:
        """Best-effort release and reconcile every key owned by this controller."""
        async with self._operation_lock:
            targets = {
                *(normalize_key(key) for key in keys),
                *self._held_keys,
            }
            released: list[str] = []
            failures: list[KeyReleaseFailure] = []
            for normalized in reversed(sorted(targets)):
                tracked = self._held_keys.get(normalized)
                self._release_attempt_backend = None
                try:
                    await self._up_unlocked(normalized)
                except Exception:
                    # A release is idempotent, so one direct retry is safe even when
                    # the first adapter may have delivered the event before failing.
                    try:
                        await self._retry_release_unlocked(normalized, tracked)
                        await self._key_up_state(normalized)
                    except Exception:
                        failures.append(
                            KeyReleaseFailure(
                                key=normalized,
                                input_backend=self._release_attempt_backend,
                            )
                        )
                        continue
                    self._held_keys.pop(normalized, None)
                released.append(normalized)
            return KeyboardReleaseOutcome(
                released=tuple(sorted(released)),
                failures=tuple(sorted(failures, key=lambda failure: failure.key)),
            )

    async def _up_unlocked(self, key: str) -> None:
        try:
            await self._release_key_unlocked(key)
        except X11InputReleaseError:
            raise
        except X11InputInjectionError as exc:
            raise X11InputReleaseError(
                "key release outcome is indeterminate",
                input_backend=self._release_attempt_backend or self._active_backend,
            ) from exc

    async def _release_key_unlocked(self, key: str) -> None:
        normalized = normalize_key(key)
        tracked = self._held_keys.get(normalized)
        if tracked is not None and tracked.keycode is not None:
            releases = _held_key_releases(tracked)
            if releases:
                try:
                    self._release_attempt_backend = "xtest"
                    self._xtest.emit(releases)
                    self._active_backend = "xtest"
                except X11InputUnavailableError:
                    if self._configured_backend != "auto":
                        raise
                    self._active_backend = "xdotool"
                    self._release_attempt_backend = "xdotool"
                    await self._release_held_via_xdotool(normalized, tracked)
        elif tracked is not None:
            self._active_backend = "xdotool"
            self._release_attempt_backend = "xdotool"
            await self._run_xdotool_release("xdotool", "keyup", normalized)
        elif self._native_enabled():
            try:
                stroke = self._keymap.action_key(normalized)
                self._release_attempt_backend = "xtest"
                self._xtest.emit([KeyEvent(stroke.keycode, False)])
                self._active_backend = "xtest"
            except X11InputUnavailableError:
                if self._configured_backend != "auto":
                    raise
                self._active_backend = "xdotool"
                self._release_attempt_backend = "xdotool"
                await self._run_xdotool_release("xdotool", "keyup", normalized)
        else:
            self._active_backend = "xdotool"
            self._release_attempt_backend = "xdotool"
            await self._run_xdotool_release("xdotool", "keyup", normalized)
        await self._key_up_state(key)
        self._held_keys.pop(normalized, None)

    async def _retry_release_unlocked(
        self,
        normalized: str,
        tracked: _HeldKey | None,
    ) -> None:
        if tracked is not None and tracked.keycode is not None:
            releases = _held_key_releases(tracked)
            if not releases:
                return
            try:
                self._release_attempt_backend = "xtest"
                self._xtest.emit(releases)
                self._active_backend = "xtest"
                return
            except (X11InputInjectionError, X11InputUnavailableError):
                if self._configured_backend != "auto":
                    raise
            self._active_backend = "xdotool"
            self._release_attempt_backend = "xdotool"
            await self._release_held_via_xdotool(normalized, tracked)
            return
        self._active_backend = "xdotool"
        self._release_attempt_backend = "xdotool"
        await self._run_xdotool_release("xdotool", "keyup", normalized)

    async def _release_held_via_xdotool(
        self,
        normalized: str,
        tracked: _HeldKey,
    ) -> None:
        keys = [
            *((normalized,) if tracked.owned else ()),
            *tracked.owned_modifier_names,
        ]
        for key in keys:
            await self._run_xdotool_release("xdotool", "keyup", key)

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

        if method == "auto" and len(text) > 80:
            await self._type_via_clipboard(text)
            return "clipboard"

        if self._native_enabled():
            try:
                use_chunks = method == "keystrokes" and len(text) > _NATIVE_TYPE_CHUNK_SIZE
                if use_chunks:
                    chunked_strokes = self._keymap.text_chunks(
                        text,
                        chunk_size=_NATIVE_TYPE_CHUNK_SIZE,
                    )
                    strokes = None
                else:
                    chunked_strokes = None
                    strokes = self._keymap.text(text)
            except X11InputUnavailableError:
                if self._configured_backend != "auto":
                    raise
                strokes = None
                chunked_strokes = None
                native_unavailable = True
            else:
                native_unavailable = False
            if strokes is not None or chunked_strokes is not None:
                try:
                    if chunked_strokes is not None:
                        await self._type_via_native_chunks(chunked_strokes, delay_ms)
                    else:
                        await self._type_via_native(strokes or (), delay_ms)
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

    async def _type_via_native_chunks(
        self,
        chunks: Iterable[Sequence[_KeyStroke]],
        delay_ms: int,
    ) -> None:
        emitted = False
        for strokes in chunks:
            if emitted and delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000)
            try:
                await self._type_via_native(strokes, delay_ms)
            except (X11InputInjectionError, X11InputUnavailableError) as exc:
                if emitted:
                    raise X11InputInjectionError(
                        "typing may have been partially injected"
                    ) from exc
                raise
            emitted = True

    async def _type_via_native(self, strokes: Sequence[_KeyStroke], delay_ms: int) -> None:
        if delay_ms == 0:
            events: list[KeyEvent] = []
            keycodes: list[int] = []
            target_keycodes: list[int] = []
            for stroke in strokes:
                events.extend(_stroke_events(stroke))
                keycodes.extend((*stroke.modifiers, stroke.keycode))
                target_keycodes.append(stroke.keycode)
            self._emit_taps(events, keycodes, target_keycodes)
            return
        emitted = False
        for index, stroke in enumerate(strokes):
            try:
                self._emit_taps(
                    _stroke_events(stroke),
                    (*stroke.modifiers, stroke.keycode),
                    (stroke.keycode,),
                )
            except (X11InputStateConflictError, X11InputUnavailableError) as exc:
                if emitted:
                    raise X11InputInjectionError("typing may have been partially injected") from exc
                raise
            emitted = True
            if index + 1 < len(strokes):
                await asyncio.sleep(delay_ms / 1000)

    async def _type_via_xdotool(self, text: str, delay_ms: int) -> None:
        self._active_backend = "xdotool"
        await self._run_xdotool_emission(
            "xdotool",
            "type",
            "--delay",
            str(delay_ms),
            "--file",
            "-",
            input_text=text,
        )

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
                strokes = self._keymap.action_keys(keys)
                keycodes = _deduplicate(
                    [
                        keycode
                        for stroke in strokes
                        for keycode in (*stroke.modifiers, stroke.keycode)
                    ]
                )
                await self._native_chord(
                    keycodes,
                    duration_ms,
                    target_keycodes=(strokes[-1].keycode,),
                )
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
        owned_modifier_names = [
            name
            for keycode, name in zip(
                stroke.modifiers,
                stroke.modifier_names,
                strict=True,
            )
            if keycode in owned_modifiers
        ]
        events = [
            *(KeyEvent(keycode, True) for keycode in owned_modifiers),
            KeyEvent(stroke.keycode, True),
        ]
        try:
            result = self._xtest.emit(
                events,
                preserve_pressed_keycodes=(*stroke.modifiers, stroke.keycode),
            )
            self._active_backend = "xtest"
        except X11InputInjectionError as exc:
            possible_hold = _held_key_from_emission(
                stroke.keycode,
                owned_modifiers,
                owned_modifier_names,
                exc.emission_result,
            )
            cleanup = _held_key_releases(possible_hold)
            try:
                if cleanup:
                    self._xtest.emit(cleanup)
            except Exception:
                exc._possible_held_key = possible_hold
            raise
        return _held_key_from_emission(
            stroke.keycode,
            owned_modifiers,
            owned_modifier_names,
            result,
        )

    async def _native_chord(
        self,
        keycodes: Sequence[int],
        duration_ms: int,
        *,
        target_keycodes: Iterable[int],
    ) -> None:
        targets = frozenset(target_keycodes)
        unique_keycodes = _deduplicate(keycodes)
        downs = [KeyEvent(keycode, True) for keycode in unique_keycodes]
        preserved = tuple(keycode for keycode in unique_keycodes if keycode not in targets)
        if duration_ms <= 0:
            ups = [KeyEvent(keycode, False) for keycode in reversed(unique_keycodes)]
            try:
                self._xtest.emit(
                    [*downs, *ups],
                    preserve_pressed_keycodes=preserved,
                    reject_pressed_keycodes=targets,
                )
                self._active_backend = "xtest"
            except X11InputInjectionError as exc:
                cleanup = _owned_key_releases(exc.emission_result, unique_keycodes)
                with contextlib.suppress(Exception):
                    self._xtest.emit(cleanup)
                raise
            return
        try:
            result = self._xtest.emit(
                downs,
                preserve_pressed_keycodes=preserved,
                reject_pressed_keycodes=targets,
            )
            self._active_backend = "xtest"
        except X11InputInjectionError as exc:
            cleanup = _owned_key_releases(exc.emission_result, unique_keycodes)
            with contextlib.suppress(Exception):
                self._xtest.emit(cleanup)
            raise
        ups = _owned_key_releases(result, unique_keycodes)
        try:
            await asyncio.sleep(duration_ms / 1000)
        except BaseException:
            with contextlib.suppress(Exception):
                self._xtest.emit(ups)
            raise
        self._finish_native_chord(ups)

    def _finish_native_chord(self, ups: Sequence[KeyEvent]) -> None:
        try:
            self._xtest.emit(ups)
            self._active_backend = "xtest"
        except (X11InputInjectionError, X11InputUnavailableError):
            # Downs have already been delivered. A key-up retry is idempotent, but
            # this operation must never be replayed through another adapter.
            try:
                self._xtest.emit(ups)
                self._active_backend = "xtest"
            except Exception as cleanup_exc:
                raise X11InputInjectionError(
                    "native chord release failed after key-down events were emitted"
                ) from cleanup_exc

    def _emit_taps(
        self,
        events: Sequence[KeyEvent],
        keycodes: Iterable[int],
        target_keycodes: Iterable[int],
    ) -> None:
        unique_keycodes = _deduplicate(keycodes)
        targets = frozenset(target_keycodes)
        try:
            self._xtest.emit(
                events,
                preserve_pressed_keycodes=(
                    keycode for keycode in unique_keycodes if keycode not in targets
                ),
                reject_pressed_keycodes=targets,
            )
            self._active_backend = "xtest"
        except X11InputInjectionError as exc:
            cleanup = _owned_key_releases(exc.emission_result, unique_keycodes)
            with contextlib.suppress(Exception):
                self._xtest.emit(cleanup)
            raise

    async def _xdotool_press(
        self,
        key: str,
        modifiers: Sequence[str],
        duration_ms: int,
    ) -> None:
        self._active_backend = "xdotool"
        if key in self._held_keys:
            raise X11InputStateConflictError("keyboard target key is already held")
        transient = [
            modifier for modifier in _deduplicate(modifiers) if modifier not in self._held_keys
        ]
        pending_releases: list[str] = []
        try:
            for modifier in transient:
                pending_releases.append(modifier)
                await self._run_xdotool_emission("xdotool", "keydown", modifier)
            target_registered = key not in self._held_keys and key not in pending_releases
            if target_registered:
                pending_releases.append(key)
            if duration_ms > 0:
                if key not in self._held_keys:
                    await self._run_xdotool_emission("xdotool", "keydown", key)
                    await asyncio.sleep(duration_ms / 1000)
                    await self._run_xdotool_release("xdotool", "keyup", key)
                    if target_registered:
                        pending_releases.remove(key)
                else:
                    await asyncio.sleep(duration_ms / 1000)
            else:
                if key not in self._held_keys:
                    await self._run_xdotool_emission("xdotool", "key", key)
                    if target_registered:
                        pending_releases.remove(key)
        finally:
            await self._cleanup_xdotool_keys(pending_releases)

    async def _xdotool_chord(self, keys: Sequence[str], duration_ms: int) -> None:
        self._active_backend = "xdotool"
        if keys and keys[-1] in self._held_keys:
            raise X11InputStateConflictError("keyboard target key is already held")
        if duration_ms <= 0 and not any(key in self._held_keys for key in keys):
            candidates = _deduplicate(keys)
            try:
                await self._run_xdotool_emission("xdotool", "key", "+".join(keys))
            except BaseException:
                await self._cleanup_xdotool_keys(candidates)
                raise
            return
        transient = [key for key in _deduplicate(keys) if key not in self._held_keys]
        pending_releases: list[str] = []
        try:
            for key in transient:
                pending_releases.append(key)
                await self._run_xdotool_emission("xdotool", "keydown", key)
            if duration_ms > 0:
                await asyncio.sleep(duration_ms / 1000)
        finally:
            await self._cleanup_xdotool_keys(pending_releases)

    async def _cleanup_xdotool_keys(self, keys: Sequence[str]) -> None:
        for key in reversed(keys):
            await _shielded_cleanup(self._run_xdotool_release("xdotool", "keyup", key))

    async def _run_xdotool_emission(
        self,
        *args: str,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return await self._run(*args, input_text=input_text)
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

    async def _run_xdotool_release(
        self,
        *args: str,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return await self._run(*args)
        except X11InputUnavailableError as exc:
            raise X11InputUnavailableError(
                "xdotool is unavailable before key release",
                input_backend="xdotool",
            ) from exc
        except (FileNotFoundError, PermissionError) as exc:
            raise X11InputUnavailableError(
                "xdotool could not start before key release",
                input_backend="xdotool",
            ) from exc
        except X11InputReleaseError:
            raise
        except X11InputInjectionError as exc:
            raise X11InputReleaseError(
                "xdotool key release outcome is indeterminate",
                input_backend="xdotool",
            ) from exc
        except Exception as exc:
            raise X11InputReleaseError(
                "xdotool key release may have been partially applied",
                input_backend="xdotool",
            ) from exc

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


def _owned_key_releases(
    result: X11EmissionResult | None,
    keycodes: Sequence[int],
) -> list[KeyEvent]:
    if result is None:
        return []
    return [
        KeyEvent(keycode, False)
        for keycode in reversed(keycodes)
        if keycode not in result.initially_pressed_keycodes
    ]


def _held_key_from_emission(
    target_keycode: int,
    modifier_keycodes: Sequence[int],
    modifier_names: Sequence[str],
    result: X11EmissionResult | None,
) -> _HeldKey:
    initially_pressed = result.initially_pressed_keycodes if result is not None else frozenset()
    return _HeldKey(
        keycode=target_keycode,
        owned=result is not None and target_keycode not in initially_pressed,
        owned_modifiers=(
            tuple(keycode for keycode in modifier_keycodes if keycode not in initially_pressed)
            if result is not None
            else ()
        ),
        owned_modifier_names=(
            tuple(
                name
                for keycode, name in zip(
                    modifier_keycodes,
                    modifier_names,
                    strict=True,
                )
                if keycode not in initially_pressed
            )
            if result is not None
            else ()
        ),
    )


def _held_key_releases(held: _HeldKey) -> list[KeyEvent]:
    return [
        *([KeyEvent(held.keycode, False)] if held.owned and held.keycode is not None else []),
        *(KeyEvent(keycode, False) for keycode in reversed(held.owned_modifiers)),
    ]


async def _shielded_cleanup(cleanup: Awaitable[object]) -> None:
    with anyio.CancelScope(shield=True):
        with contextlib.suppress(BaseException):
            await cleanup


__all__ = [
    "KEY_ALIASES",
    "TypingMethod",
    "X11KeyboardController",
    "XkbKeymapResolver",
    "normalize_key",
    "normalize_key_combo",
]
