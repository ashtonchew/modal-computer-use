from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from time import monotonic
from typing import Any

from .config import ComputerConfig

_MODAL_CPU_RATE = 0.00003942
_MODAL_MEMORY_RATE = 0.00000667


@dataclass
class SessionStartupTiming:
    """Monotonic, secret-free timing marks for one session request."""

    clock: Callable[[], float] = monotonic
    _started: float = field(init=False, repr=False)
    _stages: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._started = self.clock()

    def mark(self, stage: str) -> None:
        elapsed_ms = max(0.0, (self.clock() - self._started) * 1000.0)
        previous = [
            value["elapsed_ms"]
            for value in self._stages.values()
            if value.get("status") == "observed" and value.get("elapsed_ms") is not None
        ]
        if previous and elapsed_ms < previous[-1]:
            raise ValueError("startup timing stages must be monotonic")
        self._stages[stage] = {"status": "observed", "elapsed_ms": elapsed_ms}

    def unsupported(self, stage: str, reason: str) -> None:
        self._stages[stage] = {
            "status": "unsupported",
            "elapsed_ms": None,
            "reason": reason,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"stages": {key: dict(value) for key, value in self._stages.items()}}


@dataclass(frozen=True)
class WarmPoolEntry:
    sandbox_id: str
    slot_name: str
    pool_name: str
    app_name: str
    config_identity: str
    queue_identity: str
    created_at: datetime
    ready_at: datetime
    expires_at: datetime
    requested_region: str | None
    actual_region: str | None
    cpu: float | None
    memory_mib: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "sandbox_id": self.sandbox_id,
            "slot_name": self.slot_name,
            "pool_name": self.pool_name,
            "app_name": self.app_name,
            "config_identity": self.config_identity,
            "queue_identity": self.queue_identity,
            "created_at": _utc_iso(self.created_at),
            "ready_at": _utc_iso(self.ready_at),
            "expires_at": _utc_iso(self.expires_at),
            "requested_region": self.requested_region,
            "actual_region": self.actual_region,
            "cpu": self.cpu,
            "memory_mib": self.memory_mib,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> WarmPoolEntry:
        return cls(
            sandbox_id=str(value["sandbox_id"]),
            slot_name=str(value["slot_name"]),
            pool_name=str(value["pool_name"]),
            app_name=str(value["app_name"]),
            config_identity=str(value["config_identity"]),
            queue_identity=str(value["queue_identity"]),
            created_at=_parse_datetime(value["created_at"]),
            ready_at=_parse_datetime(value["ready_at"]),
            expires_at=_parse_datetime(value["expires_at"]),
            requested_region=_optional_string(value.get("requested_region")),
            actual_region=_optional_string(value.get("actual_region")),
            cpu=_optional_float(value.get("cpu")),
            memory_mib=_optional_int(value.get("memory_mib")),
        )


@dataclass(frozen=True)
class WarmPoolPolicy:
    pool_name: str
    capacity: int
    min_remaining_seconds: int = 60
    expiry_skew_seconds: int = 15
    max_claim_scan: int = 4

    def __post_init__(self) -> None:
        if not self.pool_name.strip():
            raise ValueError("pool_name must be non-empty")
        if self.capacity < 1 or self.capacity > 100:
            raise ValueError("capacity must be between 1 and 100")
        longest_slot = f"{self.pool_name}-{self.capacity - 1:03d}"
        if len(longest_slot.encode("utf-8")) >= 64:
            raise ValueError("pool_name produces a Modal slot name of 64 bytes or longer")
        if len(longest_slot) >= 64 or re.fullmatch(r"[a-zA-Z0-9._-]+", longest_slot) is None:
            raise ValueError("pool_name produces an invalid Modal Sandbox name")
        if self.min_remaining_seconds < 1:
            raise ValueError("min_remaining_seconds must be positive")
        if self.expiry_skew_seconds < 0:
            raise ValueError("expiry_skew_seconds must be non-negative")
        if self.max_claim_scan < 1:
            raise ValueError("max_claim_scan must be positive")

    def validate_config(self, config: ComputerConfig) -> None:
        if config.runtime.idle_timeout_seconds is not None:
            raise ValueError("warm capacity does not support idle_timeout_seconds")
        if config.vnc_password is not None:
            raise ValueError("warm capacity does not support explicit vnc_password credentials")

    def rejection_reason(
        self,
        entry: WarmPoolEntry,
        *,
        expected_identity: str,
        now: datetime,
    ) -> str | None:
        current = _as_utc(now)
        if entry.pool_name != self.pool_name:
            return "pool_mismatch"
        allowed_slots = {f"{self.pool_name}-{index:03d}" for index in range(self.capacity)}
        if entry.slot_name not in allowed_slots:
            return "outside_capacity"
        if entry.config_identity != expected_identity:
            return "config_mismatch"
        remaining = (entry.expires_at - current).total_seconds() - self.expiry_skew_seconds
        if remaining < self.min_remaining_seconds:
            return "near_expiry"
        return None


@dataclass(frozen=True)
class WarmPoolFillResult:
    pool_name: str
    config_identity: str
    requested_region: str | None
    configured_capacity: int
    existing_count: int
    created_count: int
    entries: tuple[WarmPoolEntry, ...]


@dataclass(frozen=True)
class WarmPoolReconcileResult:
    pool_name: str
    inspected_count: int
    terminated_count: int
    terminated: tuple[tuple[str, str], ...]
    skipped_count: int


@dataclass(frozen=True)
class WarmPoolClaimMetrics:
    pool_name: str
    configured_pool_size: int
    config_identity: str
    requested_region: str | None
    actual_region: str | None
    hit: bool
    claim_elapsed_ms: float
    cold_fallback: bool
    request_to_authenticated_ms: float
    miss_reason: str | None = None
    rejection_reasons: tuple[str, ...] = ()
    remaining_lifetime_seconds: float | None = None
    cost_accounting: dict[str, Any] | None = None
    request_to_first_frame_ms: float | None = None


