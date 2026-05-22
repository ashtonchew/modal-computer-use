from __future__ import annotations

from typing import Literal

from modal_computer_use.models import Region, Screenshot

from .base import Namespace


class ScreenshotsNamespace(Namespace):
    def _payload(
        self,
        *,
        format: Literal["png", "jpeg", "webp"],
        quality: int,
        scale: float,
        show_cursor: bool,
        processing: Literal["daemon", "client", "auto"],
        storage: Literal["inline", "artifact", "auto"] = "inline",
    ) -> dict[str, object]:
        return {
            "format": format,
            "quality": quality,
            "scale": scale,
            "show_cursor": show_cursor,
            "processing": processing,
            "storage": storage,
        }

    def full(
        self,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int = 90,
        scale: float = 1.0,
        show_cursor: bool = False,
        processing: Literal["daemon", "client", "auto"] = "auto",
        storage: Literal["inline", "artifact", "auto"] = "inline",
    ) -> Screenshot:
        return Screenshot.model_validate(
            self._client.post_json(
                "/v1/screenshots/full",
                json=self._payload(
                    format=format,
                    quality=quality,
                    scale=scale,
                    show_cursor=show_cursor,
                    processing=processing,
                    storage=storage,
                ),
            )
        )

    def full_bytes(
        self,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int = 90,
        scale: float = 1.0,
        show_cursor: bool = False,
        processing: Literal["daemon", "client", "auto"] = "auto",
    ) -> bytes:
        return self._client.post_bytes(
            "/v1/screenshots/full/raw",
            json=self._payload(
                format=format,
                quality=quality,
                scale=scale,
                show_cursor=show_cursor,
                processing=processing,
            ),
        )

    def region(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int = 90,
        scale: float = 1.0,
        show_cursor: bool = False,
        processing: Literal["daemon", "client", "auto"] = "auto",
        storage: Literal["inline", "artifact", "auto"] = "inline",
    ) -> Screenshot:
        return Screenshot.model_validate(
            self._client.post_json(
                "/v1/screenshots/region",
                json=self._payload(
                    format=format,
                    quality=quality,
                    scale=scale,
                    show_cursor=show_cursor,
                    processing=processing,
                    storage=storage,
                )
                | {
                    "region": {"x": x, "y": y, "width": width, "height": height},
                },
            )
        )

    def region_bytes(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int = 90,
        scale: float = 1.0,
        show_cursor: bool = False,
        processing: Literal["daemon", "client", "auto"] = "auto",
    ) -> bytes:
        return self._client.post_bytes(
            "/v1/screenshots/region/raw",
            json=self._payload(
                format=format,
                quality=quality,
                scale=scale,
                show_cursor=show_cursor,
                processing=processing,
            )
            | {"region": {"x": x, "y": y, "width": width, "height": height}},
        )

    def zoom(
        self,
        region: Region,
        scale: float = 2.0,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int = 90,
        show_cursor: bool = True,
        storage: Literal["inline", "artifact", "auto"] = "inline",
    ) -> Screenshot:
        return Screenshot.model_validate(
            self._client.post_json(
                "/v1/screenshots/zoom",
                json={
                    "region": region.model_dump(),
                    "scale": scale,
                    "format": format,
                    "quality": quality,
                    "show_cursor": show_cursor,
                    "storage": storage,
                },
            )
        )

    def zoom_bytes(
        self,
        region: Region,
        scale: float = 2.0,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int = 90,
        show_cursor: bool = True,
    ) -> bytes:
        return self._client.post_bytes(
            "/v1/screenshots/zoom/raw",
            json={
                "region": region.model_dump(),
                "scale": scale,
                "format": format,
                "quality": quality,
                "show_cursor": show_cursor,
            },
        )

    def zoom_around(
        self,
        center: tuple[int, int],
        width: int,
        height: int,
        scale: float = 2.0,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int = 90,
        show_cursor: bool = True,
    ) -> Screenshot:
        x = max(0, center[0] - width // 2)
        y = max(0, center[1] - height // 2)
        return self.zoom(
            Region(x=x, y=y, width=width, height=height),
            scale=scale,
            format=format,
            quality=quality,
            show_cursor=show_cursor,
        )
