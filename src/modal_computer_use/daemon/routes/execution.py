from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from fastapi import Request

from modal_computer_use.daemon.budget_policy import BudgetKind, BudgetPolicy
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.routes.validation import (
    ensure_desktop_ready,
    mark_desktop_ready,
    mutation_lock,
    ready_input_lock,
    ready_mutation_lock,
)
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
    semantic_data: Any,
    fallback_code: str = "action_failed",
    fallback_message: str = "input action failed",
    token_cost: int = 1,
) -> T:
    await ensure_desktop_ready(request)
    policy = budget_policy(request)
    # The early check is advisory for non-rate budgets. The token bucket is
    # refilled and consumed only while the input lock is held below.
    error = policy.action_reservation_error()
    if error is not None:
        raise error
    async with ready_mutation_lock(request, semantic_data=semantic_data):
        policy.reserve_action_with_cost(token_cost=token_cost)
        result = await operation()
        if _is_indeterminate_action_result(result):
            raise_for_failed_action_result(
                result,
                fallback_code=fallback_code,
                fallback_message=fallback_message,
            )
    return raise_for_failed_action_result(
        result,
        fallback_code=fallback_code,
        fallback_message=fallback_message,
    )


def _is_indeterminate_action_result(result: Any) -> bool:
    if not isinstance(result, ActionResult) or result.ok or not isinstance(result.output, dict):
        return False
    code = result.output.get("code")
    return bool(
        result.output.get("indeterminate") is True
        or code == "input_may_be_partial"
        or (isinstance(code, str) and code.endswith("_indeterminate"))
    )


async def run_screenshot_capture[T](
    request: Request,
    operation: Callable[[], Awaitable[T]],
    *,
    mutation_semantic_data: Any = None,
) -> T:
    result, _timing = await run_screenshot_capture_with_timing(
        request,
        operation,
        mutation_semantic_data=mutation_semantic_data,
    )
    return result


async def run_screenshot_capture_with_timing[T](
    request: Request,
    operation: Callable[[], Awaitable[T]],
    *,
    mutation_semantic_data: Any = None,
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
    lock = (
        ready_mutation_lock(request, semantic_data=mutation_semantic_data)
        if mutation_semantic_data is not None
        else ready_input_lock(request)
    )
    async with lock:
        lock_wait_ms = (perf_counter() - lock_wait_started) * 1000
        error = policy.screenshot_reservation_error()
        if error is not None:
            raise error
        policy.reserve_screenshot()
        operation_started = perf_counter()
        result = await operation()
        operation_ms = (perf_counter() - operation_started) * 1000
        policy.enforce("screenshots", "artifacts")
        mark_desktop_ready(request.app.state)
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
    semantic_data: Any,
    rollback: Callable[[T], None] | None = None,
) -> T:
    await ensure_desktop_ready(request)
    policy = budget_policy(request)
    error = policy.recording_start_error()
    if error is not None:
        raise error
    async with mutation_lock(request, semantic_data=semantic_data):
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
    semantic_data: Any,
    enforce_after: tuple[BudgetKind, ...] = (),
    rollback: Callable[[T], None] | None = None,
    after_success: Callable[[T], None] | None = None,
) -> T:
    policy = budget_policy(request)
    async with mutation_lock(request, semantic_data=semantic_data):
        policy.enforce_idle()
        result = await operation()
        try:
            if enforce_after:
                policy.enforce(*enforce_after)
        except DaemonError:
            if rollback is not None:
                rollback(result)
            raise
        if after_success is not None:
            after_success(result)
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
