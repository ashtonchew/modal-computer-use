from __future__ import annotations

from typing import Literal

from modal_computer_use.models import ActionResult, Point

from .base import AsyncNamespace, Namespace


class MouseNamespace(Namespace):
    def click(
        self,
        x: int | None = None,
        y: int | None = None,
        button: Literal["left", "middle", "right"] = "left",
        double: bool = False,
        modifiers: list[str] | None = None,
    ) -> Point:
        payload = {"x": x, "y": y, "button": button, "double": double, "modifiers": modifiers or []}
        return Point.model_validate(
            self._client.post_json("/v1/mouse/click", json=payload, _mutation=True)
        )

    def move(self, x: int, y: int) -> Point:
        return Point.model_validate(
            self._client.post_json(
                "/v1/mouse/move", json={"x": x, "y": y}, _mutation=True
            )
        )

    def drag(
        self,
        start_x: int | None = None,
        start_y: int | None = None,
        end_x: int | None = None,
        end_y: int | None = None,
        *,
        path: list[Point] | None = None,
        button: Literal["left", "middle", "right"] = "left",
        duration_ms: int = 500,
        modifiers: list[str] | None = None,
    ) -> Point:
        payload = {
            "start_x": start_x,
            "start_y": start_y,
            "end_x": end_x,
            "end_y": end_y,
            "path": [point.model_dump() for point in path] if path else None,
            "button": button,
            "duration_ms": duration_ms,
            "modifiers": modifiers or [],
        }
        return Point.model_validate(
            self._client.post_json("/v1/mouse/drag", json=payload, _mutation=True)
        )

    def scroll(
        self,
        direction: Literal["up", "down", "left", "right"],
        amount: int = 1,
        x: int | None = None,
        y: int | None = None,
    ) -> ActionResult:
        return ActionResult.model_validate(
            self._client.post_json(
                "/v1/mouse/scroll",
                json={"direction": direction, "amount": amount, "x": x, "y": y},
                _mutation=True,
            )
        )

    def down(
        self,
        button: Literal["left", "middle", "right"] = "left",
        x: int | None = None,
        y: int | None = None,
    ) -> ActionResult:
        return ActionResult.model_validate(
            self._client.post_json(
                "/v1/mouse/down",
                json={"button": button, "x": x, "y": y},
                _mutation=True,
            )
        )

    def up(
        self,
        button: Literal["left", "middle", "right"] = "left",
        x: int | None = None,
        y: int | None = None,
    ) -> ActionResult:
        return ActionResult.model_validate(
            self._client.post_json(
                "/v1/mouse/up",
                json={"button": button, "x": x, "y": y},
                _mutation=True,
            )
        )

    def position(self) -> Point:
        return Point.model_validate(self._client.get_json("/v1/mouse/position"))


class AsyncMouseNamespace(AsyncNamespace):
    async def click(
        self,
        x: int | None = None,
        y: int | None = None,
        button: Literal["left", "middle", "right"] = "left",
        double: bool = False,
        modifiers: list[str] | None = None,
    ) -> Point:
        payload = {
            "x": x,
            "y": y,
            "button": button,
            "double": double,
            "modifiers": modifiers or [],
        }
        return Point.model_validate(
            await self._client.post_json(
                "/v1/mouse/click", json=payload, _mutation=True
            )
        )

    async def move(self, x: int, y: int) -> Point:
        return Point.model_validate(
            await self._client.post_json(
                "/v1/mouse/move", json={"x": x, "y": y}, _mutation=True
            )
        )

    async def drag(
        self,
        start_x: int | None = None,
        start_y: int | None = None,
        end_x: int | None = None,
        end_y: int | None = None,
        *,
        path: list[Point] | None = None,
        button: Literal["left", "middle", "right"] = "left",
        duration_ms: int = 500,
        modifiers: list[str] | None = None,
    ) -> Point:
        payload = {
            "start_x": start_x,
            "start_y": start_y,
            "end_x": end_x,
            "end_y": end_y,
            "path": [point.model_dump() for point in path] if path else None,
            "button": button,
            "duration_ms": duration_ms,
            "modifiers": modifiers or [],
        }
        return Point.model_validate(
            await self._client.post_json(
                "/v1/mouse/drag", json=payload, _mutation=True
            )
        )

    async def scroll(
        self,
        direction: Literal["up", "down", "left", "right"],
        amount: int = 1,
        x: int | None = None,
        y: int | None = None,
    ) -> ActionResult:
        return ActionResult.model_validate(
            await self._client.post_json(
                "/v1/mouse/scroll",
                json={"direction": direction, "amount": amount, "x": x, "y": y},
                _mutation=True,
            )
        )

    async def down(
        self,
        button: Literal["left", "middle", "right"] = "left",
        x: int | None = None,
        y: int | None = None,
    ) -> ActionResult:
        return ActionResult.model_validate(
            await self._client.post_json(
                "/v1/mouse/down",
                json={"button": button, "x": x, "y": y},
                _mutation=True,
            )
        )

    async def up(
        self,
        button: Literal["left", "middle", "right"] = "left",
        x: int | None = None,
        y: int | None = None,
    ) -> ActionResult:
        return ActionResult.model_validate(
            await self._client.post_json(
                "/v1/mouse/up",
                json={"button": button, "x": x, "y": y},
                _mutation=True,
            )
        )

    async def position(self) -> Point:
        return Point.model_validate(await self._client.get_json("/v1/mouse/position"))
