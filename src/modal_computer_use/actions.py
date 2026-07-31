from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .models import ComputerAction, parse_action

KEY_ALIASES: dict[str, str] = {
    "alt": "alt",
    "option": "alt",
    "backspace": "BackSpace",
    "bksp": "BackSpace",
    "cmd": "super",
    "command": "super",
    "ctrl": "ctrl",
    "control": "ctrl",
    "delete": "Delete",
    "del": "Delete",
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "home": "Home",
    "meta": "super",
    "end": "End",
    "left": "Left",
    "right": "Right",
    "up": "Up",
    "down": "Down",
    "pageup": "Page_Up",
    "page_up": "Page_Up",
    "pagedown": "Page_Down",
    "page_down": "Page_Down",
    "shift": "shift",
    "space": "space",
    "super": "super",
    "tab": "Tab",
    "arrow_down": "Down",
    "arrowdown": "Down",
    "arrow_left": "Left",
    "arrowleft": "Left",
    "arrow_right": "Right",
    "arrowright": "Right",
    "arrow_up": "Up",
    "arrowup": "Up",
}


def normalize_key(key: str) -> str:
    stripped = key.strip()
    if not stripped:
        raise ValueError("key must be non-empty")
    lowered = stripped.lower().replace("-", "_")
    if lowered in KEY_ALIASES:
        return KEY_ALIASES[lowered]
    if len(stripped) == 1:
        return stripped
    if lowered.startswith("f") and lowered[1:].isdigit():
        number = int(lowered[1:])
        if 1 <= number <= 24:
            return f"F{number}"
    return stripped


def is_supported_key(key: str) -> bool:
    stripped = key.strip()
    if not stripped:
        return False
    lowered = stripped.lower().replace("-", "_")
    if lowered in KEY_ALIASES:
        return True
    if stripped.lower() in {value.lower() for value in KEY_ALIASES.values()}:
        return True
    if len(stripped) == 1:
        return True
    if lowered.startswith("f") and lowered[1:].isdigit():
        number = int(lowered[1:])
        return 1 <= number <= 24
    return False


def normalize_key_combo(keys: str | Iterable[str]) -> list[str]:
    if isinstance(keys, str):
        parts = [part for part in keys.replace("+", " ").split() if part]
    else:
        parts = list(keys)
    if not parts:
        raise ValueError("key combo must contain at least one key")
    return [normalize_key(part) for part in parts]


def normalize_actions(actions: Iterable[ComputerAction | dict[str, Any]]) -> list[ComputerAction]:
    return [parse_action(action) for action in actions]
