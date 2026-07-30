from __future__ import annotations

import math
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from modal_computer_use.daemon.errors import DaemonError

LEASE_ID_HEADER = "x-computer-use-lease-id"
LEASE_EPOCH_HEADER = "x-computer-use-lease-epoch"
LEASE_FENCE_HEADER = "x-computer-use-lease-fence"
LEASE_TOKEN_HEADER = "x-computer-use-lease-token"  # noqa: S105
LEASE_PROTOCOL_VERSION = "1"

_DEFAULT_TTL_SECONDS = 30.0
_DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 10.0

LeaseState = Literal["free", "active", "released", "expired"]


class _Headers(Protocol):
    def get(self, key: str, default: str | None = None) -> str | None: ...


@dataclass(frozen=True, slots=True)
class LeaseCredentials:
    lease_id: str
    epoch: str
    fence: int
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class LeaseGrant:
    lease_id: str
    run_id: str = field(repr=False)
    epoch: str
    fence: int
    token: str = field(repr=False)
    ttl_seconds: float
    heartbeat_interval_seconds: float

    def public_payload(self) -> dict[str, Any]:
        return {
            "state": "active",
            "lease_id": self.lease_id,
            "run_id": self.run_id,
            "daemon_epoch": self.epoch,
            "fence": self.fence,
            "ttl_seconds": self.ttl_seconds,
            "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
        }


@dataclass(frozen=True, slots=True)
class MutationLease:
    run_id: str = field(repr=False)
    epoch: str
    fence: int


@dataclass(slots=True)
class _Lease:
    lease_id: str
    run_id: str = field(repr=False)
    fence: int
    token: str = field(repr=False)
    expires_at: float


