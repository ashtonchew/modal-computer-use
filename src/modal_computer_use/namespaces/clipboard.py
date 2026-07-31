from __future__ import annotations

from modal_computer_use.models import ActionResult

from .base import AsyncNamespace, Namespace


class ClipboardNamespace(Namespace):
    def get_text(self) -> str:
        payload = self._client.get_json("/v1/clipboard/text")
        return str(payload.get("text", ""))

    def set_text(self, text: str) -> ActionResult:
        return ActionResult.model_validate(
            self._client.put_json(
                "/v1/clipboard/text", json={"text": text}, _mutation=True
            )
        )

    def clear(self) -> ActionResult:
        return ActionResult.model_validate(
            self._client.delete_json("/v1/clipboard/text", _mutation=True)
        )


class AsyncClipboardNamespace(AsyncNamespace):
    async def get_text(self) -> str:
        payload = await self._client.get_json("/v1/clipboard/text")
        return str(payload.get("text", ""))

    async def set_text(self, text: str) -> ActionResult:
        return ActionResult.model_validate(
            await self._client.put_json(
                "/v1/clipboard/text", json={"text": text}, _mutation=True
            )
        )

    async def clear(self) -> ActionResult:
        return ActionResult.model_validate(
            await self._client.delete_json("/v1/clipboard/text", _mutation=True)
        )