@dataclass
class WarmPoolClaim:
    computer: Any
    metrics: WarmPoolClaimMetrics
    entry: WarmPoolEntry | None = None
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.computer.terminate(wait=True)
        finally:
            self.computer.detach()
            self._closed = True

    def __enter__(self) -> Any:
        return self.computer

    def __exit__(self, *_exc: object) -> None:
        self.close()


def pool_config_identity(config: ComputerConfig) -> str:
    """Hash only compatibility inputs; request identity and bearer secrets are excluded."""
    payload = config.model_dump(
        mode="json",
        exclude={"run_id", "request_id", "vnc_password"},
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def validate_first_frame(
    payload: bytes,
    *,
    expected_width: int,
    expected_height: int,
    image_format: str,
) -> bytes:
    if not payload:
        raise ValueError("first frame is empty")
    try:
        from PIL import Image

        with Image.open(BytesIO(payload)) as image:
            image.load()
            actual_format = (image.format or "").lower()
            actual_size = image.size
    except Exception as exc:
        raise ValueError("first frame could not be decoded") from exc
    expected_format = image_format.lower().replace("jpg", "jpeg")
    if actual_format.lower().replace("jpg", "jpeg") != expected_format:
        raise ValueError("first frame content type does not match the requested format")
    if actual_size != (expected_width, expected_height):
        raise ValueError("first frame geometry does not match the configured desktop")
    return payload


def estimate_warm_idle_cost(
    entry: WarmPoolEntry,
    *,
    claimed_at: datetime,
    configured_pool_size: int,
) -> dict[str, Any]:
    idle_seconds = max(0.0, (_as_utc(claimed_at) - entry.ready_at).total_seconds())
    region_multiplier = _modal_region_pricing_multiplier(entry.requested_region)
    cpu_core_seconds = idle_seconds * entry.cpu if entry.cpu is not None else None
    memory_gib = entry.memory_mib / 1024.0 if entry.memory_mib is not None else None
    memory_gib_seconds = idle_seconds * memory_gib if memory_gib is not None else None
    components: list[dict[str, Any]] = []
    if cpu_core_seconds is not None:
        components.append(
            {
                "resource": "cpu",
                "quantity": cpu_core_seconds,
                "quantity_unit": "core_seconds",
                "amount": cpu_core_seconds * _MODAL_CPU_RATE * region_multiplier,
            }
        )
    if memory_gib_seconds is not None:
        components.append(
            {
                "resource": "memory",
                "quantity": memory_gib_seconds,
                "quantity_unit": "GiB_seconds",
                "amount": memory_gib_seconds * _MODAL_MEMORY_RATE * region_multiplier,
            }
        )
    complete = len(components) == 2
    return {
        "pool_name": entry.pool_name,
        "config_identity": entry.config_identity,
        "requested_region": entry.requested_region,
        "actual_region": entry.actual_region,
        "configured_pool_size": configured_pool_size,
        "idle_resource_seconds": idle_seconds,
        "cpu_core_seconds": cpu_core_seconds,
        "memory_gib_seconds": memory_gib_seconds,
        "estimated_cost": {
            "status": "estimated" if complete else "partial",
            "currency": "USD",
            "total": sum(float(item["amount"]) for item in components),
            "components": components,
            "region_multiplier": region_multiplier,
            "source_url": "https://modal.com/products/sandboxes",
            "region_pricing_source_url": "https://modal.com/docs/guide/region-selection#pricing",
            "pricing_retrieved_date": "2026-07-18",
        },
        "billed_cost": {
            "status": "pending_reconciliation",
            "source": "modal.Workspace.billing.report or modal.Environment.billing.report",
        },
    }


def estimate_pool_idle_cost(
    entries: list[WarmPoolEntry],
    *,
    observed_at: datetime,
    configured_pool_size: int,
) -> dict[str, Any]:
    per_slot = [
        estimate_warm_idle_cost(
            entry,
            claimed_at=observed_at,
            configured_pool_size=configured_pool_size,
        )
        for entry in entries
    ]
    cpu_seconds = sum(float(item["cpu_core_seconds"] or 0.0) for item in per_slot)
    memory_seconds = sum(float(item["memory_gib_seconds"] or 0.0) for item in per_slot)
    estimated_total = sum(float(item["estimated_cost"]["total"] or 0.0) for item in per_slot)
    estimates_complete = all(item["estimated_cost"]["status"] == "estimated" for item in per_slot)
    return {
        "configured_pool_size": configured_pool_size,
        "observed_ready_slots": len(entries),
        "idle_resource_seconds": sum(float(item["idle_resource_seconds"]) for item in per_slot),
        "cpu_core_seconds": cpu_seconds,
        "memory_gib_seconds": memory_seconds,
        "estimated_cost": {
            "status": (
                "estimated"
                if len(entries) == configured_pool_size and estimates_complete
                else "partial"
            ),
            "currency": "USD",
            "total": estimated_total,
            "source_url": "https://modal.com/products/sandboxes",
            "pricing_retrieved_date": "2026-07-18",
        },
        "billed_cost": {
            "status": "pending_reconciliation",
            "source": "modal.Workspace.billing.report or modal.Environment.billing.report",
        },
    }


def _modal_region_pricing_multiplier(requested_region: str | None) -> float:
    if requested_region is None:
        return 1.0
    return 1.75 if "-" in requested_region else 1.5


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("warm pool timestamps must be ISO-8601 strings")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _as_utc(parsed)


def _utc_iso(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    return int(value)