class LeaseCoordinator:
    """Daemon-private trajectory ownership state.

    Callers serialize all methods with the daemon input lock. Keeping lease
    transitions on that lock makes activation and release fencing boundaries
    for already-admitted mutations.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        heartbeat_interval_seconds: float = _DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self.epoch = f"epoch_{secrets.token_urlsafe(18)}"
        self.ttl_seconds = ttl_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self._clock = clock
        self._state: LeaseState = "free"
        self._fence = 0
        self._lease: _Lease | None = None
        self._run_state: Literal["none", "active", "released", "interrupted"] = "none"

    def acquire(self, run_id: str) -> LeaseGrant:
        if not run_id.strip():
            raise DaemonError(
                "application run ID must be non-empty",
                status_code=422,
                code="invalid_run_id",
            )
        self._expire_if_needed()
        if self._state == "active":
            remaining = self._remaining_seconds()
            raise DaemonError(
                "trajectory session is busy",
                status_code=409,
                code="session_busy",
                details={
                    "retry_after_seconds": min(
                        max(1, math.ceil(remaining)),
                        math.ceil(self.ttl_seconds),
                    )
                },
            )
        self._fence += 1
        token = secrets.token_urlsafe(32)
        lease = _Lease(
            lease_id=f"lease_{secrets.token_urlsafe(18)}",
            run_id=run_id,
            fence=self._fence,
            token=token,
            expires_at=self._clock() + self.ttl_seconds,
        )
        self._lease = lease
        self._state = "active"
        self._run_state = "active"
        return LeaseGrant(
            lease_id=lease.lease_id,
            run_id=lease.run_id,
            epoch=self.epoch,
            fence=lease.fence,
            token=token,
            ttl_seconds=self.ttl_seconds,
            heartbeat_interval_seconds=self.heartbeat_interval_seconds,
        )

    def heartbeat(self, credentials: LeaseCredentials | None) -> dict[str, Any]:
        self._require_owner(credentials)
        assert self._lease is not None
        self._lease.expires_at = self._clock() + self.ttl_seconds
        return self.status()

    def release(self, credentials: LeaseCredentials | None) -> dict[str, Any]:
        self._require_owner(credentials)
        self._state = "released"
        self._run_state = "released"
        return self.status()

    def release_validated(self, admitted: MutationLease) -> dict[str, Any]:
        """Release an already-admitted lease without reevaluating its TTL."""
        lease = self._lease
        matches = bool(
            self._state == "active"
            and lease is not None
            and admitted.epoch == self.epoch
            and admitted.fence == lease.fence
            and secrets.compare_digest(admitted.run_id, lease.run_id)
        )
        if not matches:
            raise DaemonError(
                "admitted trajectory lease is no longer current",
                status_code=409,
                code="lease_stale",
            )
        self._state = "released"
        self._run_state = "released"
        return self.status()

    def reset_after_owner_recovery(self) -> None:
        self._expire_if_needed()
        if self._state == "active":
            self._state = "released"
            self._run_state = "interrupted"

    def validate_mutation(self, credentials: LeaseCredentials | None) -> MutationLease | None:
        self._expire_if_needed()
        if self._state == "active":
            self._require_owner(credentials)
            assert self._lease is not None
            return MutationLease(
                run_id=self._lease.run_id,
                epoch=self.epoch,
                fence=self._lease.fence,
            )
        if credentials is None:
            return None
        self._raise_inactive_or_stale()
        return None

    def authenticate_last_released(
        self, credentials: LeaseCredentials | None
    ) -> MutationLease | None:
        """Authenticate the immediately released lease without changing time-based state."""
        lease = self._lease
        if (
            self._state != "released"
            or self._run_state != "released"
            or lease is None
            or not self._credentials_match(lease, credentials)
        ):
            return None
        return MutationLease(
            run_id=lease.run_id,
            epoch=self.epoch,
            fence=lease.fence,
        )

    def status(self) -> dict[str, Any]:
        self._expire_if_needed()
        lease = self._lease
        return {
            "protocol_version": LEASE_PROTOCOL_VERSION,
            "state": self._state,
            "run_id": lease.run_id if lease is not None else None,
            "daemon_epoch": self.epoch,
            "fence": self._fence,
            "lease_id": lease.lease_id if lease is not None else None,
            "expires_in_seconds": (
                round(self._remaining_seconds(), 3) if self._state == "active" else 0.0
            ),
            "run_state": self._run_state,
        }

    def _require_owner(self, credentials: LeaseCredentials | None) -> None:
        self._expire_if_needed()
        if self._state != "active":
            self._raise_inactive_or_stale()
        if credentials is None:
            raise DaemonError(
                "an active trajectory lease is required",
                status_code=409,
                code="lease_required",
            )
        lease = self._lease
        assert lease is not None
        if not self._credentials_match(lease, credentials):
            raise DaemonError(
                "trajectory lease credentials are stale",
                status_code=409,
                code="lease_stale",
            )

    def _credentials_match(
        self,
        lease: _Lease,
        credentials: LeaseCredentials | None,
    ) -> bool:
        return bool(
            credentials is not None
            and credentials.lease_id == lease.lease_id
            and credentials.epoch == self.epoch
            and credentials.fence == lease.fence
            and secrets.compare_digest(credentials.token, lease.token)
        )

    def _raise_inactive_or_stale(self) -> None:
        if self._state == "expired":
            code = "lease_expired"
            message = "trajectory lease has expired"
        elif self._state == "released":
            code = "lease_released"
            message = "trajectory lease has been released"
        else:
            code = "lease_stale"
            message = "trajectory lease credentials are stale"
        raise DaemonError(message, status_code=409, code=code)

    def _expire_if_needed(self) -> None:
        if (
            self._state == "active"
            and self._lease is not None
            and self._clock() >= self._lease.expires_at
        ):
            self._state = "expired"
            self._run_state = "interrupted"

    def _remaining_seconds(self) -> float:
        if self._lease is None:
            return 0.0
        return max(0.0, self._lease.expires_at - self._clock())


def lease_credentials_from_headers(headers: _Headers) -> LeaseCredentials | None:
    raw = {
        "lease_id": headers.get(LEASE_ID_HEADER),
        "epoch": headers.get(LEASE_EPOCH_HEADER),
        "fence": headers.get(LEASE_FENCE_HEADER),
        "token": headers.get(LEASE_TOKEN_HEADER),
    }
    if not any(value is not None for value in raw.values()):
        return None
    if not all(isinstance(value, str) and value for value in raw.values()):
        raise DaemonError(
            "trajectory lease credentials are incomplete",
            status_code=409,
            code="lease_stale",
        )
    try:
        fence = int(raw["fence"])
    except (TypeError, ValueError) as exc:
        raise DaemonError(
            "trajectory lease credentials are invalid",
            status_code=409,
            code="lease_stale",
        ) from exc
    return LeaseCredentials(
        lease_id=str(raw["lease_id"]),
        epoch=str(raw["epoch"]),
        fence=fence,
        token=str(raw["token"]),
    )


def validate_mutation_headers(state: Any, headers: _Headers) -> None:
    coordinator: LeaseCoordinator | None = getattr(state, "lease_coordinator", None)
    if coordinator is None:
        return
    coordinator.validate_mutation(lease_credentials_from_headers(headers))
