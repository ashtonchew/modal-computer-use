from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from .errors import ModalNotInstalledError, SandboxAmbiguousError, SandboxUnavailableError
from .models import SandboxRef
from .state import APP_ID_TAG


class SandboxRegistry:
    def __init__(
        self,
        app_name: str = "modal-computer-use",
        *,
        environment_name: str | None = None,
        allow_legacy_unscoped: bool = False,
    ) -> None:
        self.app_name = app_name
        self.environment_name = environment_name
        self.allow_legacy_unscoped = allow_legacy_unscoped
        self._app_id: str | None = None

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
        return self.list(tags={"computer-use.owner": owner})

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
        app_id = self._resolve_app_id(modal)
        scoped_tags = tags or (
            {"computer-use": "true"}
            if self.allow_legacy_unscoped
            else {APP_ID_TAG: app_id}
        )
        if not self.allow_legacy_unscoped:
            scoped_tags = {**scoped_tags, APP_ID_TAG: app_id}
        return list(modal.Sandbox.list(app_id=app_id, tags=scoped_tags))

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

    def find_sandbox_by_run_id(self, run_id: str) -> object | None:
        try:
            matches = self.list_sandboxes(tags={"computer-use.run_id": run_id})
        except SandboxUnavailableError as exc:
            if _is_modal_not_found_error(exc.__cause__):
                return None
            raise
        if len(matches) == 0:
            return None
        return self.require_one_sandbox(matches, description=f"run_id={run_id}")

    def require_sandbox_by_run_id(self, run_id: str) -> object:
        matches = self.list_sandboxes(tags={"computer-use.run_id": run_id})
        return self.require_one_sandbox(matches, description=f"run_id={run_id}")

    def require_sandbox_by_id(self, sandbox_id: str) -> object:
        modal = self._modal()
        try:
            sandbox = modal.Sandbox.from_id(sandbox_id)
        except Exception as exc:
            raise SandboxUnavailableError(f"no matching sandbox_id={sandbox_id} found") from exc
        try:
            self.require_app_owned(sandbox, description=f"sandbox_id={sandbox_id}")
        except BaseException as exc:
            _detach_after_failed_validation(sandbox, primary=exc)
            raise
        return sandbox

    def require_app_owned(self, sandbox: object, *, description: str) -> None:
        modal = self._modal()
        app_id = self._resolve_app_id(modal)
        sandbox_id = str(getattr(sandbox, "object_id", ""))
        if not sandbox_id:
            raise SandboxUnavailableError(f"no matching {description} found")
        tags = _safe_tags(sandbox)
        if tags.get(APP_ID_TAG) == app_id:
            candidates = modal.Sandbox.list(app_id=app_id, tags={APP_ID_TAG: app_id})
        elif self.allow_legacy_unscoped and APP_ID_TAG not in tags:
            candidates = modal.Sandbox.list(app_id=app_id)
        else:
            raise SandboxUnavailableError(f"no app-owned {description} found")
        if not any(
            str(getattr(candidate, "object_id", "")) == sandbox_id
            for candidate in candidates
        ):
            raise SandboxUnavailableError(f"no app-owned {description} found")

    def require_app_tag(self, sandbox: object, *, description: str) -> None:
        modal = self._modal()
        app_id = self._resolve_app_id(modal)
        tags = _safe_tags(sandbox)
        if tags.get(APP_ID_TAG) == app_id:
            return
        if self.allow_legacy_unscoped and APP_ID_TAG not in tags:
            return
        raise SandboxUnavailableError(f"no app-owned {description} found")

    def _resolve_app_id(self, modal: object) -> str:
        if self._app_id is not None:
            return self._app_id
        try:
            lookup_kwargs = {"create_if_missing": False}
            if self.environment_name is not None:
                lookup_kwargs["environment_name"] = self.environment_name
            app = modal.App.lookup(self.app_name, **lookup_kwargs)
            app_id = str(app.app_id)
        except Exception as exc:
            raise SandboxUnavailableError(f"no matching app={self.app_name} found") from exc
        self._app_id = app_id
        return app_id

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
            error = SandboxAmbiguousError(
                f"multiple matching {description}s found; attach by sandbox_id or name"
            )
            for sandbox in matches:
                _detach_after_failed_validation(sandbox, primary=error)
            raise error
        return matches[0]


def _safe_tags(sandbox: object) -> dict[str, str]:
    if not hasattr(sandbox, "get_tags"):
        return {}
    tags = sandbox.get_tags()
    if not isinstance(tags, dict):
        return {}
    return {str(key): str(value) for key, value in tags.items()}


def _detach_after_failed_validation(sandbox: object, *, primary: BaseException) -> None:
    detach = getattr(sandbox, "detach", None)
    if not callable(detach):
        return
    try:
        detach()
    except BaseException as cleanup_exc:
        primary.add_note(
            f"resource cleanup also failed: sandbox.detach ({type(cleanup_exc).__name__})"
        )


def _is_modal_not_found_error(exc: BaseException | None) -> bool:
    try:
        from modal.exception import NotFoundError
    except ImportError:
        return False
    return isinstance(exc, NotFoundError)


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
