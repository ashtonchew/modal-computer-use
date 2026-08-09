from __future__ import annotations

import math
import time
from typing import Any, Literal

from modal_computer_use.daemon.errors import DaemonError

BudgetKind = Literal["actions", "screenshots", "artifacts", "recordings", "idle", "all"]


class BudgetPolicy:
    def __init__(self, state: Any) -> None:
        self._state = state

    def snapshot(self) -> dict[str, int | float | None]:
        settings = self._state.settings
        artifact_bytes = self._state.artifacts.total_public_bytes()
        recording_bytes = self._state.recordings.total_size_bytes()
        idle_seconds = time.monotonic() - self._state.last_activity_at
        return {
            "actions": self._state.action_count,
            "max_actions": settings.max_actions,
            "screenshots": self._state.screenshot_count,
            "max_screenshots": settings.max_screenshots,
            "artifact_bytes": artifact_bytes + recording_bytes,
            "max_artifact_bytes": settings.max_artifact_bytes,
            "recording_seconds": self._state.recordings.total_duration_seconds(),
            "max_recording_seconds": settings.max_recording_seconds,
            "idle_seconds": idle_seconds,
            "max_idle_seconds": settings.max_idle_seconds,
        }

    def enforce(self, *kinds: BudgetKind) -> None:
        settings = self._state.settings
        checks = set(kinds or ("all",))
        state = self.snapshot()
        if "all" in checks:
            checks.update(("actions", "screenshots", "artifacts", "recordings", "idle"))

        if (
            "actions" in checks
            and settings.max_actions is not None
            and self._state.action_count > settings.max_actions
        ):
            raise _budget_error("action budget exceeded", state)
        if (
            "screenshots" in checks
            and settings.max_screenshots is not None
            and self._state.screenshot_count > settings.max_screenshots
        ):
            raise _budget_error("screenshot budget exceeded", state)
        if (
            "artifacts" in checks
            and settings.max_artifact_bytes is not None
            and state["artifact_bytes"] > settings.max_artifact_bytes
        ):
            raise _budget_error("artifact byte budget exceeded", state)
        if (
            "recordings" in checks
            and settings.max_recording_seconds is not None
            and self._state.recordings.total_duration_seconds() > settings.max_recording_seconds
        ):
            raise _budget_error("recording duration budget exceeded", state)
        if (
            "idle" in checks
            and settings.max_idle_seconds is not None
            and state["idle_seconds"] is not None
            and state["idle_seconds"] > settings.max_idle_seconds
        ):
            raise _budget_error("idle time budget exceeded", state)

    def enforce_idle(self) -> None:
        self.enforce("idle")

    def touch_activity(self) -> None:
        self._state.last_activity_at = time.monotonic()

    def recording_start_error(self) -> DaemonError | None:
        settings = self._state.settings
        idle_error = self.idle_reservation_error()
        if idle_error is not None:
            return idle_error
        if settings.max_recording_seconds is None:
            return None
        state = self.snapshot()
        if self._state.recordings.total_duration_seconds() >= settings.max_recording_seconds:
            return _budget_error("recording duration budget exceeded", state)
        return None

    def enforce_artifact_write(self, path: str, incoming_size: int) -> None:
        settings = self._state.settings
        self.enforce_idle()
        if settings.max_artifact_bytes is None:
            return
        state = self.snapshot()
        existing_size = 0
        try:
            target = self._state.artifacts.resolve(path)
        except Exception:
            target = None
        if target is not None and target.is_file():
            existing_size = target.stat().st_size
        projected = int(state["artifact_bytes"] or 0) - existing_size + incoming_size
        if projected > settings.max_artifact_bytes:
            projected_state = dict(state)
            projected_state["artifact_bytes"] = projected
            raise _budget_error("artifact byte budget exceeded", projected_state)

    def reserve_action(self) -> None:
        self.reserve_action_with_cost(token_cost=1)

    def reserve_action_with_cost(self, *, token_cost: int) -> None:
        error = self.action_reservation_error(token_cost=token_cost)
        if error is not None:
            raise error
        self._state.action_count += 1
        self._reserve_input_tokens(token_cost)
        self.touch_activity()

    def action_reservation_error(
        self,
        *,
        count: int = 1,
        token_cost: int = 0,
    ) -> DaemonError | None:
        settings = self._state.settings
        idle_error = self.idle_reservation_error()
        if idle_error is not None:
            return idle_error
        if (
            settings.max_actions is not None
            and self._state.action_count + count > settings.max_actions
        ):
            return _budget_error("action budget exceeded", self.snapshot())
        if token_cost:
            return self.input_token_reservation_error(token_cost=token_cost)
        return None

    def input_token_reservation_error(self, *, token_cost: int) -> DaemonError | None:
        settings = self._state.settings
        if settings.input_rate_limit_per_sec <= 0 or token_cost <= 0:
            return None
        if token_cost > settings.input_rate_limit_burst:
            return DaemonError(
                "input cost exceeds configured burst capacity",
                status_code=422,
                code="input_cost_exceeds_burst",
                details={
                    "required_tokens": token_cost,
                    "bucket_capacity": settings.input_rate_limit_burst,
                    "retry_safe": True,
                    "emission_state": "not_started",
                },
            )
        wait = self._state.input_token_bucket.reservation_error(token_cost)
        if wait is None:
            return None
        retry_after_seconds = max(1, math.ceil(wait.retry_after_ms / 1_000))
        return DaemonError(
            "input rate limit exceeded",
            status_code=429,
            code="rate_limited",
            details={
                "required_tokens": token_cost,
                "available_tokens": round(wait.available_tokens, 3),
                "refill_tokens_per_second": settings.input_rate_limit_per_sec,
                "bucket_capacity": settings.input_rate_limit_burst,
                "retry_after_ms": wait.retry_after_ms,
                "retry_safe": True,
                "emission_state": "not_started",
                "budgets": self.snapshot(),
            },
            headers={"Retry-After": str(retry_after_seconds)},
        )

    def reserve_input_tokens(self, *, token_cost: int) -> None:
        error = self.input_token_reservation_error(token_cost=token_cost)
        if error is not None:
            raise error
        self._reserve_input_tokens(token_cost)

    def refund_input_tokens(self, *, token_cost: int) -> None:
        self._state.input_token_bucket.refund(token_cost)

    def screenshot_reservation_error(self, *, count: int = 1) -> DaemonError | None:
        settings = self._state.settings
        idle_error = self.idle_reservation_error()
        if idle_error is not None:
            return idle_error
        if (
            settings.max_screenshots is not None
            and self._state.screenshot_count + count > settings.max_screenshots
        ):
            return _budget_error("screenshot budget exceeded", self.snapshot())
        return None

    def idle_reservation_error(self) -> DaemonError | None:
        settings = self._state.settings
        if settings.max_idle_seconds is None:
            return None
        state = self.snapshot()
        if state["idle_seconds"] is not None and state["idle_seconds"] > settings.max_idle_seconds:
            return _budget_error("idle time budget exceeded", state)
        return None

    def reserve_screenshot(self) -> None:
        error = self.screenshot_reservation_error()
        if error is not None:
            raise error
        self._state.screenshot_count += 1
        self.touch_activity()

    def _reserve_input_tokens(self, token_cost: int) -> None:
        wait = self._state.input_token_bucket.reserve(token_cost)
        if wait is not None:  # The caller must reserve under the input lock.
            raise RuntimeError("input token reservation changed while locked")


def _budget_error(message: str, state: dict[str, int | float | None]) -> DaemonError:
    return DaemonError(
        message,
        status_code=429,
        code="budget_exceeded",
        details={"budgets": state},
    )
