from __future__ import annotations

from modal_computer_use.models import ComputerStatus, LifecycleResult

from .base import AsyncNamespace, Namespace


class LifecycleNamespace(Namespace):
    def start(self) -> LifecycleResult:
        return LifecycleResult.model_validate(self._client.post_json("/v1/computer/start"))

    def stop(self) -> LifecycleResult:
        return LifecycleResult.model_validate(self._client.post_json("/v1/computer/stop"))

    def restart(self) -> LifecycleResult:
        return LifecycleResult.model_validate(self._client.post_json("/v1/computer/restart"))

    def status(self) -> ComputerStatus:
        return ComputerStatus.model_validate(self._client.get_json("/v1/computer/status"))


class AsyncLifecycleNamespace(AsyncNamespace):
    async def start(self) -> LifecycleResult:
        return LifecycleResult.model_validate(await self._client.post_json("/v1/computer/start"))

    async def stop(self) -> LifecycleResult:
        return LifecycleResult.model_validate(await self._client.post_json("/v1/computer/stop"))

    async def restart(self) -> LifecycleResult:
        return LifecycleResult.model_validate(await self._client.post_json("/v1/computer/restart"))

    async def status(self) -> ComputerStatus:
        return ComputerStatus.model_validate(await self._client.get_json("/v1/computer/status"))
