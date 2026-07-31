from __future__ import annotations

from typing import Any

from modal_computer_use.models import ActionResult

from .base import AsyncNamespace, Namespace


class BrowserNamespace(Namespace):
    def open_url(self, url: str, wait_for_window: bool = True) -> ActionResult:
        return ActionResult.model_validate(
            self._client.post_json(
                "/v1/browser/open-url",
                json={"url": url, "wait_for_window": wait_for_window},
                _mutation=True,
            )
        )

    def status(self) -> dict[str, Any]:
        return dict(self._client.get_json("/v1/browser/status"))

    def render_metrics(self, url: str, timeout_seconds: float = 30.0) -> dict[str, Any]:
        return dict(
            self._client.post_json(
                "/v1/browser/render-metrics",
                json={"url": url, "timeout_seconds": timeout_seconds},
                _mutation=True,
            )
        )


class AsyncBrowserNamespace(AsyncNamespace):
    async def open_url(self, url: str, wait_for_window: bool = True) -> ActionResult:
        return ActionResult.model_validate(
            await self._client.post_json(
                "/v1/browser/open-url",
                json={"url": url, "wait_for_window": wait_for_window},
                _mutation=True,
            )
        )

    async def status(self) -> dict[str, Any]:
        return dict(await self._client.get_json("/v1/browser/status"))

    async def render_metrics(
        self, url: str, timeout_seconds: float = 30.0
    ) -> dict[str, Any]:
        return dict(
            await self._client.post_json(
                "/v1/browser/render-metrics",
                json={"url": url, "timeout_seconds": timeout_seconds},
                _mutation=True,
            )
        )
