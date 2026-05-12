from __future__ import annotations

from modal_computer_use.models import ActionResult

from .base import Namespace


class InputNamespace(Namespace):
    def release_all(self) -> ActionResult:
        return ActionResult.model_validate(self._client.post_json("/v1/input/release-all"))
