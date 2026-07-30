from __future__ import annotations

from modal_computer_use.models import ActionResult

from .base import AsyncNamespace, Namespace


class InputNamespace(Namespace):
    def release_all(self) -> ActionResult:
        return ActionResult.model_validate(self._client.post_json("/v1/input/release-all"))


class AsyncInputNamespace(AsyncNamespace):
    async def release_all(self) -> ActionResult:
        return ActionResult.model_validate(await self._client.post_json("/v1/input/release-all"))
