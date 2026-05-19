from __future__ import annotations

from modal_computer_use.models import SandboxRef

from .base import Namespace


class SessionNamespace(Namespace):
    def metadata(self) -> SandboxRef:
        return SandboxRef.model_validate(self._client.get_json("/v1/session/metadata"))

    def refresh(self) -> SandboxRef:
        return SandboxRef.model_validate(self._client.post_json("/v1/session/refresh"))

    def tunnel_authorize(self) -> dict[str, object]:
        return self._client.post_json("/v1/session/tunnel-authorize")
