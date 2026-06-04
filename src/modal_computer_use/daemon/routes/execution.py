from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter

from fastapi import Request

from modal_computer_use.daemon.budget_policy import BudgetKind, BudgetPolicy
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.routes.validation import ensure_desktop_ready, ready_input_lock
from modal_computer_use.models import ActionResult
from modal_computer_use.redaction import sanitize_payload


@dataclass(frozen=True, slots=True)
class ScreenshotCaptureTiming:
    ready_ms: float
    lock_wait_ms: float
    operation_ms: float
    total_ms: float


def budget_policy(request: Request) -> BudgetPolicy:
    return request.app.state.budget_policy


async def run_input_action[T](
    request: Request,
    operation: Callable[[], Awaitable[T]],
    *,
    fallback_code: str = "action_failed",
    fallback_message: str = "input action failed",
) -> T:
    await ensure_desktop_ready(request)
    policy = budget_policy(request)
    error = policy.action_reservation_error()
    if error is not None:
        raise error
    async with ready_input_lock(request):
        policy.reserve_action()
        result = await operation()
    return raise_for_failed_action_result(
        result,
        fallback_code=fallback_code,
        fallback_message=fallback_message,
    )


async def run_screenshot_capture[T](
    request: Request,
    operation: Callable[[], Awaitable[T]],
) -> T:
    result, _timing = await run_screenshot_capture_with_timing(request, operation)
    return result


async def run_screenshot_capture_with_timing[T](
    request: Request,
    operation: Callable[[], Awaitable[T]],
) -> tuple[T, ScreenshotCaptureTiming]:
    total_started = perf_counter()
    ready_started = total_started
    await ensure_desktop_ready(request)
    ready_ms = (perf_counter() - ready_started) * 1000
    policy = budget_policy(request)
    error = policy.screenshot_reservation_error()
    if error is not None:
        raise error
    lock_wait_started = perf_counter()
    async with ready_input_lock(request):
        lock_wait_ms = (perf_counter() - lock_wait_started) * 1000
        error = policy.screenshot_reservation_error()
        if error is not None:
            raise error
        policy.reserve_screenshot()
        operation_started = perf_counter()
        result = await operation()
        operation_ms = (perf_counter() - operation_started) * 1000
        policy.enforce("screenshots", "artifacts")
    timing = ScreenshotCaptureTiming(
        ready_ms=ready_ms,
        lock_wait_ms=lock_wait_ms,
        operation_ms=operation_ms,
        total_ms=(perf_counter() - total_started) * 1000,
    )
    return result, timing


async def run_recording_start[T](
    request: Request,
    operation: Callable[[], Awaitable[T]],
    *,
    rollback: Callable[[T], None] | None = None,
) -> T:
    await ensure_desktop_ready(request)
    policy = budget_policy(request)
    error = policy.recording_start_error()
    if error is not None:
        raise error
    result = await operation()
    try:
        policy.enforce("recordings")
    except DaemonError:
        if rollback is not None:
            rollback(result)
        raise
    policy.touch_activity()
    return result


async def run_idle_only_mutation[T](
    request: Request,
    operation: Callable[[], Awaitable[T]],
    *,
    enforce_after: tuple[BudgetKind, ...] = (),
    rollback: Callable[[T], None] | None = None,
) -> T:
    policy = budget_policy(request)
    policy.enforce_idle()
    result = await operation()
    try:
        if enforce_after:
            policy.enforce(*enforce_after)
    except DaemonError:
        if rollback is not None:
            rollback(result)
        raise
    policy.touch_activity()
    return result


def raise_for_failed_action_result[T](
    result: T,
    *,
    fallback_code: str,
    fallback_message: str,
) -> T:
    if not isinstance(result, ActionResult) or result.ok:
        return result
    output = sanitize_payload(result.output)
    details = output if isinstance(output, dict) else {}
    code = details.get("code") if isinstance(details.get("code"), str) else fallback_code
    message = result.message or details.get("message") or fallback_message
    raise DaemonError(str(message), status_code=400, code=code, details=details)
