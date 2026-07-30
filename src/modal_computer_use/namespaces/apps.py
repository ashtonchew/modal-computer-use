from __future__ import annotations

from modal_computer_use.models import ActionResult

from .base import AsyncNamespace, Namespace


class AppsNamespace(Namespace):
    def launch(self, command: str, args: list[str] | None = None) -> ActionResult:
        return ActionResult.model_validate(
            self._client.post_json("/v1/apps/launch", json={"command": command, "args": args or []})
        )

    def open_artifact(self, path: str) -> ActionResult:
        return ActionResult.model_validate(
            self._client.post_json("/v1/apps/open-artifact", json={"path": path})
        )


class AsyncAppsNamespace(AsyncNamespace):
    async def launch(self, command: str, args: list[str] | None = None) -> ActionResult:
        return ActionResult.model_validate(
            await self._client.post_json(
                "/v1/apps/launch", json={"command": command, "args": args or []}
            )
        )

    async def open_artifact(self, path: str) -> ActionResult:
        return ActionResult.model_validate(
            await self._client.post_json("/v1/apps/open-artifact", json={"path": path})
        )
