from __future__ import annotations

from modal_computer_use.models import DebugUrls

from .base import Namespace


class DebugNamespace(Namespace):
    def urls(self) -> DebugUrls:
        return DebugUrls.model_validate(self._client.get_json("/v1/debug/urls"))

    def vnc_url(self, refresh: bool = False) -> str | None:
        payload = self._client.get_json("/v1/debug/urls", params={"refresh": refresh})
        return DebugUrls.model_validate(payload).vnc
