from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from time import monotonic, sleep
from typing import Any, Literal
from uuid import uuid4

import httpx

from .config import ComputerConfig
from .errors import (
    BrowserReadinessError,
    DaemonHTTPError,
    FrameValidationError,
    SandboxUnavailableError,
)
from .latency import (
    WarmPoolClaim,
    WarmPoolClaimMetrics,
    WarmPoolEntry,
    WarmPoolFillResult,
    WarmPoolPolicy,
    WarmPoolReconcileResult,
    estimate_warm_idle_cost,
    pool_config_identity,
)
from .models import SandboxCleanupItem, SandboxCleanupResult, SandboxRef
from .registry import SandboxRegistry
from .sandbox import (
    ComputerSandbox,
    ConfigMismatchPolicy,
    ReusePolicy,
    _is_modal_availability_error,
)


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

    def fill_warm_pool(
        self,
        *,
        config: ComputerConfig,
        policy: WarmPoolPolicy,
        queue: object | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> WarmPoolFillResult:
        """Create missing fixed slots and enqueue them only after a valid first frame."""
        policy.validate_config(config)
        pool_queue = queue or _modal_pool_queue(self.app_name, policy.pool_name)
        identity = pool_config_identity(config)
        existing = self.registry.list_sandboxes_with_refs(
            tags={"computer-use.pool": policy.pool_name}
        )
        current = _as_utc(now())
        allowed_names = {f"{policy.pool_name}-{index:03d}" for index in range(policy.capacity)}
        valid_existing: list[tuple[object, SandboxRef]] = []
        for sandbox, ref in existing:
            if not _warm_sandbox_running(sandbox):
                terminate = getattr(sandbox, "terminate", None)
                if callable(terminate):
                    terminate(wait=True)
                continue
            if ref.name not in allowed_names:
                if ref.tags.get("computer-use.pool_state") == "claimed":
                    continue
                terminate = getattr(sandbox, "terminate", None)
                if not callable(terminate):
                    raise RuntimeError(
                        "cannot enforce warm pool capacity because termination is unavailable"
                    )
                terminate(wait=True)
                continue
            reason = _warm_slot_rejection_reason(
                ref,
                expected_identity=identity,
                policy=policy,
                now=current,
            )
            if reason is None:
                valid_existing.append((sandbox, ref))
                continue
            terminate = getattr(sandbox, "terminate", None)
            if not callable(terminate):
                valid_existing.append((sandbox, ref))
                continue
            terminate(wait=True)
        for sandbox, ref in valid_existing:
            if ref.tags.get("computer-use.pool_state") != "ready":
                continue
            with _warm_slot_lock(sandbox) as acquired:
                if not acquired:
                    continue
                live_ref = _refresh_pool_ref(sandbox, ref)
                if live_ref.tags.get("computer-use.pool_state") != "ready" or (
                    _warm_slot_rejection_reason(
                        live_ref,
                        expected_identity=identity,
                        policy=policy,
                        now=_as_utc(now()),
                    )
                    is not None
                ):
                    continue
                queue_identity = uuid4().hex
                entry = _warm_entry_from_ref(
                    live_ref,
                    config=config,
                    policy=policy,
                    queue_identity=queue_identity,
                )
                _set_pool_queue_identity(sandbox, queue_identity)
                _queue_replace_slot(pool_queue, entry)
        existing_names = {ref.name for _, ref in valid_existing if ref.name is not None}
        entries: list[WarmPoolEntry] = []
        concurrent_existing_count = 0
        slots_to_create = [
            f"{policy.pool_name}-{index:03d}"
            for index in range(policy.capacity)
            if f"{policy.pool_name}-{index:03d}" not in existing_names
        ]
        allowed = max(0, policy.capacity - len(valid_existing))
        for slot_name in slots_to_create[:allowed]:
            created_at = _as_utc(now())
            expires_at = created_at + timedelta(seconds=config.runtime.timeout_seconds)
            slot_config = config.model_copy(deep=True)
            slot_config.run_id = f"pool-{policy.pool_name}-{slot_name}"
            computer: ComputerSandbox | None = None
            try:
                computer = ComputerSandbox.create(
                    app_name=self.app_name,
                    config=slot_config,
                    name=slot_name,
                    tags={
                        "computer-use.pool": policy.pool_name,
                        "computer-use.pool_identity": identity,
                        "computer-use.pool_state": "provisioning",
                        "computer-use.pool_expires_at": _utc_iso(expires_at),
                    },
                    wait=True,
                    tag_profile="warm_pool",
                )
                computer.ensure_browser_ready(slot_config)
                computer.first_valid_frame(slot_config)
                metadata = computer.metadata()
                if metadata is None:
                    raise RuntimeError("warm Sandbox metadata is unavailable")
                actual_region = computer.runtime_region()
                ready_at = _as_utc(now())
                queue_identity = uuid4().hex
                entry = WarmPoolEntry(
                    sandbox_id=metadata.sandbox_id,
                    slot_name=slot_name,
                    pool_name=policy.pool_name,
                    app_name=self.app_name,
                    config_identity=identity,
                    queue_identity=queue_identity,
                    created_at=created_at,
                    ready_at=ready_at,
                    expires_at=expires_at,
                    requested_region=config.runtime.modal_region,
                    actual_region=actual_region,
                    cpu=config.resources.cpu,
                    memory_mib=config.resources.memory_mib,
                )
                if (
                    policy.rejection_reason(
                        entry,
                        expected_identity=identity,
                        now=ready_at,
                    )
                    is not None
                ):
                    computer.terminate(wait=True)
                    continue
                computer.set_tags(
                    {
                        "computer-use.pool_state": "ready",
                        "computer-use.pool_ready_at": _utc_iso(ready_at),
                        "computer-use.pool_actual_region": entry.actual_region or "unknown",
                        "computer-use.pool_queue_identity": queue_identity,
                    }
                )
                _queue_replace_slot(pool_queue, entry)
                entries.append(entry)
            except Exception:
                if computer is not None:
                    computer.terminate(wait=True)
                    raise
                if self._pool_slot_reserved_by_concurrent_fill(
                    slot_name=slot_name,
                    identity=identity,
                    policy=policy,
                    now=_as_utc(now()),
                ):
                    concurrent_existing_count += 1
                    continue
                raise
            finally:
                if computer is not None:
                    computer.detach()
        return WarmPoolFillResult(
            pool_name=policy.pool_name,
            config_identity=identity,
            requested_region=config.runtime.modal_region,
            configured_capacity=policy.capacity,
            existing_count=len(valid_existing) + concurrent_existing_count,
            created_count=len(entries),
            entries=tuple(entries),
        )

    def _pool_slot_reserved_by_concurrent_fill(
        self,
        *,
        slot_name: str,
        identity: str,
        policy: WarmPoolPolicy,
        now: datetime,
    ) -> bool:
        """Confirm a fixed-name race after Modal rejects a duplicate allocation."""
        entries = self.registry.list_sandboxes_with_refs(
            tags={"computer-use.pool": policy.pool_name}
        )
        return any(
            ref.name == slot_name
            and _warm_slot_rejection_reason(
                ref,
                expected_identity=identity,
                policy=policy,
                now=now,
            )
            is None
            for _, ref in entries
        )

    def claim_warm_pool(
        self,
        *,
        config: ComputerConfig,
        policy: WarmPoolPolicy,
        queue: object | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> WarmPoolClaim:
        """Atomically claim one one-shot warm Sandbox or create a cold fallback."""
        policy.validate_config(config)
        pool_queue = queue or _modal_pool_queue(self.app_name, policy.pool_name)
        identity = pool_config_identity(config)
        started = monotonic_clock()
        rejection_reasons: list[str] = []
        scanned = 0
        for scan_index in range(policy.capacity * policy.max_claim_scan):
            if scanned >= policy.max_claim_scan:
                break
            index = scan_index % policy.capacity
            slot_name = f"{policy.pool_name}-{index:03d}"
            raw = _queue_get(pool_queue, partition=slot_name)
            if raw is None:
                continue
            scanned += 1
            if not isinstance(raw, dict):
                rejection_reasons.append("invalid_entry")
                continue
            try:
                entry = WarmPoolEntry.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                rejection_reasons.append("invalid_entry")
                continue
            if entry.app_name != self.app_name:
                rejection_reasons.append("app_mismatch")
                continue
            current = _as_utc(now())
            reason = policy.rejection_reason(
                entry,
                expected_identity=identity,
                now=current,
            )
            if reason is not None:
                rejection_reasons.append(reason)
                self._discard_warm_entry(entry)
                continue
            try:
                computer = ComputerSandbox.attach(
                    sandbox_id=entry.sandbox_id,
                    app_name=self.app_name,
                    ingress=config.ingress,
                    wait=True,
                    readiness_timeout=config.runtime.readiness_timeout_seconds,
                )
            except Exception as exc:
                reason = _warm_candidate_exception_reason(exc, phase="attach")
                if reason is None:
                    raise
                rejection_reasons.append(reason)
                continue

            try:
                live_tags = computer.tags()
            except Exception as exc:
                _detach_warm_candidate(computer, primary=exc)
                raise
            live_tag_reason = _warm_claim_tag_rejection_reason(live_tags, entry)
            if live_tag_reason is not None:
                rejection_reasons.append(live_tag_reason)
                _detach_warm_candidate(computer)
                continue

            try:
                finished = computer.poll() is not None
            except Exception as exc:
                reason = _warm_candidate_exception_reason(exc, phase="poll")
                _retire_warm_candidate(computer, primary=exc)
                if reason is None:
                    raise
                rejection_reasons.append(reason)
                continue
            if finished:
                rejection_reasons.append("candidate_finished")
                _retire_warm_candidate(computer)
                continue

            try:
                computer.ensure_browser_ready(config)
            except Exception as exc:
                reason = _warm_candidate_exception_reason(exc, phase="browser")
                _retire_warm_candidate(computer, primary=exc)
                if reason is None:
                    raise
                rejection_reasons.append(reason)
                continue
            request_to_authenticated_ms = (monotonic_clock() - started) * 1000.0

            try:
                computer.first_valid_frame(config)
            except Exception as exc:
                reason = _warm_candidate_exception_reason(exc, phase="first_frame")
                _retire_warm_candidate(computer, primary=exc)
                if reason is None:
                    raise
                rejection_reasons.append(reason)
                continue
            request_to_first_frame_ms = (monotonic_clock() - started) * 1000.0

            claimed_at = _as_utc(now())
            post_validation_reason = policy.rejection_reason(
                entry,
                expected_identity=identity,
                now=claimed_at,
            )
            if post_validation_reason is not None:
                rejection_reasons.append(post_validation_reason)
                _retire_warm_candidate(computer)
                continue

            claim_transition_started = False
            candidate_identity_verified = True
            try:
                with _warm_slot_lock(_warm_lock_target(computer)) as acquired:
                    if not acquired:
                        rejection_reasons.append("slot_busy")
                        _detach_warm_candidate(computer)
                        continue
                    try:
                        live_tags = computer.tags()
                    except Exception as exc:
                        candidate_identity_verified = False
                        _detach_warm_candidate(computer, primary=exc)
                        raise
                    live_tag_reason = _warm_claim_tag_rejection_reason(live_tags, entry)
                    if live_tag_reason is not None:
                        rejection_reasons.append(live_tag_reason)
                        _detach_warm_candidate(computer)
                        continue
                    claim_transition_started = True
                    computer.set_tags(
                        {
                            "computer-use.pool_state": "claimed",
                            "computer-use.pool_claimed_at": _utc_iso(claimed_at),
                        },
                        remove={"computer-use.pool_queue_identity"},
                    )
                    computer._requested_modal_region = config.runtime.modal_region
                    remaining = max(
                        0.0,
                        (entry.expires_at - claimed_at).total_seconds()
                        - policy.expiry_skew_seconds,
                    )
                    return WarmPoolClaim(
                        computer=computer,
                        entry=entry,
                        metrics=WarmPoolClaimMetrics(
                            pool_name=policy.pool_name,
                            configured_pool_size=policy.capacity,
                            config_identity=identity,
                            requested_region=config.runtime.modal_region,
                            actual_region=entry.actual_region,
                            hit=True,
                            claim_elapsed_ms=(monotonic_clock() - started) * 1000.0,
                            cold_fallback=False,
                            request_to_authenticated_ms=request_to_authenticated_ms,
                            rejection_reasons=tuple(rejection_reasons),
                            remaining_lifetime_seconds=remaining,
                            cost_accounting=estimate_warm_idle_cost(
                                entry,
                                claimed_at=claimed_at,
                                configured_pool_size=policy.capacity,
                            ),
                            request_to_first_frame_ms=request_to_first_frame_ms,
                        ),
                    )
            except Exception as exc:
                if not candidate_identity_verified:
                    raise
                _retire_warm_candidate(computer, primary=exc)
                if claim_transition_started:
                    raise
                reason = _warm_candidate_exception_reason(exc, phase="claim_lock")
                if reason is None:
                    raise
                rejection_reasons.append(reason)
                continue

        claim_elapsed_ms = (monotonic_clock() - started) * 1000.0
        cold = ComputerSandbox.create(
            app_name=self.app_name,
            config=config,
            wait=True,
        )
        try:
            cold.ensure_browser_ready(config)
            request_to_authenticated_ms = (monotonic_clock() - started) * 1000.0
            cold.first_valid_frame(config)
            request_to_first_frame_ms = (monotonic_clock() - started) * 1000.0
            actual_region = cold.runtime_region()
        except Exception as exc:
            _retire_warm_candidate(cold, primary=exc)
            raise
        return WarmPoolClaim(
            computer=cold,
            metrics=WarmPoolClaimMetrics(
                pool_name=policy.pool_name,
                configured_pool_size=policy.capacity,
                config_identity=identity,
                requested_region=config.runtime.modal_region,
                actual_region=actual_region,
                hit=False,
                claim_elapsed_ms=claim_elapsed_ms,
                cold_fallback=True,
                request_to_authenticated_ms=request_to_authenticated_ms,
                miss_reason="empty" if not rejection_reasons else "rejected",
                rejection_reasons=tuple(rejection_reasons),
                request_to_first_frame_ms=request_to_first_frame_ms,
            ),
        )

    def reconcile_warm_pool(
        self,
        *,
        config: ComputerConfig,
        policy: WarmPoolPolicy,
        now: datetime | None = None,
        provisioning_grace_seconds: int = 300,
    ) -> WarmPoolReconcileResult:
        """Terminate invalid, expired, and abandoned provisioning slots.

        Ready slots remain queued until claim or expiry. Claimed slots belong to
        their consumer and are not reclaimed here.
        """
        policy.validate_config(config)
        if provisioning_grace_seconds < 1:
            raise ValueError("provisioning_grace_seconds must be positive")
        current = _as_utc(now or datetime.now(UTC))
        expected_identity = pool_config_identity(config)
        entries = self.registry.list_sandboxes_with_refs(
            tags={"computer-use.pool": policy.pool_name}
        )
        allowed_slots = {f"{policy.pool_name}-{index:03d}" for index in range(policy.capacity)}
        terminated: list[tuple[str, str]] = []
        skipped_count = 0
        for sandbox, ref in entries:
            tags = ref.tags
            state = tags.get("computer-use.pool_state")
            reason: str | None = None
            if not _warm_sandbox_running(sandbox):
                reason = "finished"
            elif state == "claimed":
                skipped_count += 1
                continue
            elif ref.name not in allowed_slots:
                reason = "outside_capacity"
            elif reason is None:
                reason = _warm_slot_rejection_reason(
                    ref,
                    expected_identity=expected_identity,
                    policy=policy,
                    now=current,
                    provisioning_grace_seconds=provisioning_grace_seconds,
                )
            if reason is None:
                skipped_count += 1
                continue
            terminate = getattr(sandbox, "terminate", None)
            if callable(terminate):
                terminate(wait=True)
                terminated.append((ref.sandbox_id, reason))
            else:
                skipped_count += 1
        return WarmPoolReconcileResult(
            pool_name=policy.pool_name,
            inspected_count=len(entries),
            terminated_count=len(terminated),
            terminated=tuple(terminated),
            skipped_count=skipped_count,
        )

    def _discard_warm_entry(self, entry: WarmPoolEntry) -> None:
        try:
            computer = ComputerSandbox.attach(
                sandbox_id=entry.sandbox_id,
                app_name=self.app_name,
                wait=False,
            )
        except Exception as exc:
            if _warm_candidate_exception_reason(exc, phase="attach") is not None:
                return
            raise
        try:
            live_tag_reason = _warm_claim_tag_rejection_reason(computer.tags(), entry)
        except Exception as exc:
            _detach_warm_candidate(computer, primary=exc)
            raise
        if live_tag_reason is not None:
            _detach_warm_candidate(computer)
            return
        _retire_warm_candidate(computer)


WarmCandidatePhase = Literal[
    "attach",
    "poll",
    "browser",
    "first_frame",
    "claim_lock",
]


def _warm_candidate_exception_reason(
    exc: Exception,
    *,
    phase: WarmCandidatePhase,
) -> str | None:
    operational = isinstance(
        exc,
        (
            SandboxUnavailableError,
            TimeoutError,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            httpx.TimeoutException,
        ),
    ) or _is_daemon_availability_error(exc) or _is_modal_availability_error(exc)
    if phase == "attach" and operational:
        return "attach_unavailable"
    if phase == "poll" and operational:
        return "candidate_unavailable"
    if phase == "browser" and (
        operational or isinstance(exc, BrowserReadinessError)
    ):
        return "browser_not_ready"
    if phase == "first_frame" and (
        operational or isinstance(exc, FrameValidationError)
    ):
        return "first_frame_invalid"
    if phase == "claim_lock" and operational:
        return "claim_lock_unavailable"
    return None


def _is_daemon_availability_error(exc: Exception) -> bool:
    if not isinstance(exc, DaemonHTTPError):
        return False
    status_code = exc.status_code
    return status_code in {404, 408, 410, 425, 429} or (
        status_code is not None and status_code >= 500
    )


def _retire_warm_candidate(
    computer: ComputerSandbox,
    *,
    primary: BaseException | None = None,
) -> None:
    cleanup_errors: list[tuple[str, Exception]] = []
    try:
        computer.terminate(wait=True)
    except Exception as exc:
        cleanup_errors.append(("terminate", exc))
    try:
        computer.detach()
    except Exception as exc:
        cleanup_errors.append(("detach", exc))
    _raise_warm_cleanup_errors(
        cleanup_errors,
        primary=primary,
        prefix="warm candidate cleanup also failed",
    )


def _detach_warm_candidate(
    computer: ComputerSandbox,
    *,
    primary: BaseException | None = None,
) -> None:
    cleanup_errors: list[tuple[str, Exception]] = []
    try:
        computer.detach()
    except Exception as exc:
        cleanup_errors.append(("detach", exc))
    _raise_warm_cleanup_errors(
        cleanup_errors,
        primary=primary,
        prefix="warm candidate cleanup also failed",
    )


def _raise_warm_cleanup_errors(
    cleanup_errors: list[tuple[str, Exception]],
    *,
    primary: BaseException | None,
    prefix: str,
) -> None:
    if not cleanup_errors:
        return
    detail = ", ".join(f"{operation} ({type(exc).__name__})" for operation, exc in cleanup_errors)
    if primary is not None:
        primary.add_note(f"{prefix}: {detail}")
        raise primary
    cleanup_error = cleanup_errors[0][1]
    if len(cleanup_errors) > 1:
        cleanup_error.add_note(f"{prefix}: {detail}")
    raise cleanup_error


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


def _utc_iso(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _parse_tag_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _warm_slot_rejection_reason(
    ref: SandboxRef,
    *,
    expected_identity: str,
    policy: WarmPoolPolicy,
    now: datetime,
    provisioning_grace_seconds: int = 300,
) -> str | None:
    tags = ref.tags
    state = tags.get("computer-use.pool_state")
    if state == "claimed":
        return None
    if state not in {"ready", "provisioning"}:
        return "invalid_state"
    if tags.get("computer-use.pool_identity") != expected_identity:
        return "config_mismatch"
    expires_at = _parse_tag_datetime(tags.get("computer-use.pool_expires_at"))
    if expires_at is None:
        return "invalid_expiry"
    remaining = (expires_at - now).total_seconds() - policy.expiry_skew_seconds
    if remaining < policy.min_remaining_seconds:
        return "near_expiry"
    if state == "ready" and _parse_tag_datetime(tags.get("computer-use.pool_ready_at")) is None:
        return "invalid_ready_at"
    if state == "provisioning":
        created_at = ref.created_at
        if (
            created_at is None
            or (now - _as_utc(created_at)).total_seconds() >= provisioning_grace_seconds
        ):
            return "abandoned_provisioning"
    return None


def _warm_sandbox_running(sandbox: object) -> bool:
    poll = getattr(sandbox, "poll", None)
    if not callable(poll):
        return True
    return poll() is None


def _warm_entry_from_ref(
    ref: SandboxRef,
    *,
    config: ComputerConfig,
    policy: WarmPoolPolicy,
    queue_identity: str,
) -> WarmPoolEntry:
    if ref.name is None or ref.created_at is None:
        raise RuntimeError("ready warm slot metadata is incomplete")
    ready_at = _parse_tag_datetime(ref.tags.get("computer-use.pool_ready_at"))
    expires_at = _parse_tag_datetime(ref.tags.get("computer-use.pool_expires_at"))
    if ready_at is None or expires_at is None:
        raise RuntimeError("ready warm slot timing tags are incomplete")
    actual_region = ref.tags.get("computer-use.pool_actual_region")
    return WarmPoolEntry(
        sandbox_id=ref.sandbox_id,
        slot_name=ref.name,
        pool_name=policy.pool_name,
        app_name=ref.app_name,
        config_identity=ref.tags["computer-use.pool_identity"],
        queue_identity=queue_identity,
        created_at=_as_utc(ref.created_at),
        ready_at=ready_at,
        expires_at=expires_at,
        requested_region=config.runtime.modal_region,
        actual_region=None if actual_region in {None, "unknown"} else actual_region,
        cpu=config.resources.cpu,
        memory_mib=config.resources.memory_mib,
    )


def _set_pool_queue_identity(
    sandbox: object,
    queue_identity: str,
) -> None:
    get_tags = getattr(sandbox, "get_tags", None)
    remote_tags = get_tags() if callable(get_tags) else {}
    complete_tags = {
        **(remote_tags if isinstance(remote_tags, dict) else {}),
        "computer-use.pool_queue_identity": queue_identity,
    }
    if len(complete_tags) > 10:
        raise RuntimeError("ready warm slot exceeds Modal's 10-tag limit")
    set_tags = getattr(sandbox, "set_tags", None)
    if not callable(set_tags):
        raise RuntimeError("ready warm slot tag replacement is unavailable")
    set_tags(complete_tags)


def _refresh_pool_ref(sandbox: object, ref: SandboxRef) -> SandboxRef:
    get_tags = getattr(sandbox, "get_tags", None)
    raw_tags = get_tags() if callable(get_tags) else {}
    if not isinstance(raw_tags, dict):
        raise RuntimeError("warm slot tags are unavailable")
    return ref.model_copy(
        update={"tags": {str(key): str(value) for key, value in raw_tags.items()}}
    )


def _warm_claim_tag_rejection_reason(
    tags: dict[str, str],
    entry: WarmPoolEntry,
) -> str | None:
    if tags.get("computer-use.pool_state") != "ready":
        return "slot_not_ready"
    if tags.get("computer-use.pool") != entry.pool_name:
        return "pool_mismatch"
    if tags.get("computer-use.pool_identity") != entry.config_identity:
        return "config_mismatch"
    if tags.get("computer-use.pool_queue_identity") != entry.queue_identity:
        return "queue_identity_mismatch"
    return None


_WARM_LOCK_PATH = "/home/desktop/.modal-computer-use/warm-pool.lock"
_WARM_LOCK_OWNER_PATH = "/home/desktop/.modal-computer-use/warm-pool-lock-owner"
_WARM_LOCK_SCRIPT = """
import fcntl
import pathlib
import sys

lock_path = pathlib.Path(sys.argv[1])
lock_path.parent.mkdir(parents=True, exist_ok=True)
handle = lock_path.open("w")
try:
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(75)
pathlib.Path(sys.argv[2]).write_text(sys.argv[3])
sys.stdin.buffer.read(1)
"""
_WARM_LOCK_CHECK_SCRIPT = """
import pathlib
import sys

try:
    owner = pathlib.Path(sys.argv[1]).read_text()
except FileNotFoundError:
    raise SystemExit(1)
raise SystemExit(0 if owner == sys.argv[2] else 1)
"""


def _warm_lock_target(computer: ComputerSandbox) -> object:
    return getattr(computer, "_sandbox", None) or computer


@contextmanager
def _warm_slot_lock(sandbox: object) -> Iterator[bool]:
    exec_process = getattr(sandbox, "exec", None)
    if not callable(exec_process):
        yield False
        return
    owner = uuid4().hex
    holder = exec_process(
        "python",
        "-c",
        _WARM_LOCK_SCRIPT,
        _WARM_LOCK_PATH,
        _WARM_LOCK_OWNER_PATH,
        owner,
        timeout=60,
    )
    acquired = False
    for _ in range(20):
        poll = getattr(holder, "poll", None)
        if callable(poll) and poll() is not None:
            break
        checker = exec_process(
            "python",
            "-c",
            _WARM_LOCK_CHECK_SCRIPT,
            _WARM_LOCK_OWNER_PATH,
            owner,
            timeout=5,
        )
        wait = getattr(checker, "wait", None)
        if callable(wait) and wait() == 0:
            acquired = True
            break
        sleep(0.05)
    try:
        yield acquired
    finally:
        stdin = getattr(holder, "stdin", None)
        write_eof = getattr(stdin, "write_eof", None)
        drain = getattr(stdin, "drain", None)
        with suppress(Exception):
            if callable(write_eof):
                write_eof()
            if callable(drain):
                drain()
            wait = getattr(holder, "wait", None)
            if callable(wait):
                wait()


def _modal_pool_queue(app_name: str, pool_name: str) -> object:
    try:
        import modal
    except ImportError as exc:
        from .errors import ModalNotInstalledError

        raise ModalNotInstalledError("Modal warm capacity requires the modal extra") from exc
    namespace = hashlib.sha256(f"{app_name}\0{pool_name}".encode()).hexdigest()[:24]
    return modal.Queue.from_name(f"modal-computer-use-warm-{namespace}", create_if_missing=True)


def _queue_put(
    queue: object,
    value: dict[str, Any],
    *,
    partition: str,
) -> None:
    put = getattr(queue, "put", None)
    if not callable(put):
        raise TypeError("warm pool queue must provide put")
    put(value, block=False, partition=partition)


def _queue_get(queue: object, *, partition: str) -> Any:
    get = getattr(queue, "get", None)
    if not callable(get):
        raise TypeError("warm pool queue must provide get")
    value = get(block=False, partition=partition)
    if value is None:
        return None
    return value


def _queue_replace_slot(queue: object, entry: WarmPoolEntry) -> None:
    for _ in range(5_000):
        if _queue_get(queue, partition=entry.slot_name) is None:
            break
    else:
        raise RuntimeError("warm pool slot partition exceeds Modal Queue limits")
    _queue_put(queue, entry.as_dict(), partition=entry.slot_name)
