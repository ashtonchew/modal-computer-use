from __future__ import annotations

from modal_computer_use.models import ActionResult, X11Window

from .base import AsyncNamespace, Namespace


class WindowsNamespace(Namespace):
    def list(self) -> list[X11Window]:
        return [X11Window.model_validate(item) for item in self._client.get_json("/v1/windows")]

    def active(self) -> X11Window | None:
        payload = self._client.get_json("/v1/windows/active")
        return X11Window.model_validate(payload) if payload else None

    def activate(self, window_id: str) -> ActionResult:
        return ActionResult.model_validate(
            self._client.post_json(
                f"/v1/windows/{window_id}/activate", _mutation=True
            )
        )

    def close(self, window_id: str) -> ActionResult:
        return ActionResult.model_validate(
            self._client.post_json(
                f"/v1/windows/{window_id}/close", _mutation=True
            )
        )

    def wait_for(
        self,
        title_regex: str | None = None,
        class_name: str | None = None,
        pid: int | None = None,
        timeout: float = 10.0,
    ) -> X11Window:
        return X11Window.model_validate(
            self._client.post_json(
                "/v1/windows/wait-for",
                json={
                    "title_regex": title_regex,
                    "class_name": class_name,
                    "pid": pid,
                    "timeout": timeout,
                },
            )
        )


class AsyncWindowsNamespace(AsyncNamespace):
    async def list(self) -> list[X11Window]:
        return [
            X11Window.model_validate(item) for item in await self._client.get_json("/v1/windows")
        ]

    async def active(self) -> X11Window | None:
        payload = await self._client.get_json("/v1/windows/active")
        return X11Window.model_validate(payload) if payload else None

    async def activate(self, window_id: str) -> ActionResult:
        return ActionResult.model_validate(
            await self._client.post_json(
                f"/v1/windows/{window_id}/activate", _mutation=True
            )
        )

    async def close(self, window_id: str) -> ActionResult:
        return ActionResult.model_validate(
            await self._client.post_json(
                f"/v1/windows/{window_id}/close", _mutation=True
            )
        )

    async def wait_for(
        self,
        title_regex: str | None = None,
        class_name: str | None = None,
        pid: int | None = None,
        timeout: float = 10.0,
    ) -> X11Window:
        return X11Window.model_validate(
            await self._client.post_json(
                "/v1/windows/wait-for",
                json={
                    "title_regex": title_regex,
                    "class_name": class_name,
                    "pid": pid,
                    "timeout": timeout,
                },
            )
        )
