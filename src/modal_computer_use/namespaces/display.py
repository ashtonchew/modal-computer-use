from __future__ import annotations

from modal_computer_use.models import DisplayInfo

from .base import Namespace


class DisplayNamespace(Namespace):
    def info(self) -> DisplayInfo:
        return DisplayInfo.model_validate(self._client.get_json("/v1/display/info"))
