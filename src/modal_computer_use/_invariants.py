from __future__ import annotations

from collections.abc import Sized


def require_coordinate_pair(
    first: object | None,
    second: object | None,
    *,
    message: str = "x and y must be supplied together",
) -> None:
    if (first is None) != (second is None):
        raise ValueError(message)


def require_safe_text(value: str) -> str:
    for char in value:
        code = ord(char)
        if code < 32 and char not in ("\n", "\r"):
            raise ValueError("control characters are not allowed; use keypress/hotkey")
    return value


def require_drag_shape(
    *,
    start_x: int | None,
    start_y: int | None,
    end_x: int | None,
    end_y: int | None,
    path: Sized | None,
    coordinate_message: str,
    start_coordinate_message: str | None = None,
    end_coordinate_message: str | None = None,
) -> None:
    require_coordinate_pair(
        start_x,
        start_y,
        message=start_coordinate_message or coordinate_message,
    )
    require_coordinate_pair(
        end_x,
        end_y,
        message=end_coordinate_message or coordinate_message,
    )
    if path is not None and len(path) < 2:
        raise ValueError("drag path must contain at least two points")
    if path is None and end_x is None and end_y is None:
        raise ValueError("drag requires path or end coordinates")
