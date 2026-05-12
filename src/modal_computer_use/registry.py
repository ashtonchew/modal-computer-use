from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from .errors import ModalNotInstalledError, SandboxAmbiguousError, SandboxUnavailableError
from .models import SandboxRef


class SandboxRegistry:
    def __init__(self, app_name: str = "modal-computer-use") -> None:
        self.app_name = app_name

    def _modal(self) -> object:
        try:
            import modal
        except ImportError as exc:
            raise ModalNotInstalledError(
                "Modal registry APIs require the modal extra, for example "
                "`uv sync --extra modal` in this repository or "
                "`uv add 'modal-computer-use[modal]'` downstream"
            ) from exc
        return modal

    def list(self, tags: dict[str, str] | None = None) -> list[SandboxRef]:
        return [self.ref_from_sandbox(sandbox) for sandbox in self.list_sandboxes(tags=tags)]

    def list_by_owner(self, owner: str) -> list[SandboxRef]:
        return self.list(tags={"computer-use": "true", "computer-use.owner": owner})

    def list_by_run_id(self, run_id: str) -> list[SandboxRef]:
        return self.list(tags={"computer-use.run_id": run_id})

    def list_older_than(self, cutoff: datetime, *, owner: str | None = None) -> list[SandboxRef]:
        return [
            ref
            for ref in (self.list_by_owner(owner) if owner else self.list())
            if ref.created_at is not None and _as_utc(ref.created_at) < _as_utc(cutoff)
        ]

    def list_sandboxes(self, tags: dict[str, str] | None = None) -> list[object]:
        modal = self._modal()
        return list(modal.Sandbox.list(tags=tags or {"computer-use": "true"}))

    def list_sandboxes_with_refs(
        self,
        tags: dict[str, str] | None = None,
    ) -> list[tuple[object, SandboxRef]]:
        sandboxes = self.list_sandboxes(tags=tags)
        return [(sandbox, self.ref_from_sandbox(sandbox)) for sandbox in sandboxes]

    def ref_from_sandbox(self, sandbox: object) -> SandboxRef:
        sandbox_tags = _safe_tags(sandbox)
        return SandboxRef(
            sandbox_id=getattr(sandbox, "object_id", "unknown"),
            app_name=self.app_name,
            name=getattr(sandbox, "name", None),
            run_id=sandbox_tags.get("computer-use.run_id"),
            owner=sandbox_tags.get("computer-use.owner"),
            created_at=_parse_created_at(sandbox_tags.get("computer-use.created_at")),
            config_hash=sandbox_tags.get("computer-use.config_hash"),
            status="unknown",
            tags=sandbox_tags,
            artifacts_dir=sandbox_tags.get("computer-use.artifacts_dir", "/home/desktop/artifacts"),
        )

    def find_by_run_id(self, run_id: str) -> SandboxRef | None:
        matches = self.list_by_run_id(run_id)
        if len(matches) == 0:
            return None
        return self.require_one(matches)

    def require_sandbox_by_run_id(self, run_id: str) -> object:
        matches = self.list_sandboxes(tags={"computer-use.run_id": run_id})
        return self.require_one_sandbox(matches, description=f"run_id={run_id}")

    def require_sandbox_by_id(self, sandbox_id: str) -> object:
        modal = self._modal()
        try:
            return modal.Sandbox.from_id(sandbox_id)
        except Exception as exc:
            raise SandboxUnavailableError(f"no matching sandbox_id={sandbox_id} found") from exc

    def require_one(
        self,
        candidates: Iterable[SandboxRef],
        *,
        description: str = "computer-use sandbox",
    ) -> SandboxRef:
        matches = list(candidates)
        if not matches:
            raise SandboxUnavailableError(f"no matching {description} found")
        if len(matches) > 1:
            raise SandboxAmbiguousError(
                f"multiple matching {description}s found; attach by sandbox_id or name"
            )
        return matches[0]

    def require_one_sandbox(
        self,
        candidates: Iterable[object],
        *,
        description: str = "computer-use sandbox",
    ) -> object:
        matches = list(candidates)
        if not matches:
            raise SandboxUnavailableError(f"no matching {description} found")
        if len(matches) > 1:
            raise SandboxAmbiguousError(
                f"multiple matching {description}s found; attach by sandbox_id or name"
            )
        return matches[0]


def _safe_tags(sandbox: object) -> dict[str, str]:
    if not hasattr(sandbox, "get_tags"):
        return {}
    tags = sandbox.get_tags()
    if not isinstance(tags, dict):
        return {}
    return {str(key): str(value) for key, value in tags.items()}


def _parse_created_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
