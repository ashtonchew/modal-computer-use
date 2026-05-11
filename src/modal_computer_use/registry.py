from __future__ import annotations

from collections.abc import Iterable

from .errors import ModalNotInstalledError, SandboxUnavailableError
from .models import SandboxRef


class SandboxRegistry:
    def __init__(self, app_name: str = "modal-computer-use") -> None:
        self.app_name = app_name

    def _modal(self) -> object:
        try:
            import modal
        except ImportError as exc:
            raise ModalNotInstalledError(
                "Modal registry APIs require `pip install modal-computer-use[modal]`"
            ) from exc
        return modal

    def list(self, tags: dict[str, str] | None = None) -> list[SandboxRef]:
        modal = self._modal()
        refs: list[SandboxRef] = []
        for sandbox in modal.Sandbox.list(tags=tags or {"computer-use": "true"}):
            sandbox_tags = sandbox.get_tags()
            refs.append(
                SandboxRef(
                    sandbox_id=getattr(sandbox, "object_id", "unknown"),
                    app_name=self.app_name,
                    name=getattr(sandbox, "name", None),
                    run_id=sandbox_tags.get("computer-use.run_id"),
                    config_hash=sandbox_tags.get("computer-use.config_hash"),
                    status="unknown",
                    tags=sandbox_tags,
                )
            )
        return refs

    def find_by_run_id(self, run_id: str) -> SandboxRef | None:
        matches = self.list(tags={"computer-use.run_id": run_id})
        return matches[0] if matches else None

    def require_one(self, candidates: Iterable[SandboxRef]) -> SandboxRef:
        for candidate in candidates:
            return candidate
        raise SandboxUnavailableError("no matching computer-use sandbox found")
