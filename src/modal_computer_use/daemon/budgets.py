from __future__ import annotations

import time
from collections import deque
from typing import Literal

from fastapi import Request

from modal_computer_use.daemon.errors import DaemonError

BudgetKind = Literal["actions", "screenshots", "artifacts", "recordings", "all"]


def snapshot(request: Request) -> dict[str, int | float | None]:
    settings = request.app.state.settings
    artifact_bytes = sum((item.size_bytes or 0) for item in request.app.state.artifacts.list())
    recording_bytes = request.app.state.recordings.total_size_bytes()
    return {
        "actions": request.app.state.action_count,
        "max_actions": settings.max_actions,
        "screenshots": request.app.state.screenshot_count,
        "max_screenshots": settings.max_screenshots,
        "artifact_bytes": artifact_bytes + recording_bytes,
        "max_artifact_bytes": settings.max_artifact_bytes,
        "recording_seconds": request.app.state.recordings.total_duration_seconds(),
        "max_recording_seconds": settings.max_recording_seconds,
    }


def enforce(request: Request, *kinds: BudgetKind) -> None:
    settings = request.app.state.settings
    checks = set(kinds or ("all",))
    state = snapshot(request)
    if "all" in checks:
        checks.update(("actions", "screenshots", "artifacts", "recordings"))

    if (
        "actions" in checks
        and settings.max_actions is not None
        and request.app.state.action_count > settings.max_actions
    ):
        raise _budget_error("action budget exceeded", state)
    if (
        "screenshots" in checks
        and settings.max_screenshots is not None
        and request.app.state.screenshot_count > settings.max_screenshots
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
        and request.app.state.recordings.total_duration_seconds()
        > settings.max_recording_seconds
    ):
        raise _budget_error("recording duration budget exceeded", state)


def enforce_artifact_write(request: Request, path: str, incoming_size: int) -> None:
    settings = request.app.state.settings
    if settings.max_artifact_bytes is None:
        return
    state = snapshot(request)
    existing_size = 0
    try:
        target = request.app.state.artifacts.resolve(path)
    except Exception:
        target = None
    if target is not None and target.is_file():
        existing_size = target.stat().st_size
    projected = int(state["artifact_bytes"] or 0) - existing_size + incoming_size
    if projected > settings.max_artifact_bytes:
        projected_state = dict(state)
        projected_state["artifact_bytes"] = projected
        raise _budget_error("artifact byte budget exceeded", projected_state)


def reserve_action(request: Request) -> None:
    error = action_reservation_error(request)
    if error is not None:
        raise error
    request.app.state.action_count += 1
    _record_action_rate(request)


def action_reservation_error(request: Request) -> DaemonError | None:
    settings = request.app.state.settings
    if settings.max_actions is not None and request.app.state.action_count >= settings.max_actions:
        return _budget_error("action budget exceeded", snapshot(request))
    rate_limit = settings.input_rate_limit_per_sec
    if rate_limit > 0:
        now = time.monotonic()
        window = request.app.state.action_rate_window
        _prune_action_rate_window(window, now=now)
        if len(window) >= rate_limit:
            return DaemonError(
                "action rate limit exceeded",
                status_code=429,
                code="rate_limited",
                details={
                    "rate_limit_per_sec": rate_limit,
                    "retry_after_seconds": 1,
                    "budgets": snapshot(request),
                },
            )
    return None


def screenshot_reservation_error(request: Request) -> DaemonError | None:
    settings = request.app.state.settings
    if (
        settings.max_screenshots is not None
        and request.app.state.screenshot_count >= settings.max_screenshots
    ):
        return _budget_error("screenshot budget exceeded", snapshot(request))
    return None


def reserve_screenshot(request: Request) -> None:
    error = screenshot_reservation_error(request)
    if error is not None:
        raise error
    request.app.state.screenshot_count += 1


def _budget_error(message: str, state: dict[str, int | float | None]) -> DaemonError:
    return DaemonError(
        message,
        status_code=429,
        code="budget_exceeded",
        details={"budgets": state},
    )


def _record_action_rate(request: Request) -> None:
    rate_limit = request.app.state.settings.input_rate_limit_per_sec
    if rate_limit <= 0:
        return
    now = time.monotonic()
    window = request.app.state.action_rate_window
    _prune_action_rate_window(window, now=now)
    window.append(now)


def _prune_action_rate_window(window: deque[float], *, now: float) -> None:
    while window and now - window[0] >= 1:
        window.popleft()
