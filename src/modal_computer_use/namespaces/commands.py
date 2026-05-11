from __future__ import annotations

from modal_computer_use.models import ActionResult

from .base import Namespace


class CommandsNamespace(Namespace):
    def run(self, *command: str, timeout: float = 30.0) -> ActionResult:
        if not command:
            raise ValueError("command must contain at least one argument")
        return ActionResult.model_validate(
            self._client.post_json(
                "/v1/commands/run",
                json={"command": list(command), "timeout": timeout},
            )
        )
