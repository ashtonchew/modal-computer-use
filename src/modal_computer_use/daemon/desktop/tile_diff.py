from __future__ import annotations

from collections.abc import Callable
from hashlib import blake2b
from typing import Any, Protocol


class _Hasher(Protocol):
    def update(self, data: bytes | memoryview) -> None: ...

    def digest(self) -> bytes: ...


try:
    import xxhash as _xxhash
except ImportError:  # pragma: no cover - exercised when optional wheel is unavailable.
    _xxhash = None

xxhash: Any | None = _xxhash


HashFactory = Callable[[], _Hasher]


def native_hash_available() -> bool:
    return xxhash is not None


def tile_hashes_rgb(
    rgb: bytes,
    width: int,
    height: int,
    tile_size: int,
    *,
    hash_factory: HashFactory | None = None,
) -> dict[tuple[int, int], bytes]:
    factory = hash_factory or _default_hash_factory()
    view = memoryview(rgb)
    hashes: dict[tuple[int, int], bytes] = {}
    row_stride = width * 3
    for top in range(0, height, tile_size):
        tile_height = min(tile_size, height - top)
        for left in range(0, width, tile_size):
            tile_width = min(tile_size, width - left)
            digest = factory()
            row_start = top * row_stride + left * 3
            row_bytes = tile_width * 3
            if tile_width == width:
                digest.update(view[row_start : row_start + row_stride * tile_height])
            else:
                for row in range(tile_height):
                    start = row_start + row * row_stride
                    digest.update(view[start : start + row_bytes])
            hashes[(left, top)] = digest.digest()
    return hashes


def dirty_rect_from_tiles(
    *,
    current: dict[tuple[int, int], bytes],
    previous: dict[tuple[int, int], bytes] | None,
    width: int,
    height: int,
    tile_size: int,
) -> dict[str, int] | None:
    if previous is None:
        return {"x": 0, "y": 0, "width": width, "height": height}
    left = width
    top = height
    right = 0
    bottom = 0
    changed = False
    for (tile_left, tile_top), digest in current.items():
        if previous.get((tile_left, tile_top)) == digest:
            continue
        changed = True
        left = min(left, tile_left)
        top = min(top, tile_top)
        right = max(right, min(tile_left + tile_size, width))
        bottom = max(bottom, min(tile_top + tile_size, height))
    if not changed:
        return None
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


def dirty_rects_from_tiles(
    *,
    current: dict[tuple[int, int], bytes],
    previous: dict[tuple[int, int], bytes] | None,
    width: int,
    height: int,
    tile_size: int,
    max_rects: int,
) -> list[dict[str, int]]:
    if max_rects <= 1:
        rect = dirty_rect_from_tiles(
            current=current,
            previous=previous,
            width=width,
            height=height,
            tile_size=tile_size,
        )
        return [] if rect is None else [rect]
    if previous is None:
        return [{"x": 0, "y": 0, "width": width, "height": height}]

    changed = {
        (tile_left, tile_top)
        for (tile_left, tile_top), digest in current.items()
        if previous.get((tile_left, tile_top)) != digest
    }
    rects: list[dict[str, int]] = []
    while changed:
        start = changed.pop()
        component = [start]
        stack = [start]
        while stack:
            left, top = stack.pop()
            for neighbor in (
                (left - tile_size, top),
                (left + tile_size, top),
                (left, top - tile_size),
                (left, top + tile_size),
            ):
                if neighbor not in changed:
                    continue
                changed.remove(neighbor)
                stack.append(neighbor)
                component.append(neighbor)
        rects.append(
            _rect_from_tile_component(
                component,
                width=width,
                height=height,
                tile_size=tile_size,
            )
        )
        if len(rects) > max_rects:
            rect = dirty_rect_from_tiles(
                current=current,
                previous=previous,
                width=width,
                height=height,
                tile_size=tile_size,
            )
            return [] if rect is None else [rect]
    return sorted(rects, key=lambda rect: (rect["y"], rect["x"]))


def crop_rgb(
    rgb: bytes,
    source_width: int,
    left: int,
    top: int,
    width: int,
    height: int,
) -> bytes:
    row_stride = source_width * 3
    row_bytes = width * 3
    output = bytearray(row_bytes * height)
    for row in range(height):
        source_start = (top + row) * row_stride + left * 3
        target_start = row * row_bytes
        output[target_start : target_start + row_bytes] = rgb[
            source_start : source_start + row_bytes
        ]
    return bytes(output)


def _rect_from_tile_component(
    component: list[tuple[int, int]],
    *,
    width: int,
    height: int,
    tile_size: int,
) -> dict[str, int]:
    left = min(tile_left for tile_left, _tile_top in component)
    top = min(tile_top for _tile_left, tile_top in component)
    right = max(min(tile_left + tile_size, width) for tile_left, _tile_top in component)
    bottom = max(min(tile_top + tile_size, height) for _tile_left, tile_top in component)
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


def _default_hash_factory() -> HashFactory:
    if xxhash is not None:
        return xxhash.xxh3_64
    return lambda: blake2b(digest_size=8)
