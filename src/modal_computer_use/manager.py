from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from .config import ComputerConfig
from .models import SandboxCleanupItem, SandboxCleanupResult, SandboxRef
from .registry import SandboxRegistry
from .sandbox import ComputerSandbox, ConfigMismatchPolicy, ReusePolicy


class ComputerSandboxManager:
    """Small orchestration facade for create/list/attach flows.

    This is intentionally thin. It does not own a provider loop or a policy engine.
    """

    def __init__(self, app_name: str = "modal-computer-use") -> None:
        self.app_name = app_name
        self.registry = SandboxRegistry(app_name=app_name)

    def create(self, *, config: ComputerConfig | None = None, **kwargs: object) -> ComputerSandbox:
        return ComputerSandbox.create(app_name=self.app_name, config=config, **kwargs)

    def attach(self, sandbox_id: str, **kwargs: object) -> ComputerSandbox:
        return ComputerSandbox.attach(sandbox_id=sandbox_id, app_name=self.app_name, **kwargs)

    def attach_or_create(
        self,
        *,
        config: ComputerConfig | None = None,
        run_id: str | None = None,
        name: str | None = None,
        reuse: bool | ReusePolicy = "by_run_id",
        on_config_mismatch: ConfigMismatchPolicy = "raise",
        **kwargs: object,
    ) -> ComputerSandbox:
        return ComputerSandbox.attach_or_create(
            app_name=self.app_name,
            config=config,
            run_id=run_id,
            name=name,
            reuse=reuse,
            on_config_mismatch=on_config_mismatch,
            **kwargs,
        )

    def list(self, *, owner: str | None = None) -> list[SandboxRef]:
        if owner is not None:
            return self.registry.list_by_owner(owner)
        return self.registry.list()

    def find_by_run_id(self, run_id: str) -> SandboxRef | None:
        return self.registry.find_by_run_id(run_id)

    def terminate(self, sandbox_id: str) -> None:
        sandbox = self.registry.require_sandbox_by_id(sandbox_id)
        if hasattr(sandbox, "terminate"):
            sandbox.terminate()

    def cleanup_expired(
        self,
        *,
        ttl_seconds: int,
        owner: str | None = None,
        dry_run: bool = True,
        now: datetime | None = None,
    ) -> SandboxCleanupResult:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

        current_time = _as_utc(now or datetime.now(UTC))
        cutoff = current_time - timedelta(seconds=ttl_seconds)
        tag_filter = (
            {"computer-use": "true", "computer-use.owner": owner} if owner is not None else None
        )
        entries = self.registry.list_sandboxes_with_refs(tags=tag_filter)

        candidates: list[SandboxCleanupItem] = []
        skipped: list[SandboxCleanupItem] = []
        errors: list[SandboxCleanupItem] = []
        terminated_count = 0

        for sandbox, ref in entries:
            item = _cleanup_item(ref, status="skipped", reason="not_expired")
            created_at_tag = ref.tags.get("computer-use.created_at")
            if not created_at_tag:
                skipped.append(_cleanup_item(ref, status="skipped", reason="missing_created_at"))
                continue
            if ref.created_at is None:
                skipped.append(_cleanup_item(ref, status="skipped", reason="invalid_created_at"))
                continue
            if _as_utc(ref.created_at) >= cutoff:
                skipped.append(item)
                continue

            if dry_run:
                candidates.append(_cleanup_item(ref, status="candidate", reason="expired"))
                continue

            if not hasattr(sandbox, "terminate"):
                skipped.append(_cleanup_item(ref, status="skipped", reason="terminate_unavailable"))
                continue
            try:
                sandbox.terminate()
            except Exception as exc:
                errors.append(_cleanup_item(ref, status="error", reason=exc.__class__.__name__))
                continue
            terminated_count += 1
            candidates.append(_cleanup_item(ref, status="terminated", reason="expired"))

        return SandboxCleanupResult(
            dry_run=dry_run,
            ttl_seconds=ttl_seconds,
            inspected_count=len(entries),
            matched_count=len(candidates),
            terminated_count=terminated_count,
            candidates=candidates,
            skipped=skipped,
            errors=errors,
        )


SandboxManager = ComputerSandboxManager


def _cleanup_item(
    ref: SandboxRef,
    *,
    status: Literal["candidate", "terminated", "skipped", "error"],
    reason: str,
) -> SandboxCleanupItem:
    return SandboxCleanupItem(
        sandbox_id=ref.sandbox_id,
        name=ref.name,
        run_id=ref.run_id,
        owner=ref.owner,
        created_at=ref.created_at,
        status=status,
        reason=reason,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
