from __future__ import annotations

from modal_computer_use.models import ProcessStatus

from .base import Namespace


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
