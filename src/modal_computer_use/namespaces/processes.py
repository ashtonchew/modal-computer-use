from __future__ import annotations

from modal_computer_use.models import ProcessStatus

from .base import AsyncNamespace, Namespace


class ProcessesNamespace(Namespace):
    def status(self, name: str) -> ProcessStatus:
        return ProcessStatus.model_validate(self._client.get_json(f"/v1/processes/{name}/status"))

    def restart(self, name: str) -> ProcessStatus:
        return ProcessStatus.model_validate(self._client.post_json(f"/v1/processes/{name}/restart"))

    def logs(self, name: str, tail: int = 200) -> str:
        return self._client.get_bytes(f"/v1/processes/{name}/logs", params={"tail": tail}).decode()

    def stderr(self, name: str, tail: int = 200) -> str:
        return self._client.get_bytes(
            f"/v1/processes/{name}/stderr", params={"tail": tail}
        ).decode()

    def errors(self, name: str, tail: int = 200) -> str:
        return self.stderr(name, tail=tail)


class AsyncProcessesNamespace(AsyncNamespace):
    async def status(self, name: str) -> ProcessStatus:
        return ProcessStatus.model_validate(
            await self._client.get_json(f"/v1/processes/{name}/status")
        )

    async def restart(self, name: str) -> ProcessStatus:
        return ProcessStatus.model_validate(
            await self._client.post_json(f"/v1/processes/{name}/restart")
        )

    async def logs(self, name: str, tail: int = 200) -> str:
        return (
            await self._client.get_bytes(f"/v1/processes/{name}/logs", params={"tail": tail})
        ).decode()

    async def stderr(self, name: str, tail: int = 200) -> str:
        return (
            await self._client.get_bytes(f"/v1/processes/{name}/stderr", params={"tail": tail})
        ).decode()

    async def errors(self, name: str, tail: int = 200) -> str:
        return await self.stderr(name, tail=tail)
