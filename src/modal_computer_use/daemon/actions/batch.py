from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from contextlib import suppress
from typing import Any

from modal_computer_use.actions import is_supported_key
from modal_computer_use.daemon.actions.traces import ActionTraceWriter, _redacted_action
from modal_computer_use.daemon.budget_policy import BudgetKind, BudgetPolicy
from modal_computer_use.daemon.errors import DaemonError, public_input_error
from modal_computer_use.daemon.leases import LeaseCredentials, lease_credentials_from_headers
from modal_computer_use.daemon.receipts import (
    ReceiptHandle,
    finish_mutation_receipt,
    operation_sequence_from_headers,
    prepare_mutation_receipt,
    require_new_receipt,
)
from modal_computer_use.daemon.routes.validation import backend_readiness, mark_desktop_ready
from modal_computer_use.errors import BudgetExceededError
from modal_computer_use.models import (
    ActionBatchRequest,
    ActionBatchResult,
    ActionBatchTiming,
    ActionItemResult,
    ActionResult,
    ClickAction,
    CursorPositionAction,
    DoubleClickAction,
    DragAction,
    HoldKeyAction,
    HotkeyAction,
    KeyPressAction,
    MouseDownAction,
    MouseUpAction,
    MoveAction,
    Point,
    Region,
    ReleaseAllAction,
    ScreenshotAction,
    ScreenshotOptions,
    ScrollAction,
    TripleClickAction,
    TypeAction,
    ValidationResult,
    WaitAction,
    ZoomAction,
    parse_action,
)
from modal_computer_use.redaction import (
    sanitize_payload,
    sanitize_payload_with_secrets,
    sanitize_text,
)

logger = logging.getLogger("modal_computer_use.daemon.actions")


class _FailedActionResultError(DaemonError):
    """Internal marker for a completed action that returned ok=False."""


class ActionBatchContext:
    def __init__(
        self,
        state: Any,
        headers: Any | None = None,
        *,
        operation_sequence: Any = None,
    ) -> None:
        self.state = state
        self.budget_policy: BudgetPolicy = state.budget_policy
        self.traces = ActionTraceWriter(self)
        self.lease_credentials: LeaseCredentials | None = (
            lease_credentials_from_headers(headers) if headers is not None else None
        )
        self.operation_sequence = (
            operation_sequence_from_headers(headers)
            if headers is not None and operation_sequence is None
            else operation_sequence
        )
        self.receipt_handle: ReceiptHandle | None = None
        self.receipt_finalized = False


async def _ensure_desktop_ready(context: ActionBatchContext, *, force: bool = False) -> None:
    ready, errors = await backend_readiness(context.state, force=force)
    if not context.state.supervisor.running:
        ready = False
        errors = ["desktop supervisor is stopped", *errors]
    if ready:
        return
    raise DaemonError(
        "desktop is not ready",
        status_code=503,
        code="desktop_not_ready",
        details={"errors": errors},
    )


async def validate(payload: ActionBatchRequest, context: ActionBatchContext) -> ValidationResult:
    await _ensure_desktop_ready(context)
    errors = _validate_batch_request(payload, context)
    return ValidationResult(ok=not errors, errors=errors)


async def run(
    payload: ActionBatchRequest,
    context: ActionBatchContext,
    idempotency_key: str | None = None,
) -> ActionBatchResult:
    result, _ = await _run(
        payload,
        context,
        idempotency_key=idempotency_key,
        raw_screenshot_after=False,
    )
    return result


async def run_with_screenshot_bytes(
    payload: ActionBatchRequest,
    context: ActionBatchContext,
    idempotency_key: str | None = None,
):
    if not payload.screenshot_after:
        raise DaemonError(
            "raw action observation requires screenshot_after",
            status_code=422,
            code="missing_screenshot_after",
        )
    return await _run(
        payload,
        context,
        idempotency_key=idempotency_key,
        raw_screenshot_after=True,
    )


async def _run(
    payload: ActionBatchRequest,
    context: ActionBatchContext,
    *,
    idempotency_key: str | None,
    raw_screenshot_after: bool,
):
    try:
        return await _run_impl(
            payload,
            context,
            idempotency_key=idempotency_key,
            raw_screenshot_after=raw_screenshot_after,
        )
    except BaseException as exc:
        if context.receipt_handle is not None and not context.receipt_finalized:
            await finish_mutation_receipt(context.state, context.receipt_handle, exc)
            context.receipt_finalized = True
        raise


async def _run_impl(
    payload: ActionBatchRequest,
    context: ActionBatchContext,
    *,
    idempotency_key: str | None,
    raw_screenshot_after: bool,
):
    effective_idempotency_key = _effective_idempotency_key(payload, idempotency_key)
    request_fingerprint = _request_fingerprint(payload)
    cached = (
        None
        if raw_screenshot_after
        else _cached_idempotency_result(context, effective_idempotency_key, request_fingerprint)
    )
    if cached is not None:
        async with context.state.input_lock:
            cached = _cached_idempotency_result(
                context,
                effective_idempotency_key,
                request_fingerprint,
            )
            if cached is not None:
                handle = await prepare_mutation_receipt(
                    context.state,
                    credentials=context.lease_credentials,
                    sequence=context.operation_sequence,
                    operation_kind="actions.run",
                    semantic_data={
                        "request": payload.model_dump(
                            mode="json", exclude={"idempotency_key"}
                        ),
                        "raw_screenshot_after": raw_screenshot_after,
                    },
                )
                if handle is not None and handle.existing_state is not None:
                    require_new_receipt(handle)
                if handle is not None and handle.existing_state is None:
                    context.receipt_finalized = True
                    await context.state.receipt_journal.complete(
                        handle,
                        classification="known_cached_result",
                    )
                return cached, None
    action_count = _count_action_tree(payload.actions)
    if action_count > context.state.settings.max_batch_actions:
        raise DaemonError(
            "batch exceeds max_batch_actions",
            status_code=413,
            code="batch_too_large",
            details={
                "max_batch_actions": context.state.settings.max_batch_actions,
                "action_count": action_count,
            },
        )
    await _ensure_desktop_ready(context)
    call_id = payload.call_id or f"call_{uuid.uuid4().hex[:12]}"
    validation_errors = _validate_batch_request(payload, context)
    if validation_errors:
        raise DaemonError(
            "action validation failed",
            status_code=422,
            code="action_validation_failed",
            details={"errors": validation_errors},
        )
    batch_start = time.perf_counter()
    results: list[ActionItemResult] = []
    screenshot = None
    screenshot_bytes = None
    batch_timed_out = False
    screenshot_after_blocked = False
    action_phase_failed = False
    lock_was_contended = context.state.input_lock.locked()
    async with context.state.input_lock:
        await context.state.receipt_journal.ensure_mutation_allowed()
        context.state.lease_coordinator.validate_mutation(context.lease_credentials)
        if lock_was_contended:
            await _ensure_desktop_ready(context, force=True)
        if context.lease_credentials is not None:
            _preflight_action_budget(context, payload.actions)
        handle = await prepare_mutation_receipt(
            context.state,
            credentials=context.lease_credentials,
            sequence=context.operation_sequence,
            operation_kind="actions.run",
            semantic_data={
                "request": payload.model_dump(mode="json", exclude={"idempotency_key"}),
                "raw_screenshot_after": raw_screenshot_after,
            },
        )
        require_new_receipt(handle)
        context.receipt_handle = handle
        cache = context.state.idempotency_cache
        cached = (
            None
            if raw_screenshot_after
            else _cached_idempotency_result(context, effective_idempotency_key, request_fingerprint)
        )
        if cached is not None:
            if handle is not None:
                context.receipt_finalized = True
                await context.state.receipt_journal.complete(
                    handle,
                    classification="known_cached_result",
                )
            return cached, None

        batch_timeout_ms = context.state.settings.max_batch_duration_ms
        batch_deadline = time.perf_counter() + (batch_timeout_ms / 1000)
        for index, action in enumerate(payload.actions):
            remaining_batch_seconds = _remaining_seconds(batch_deadline)
            if remaining_batch_seconds <= 0:
                item = _batch_timeout_result(
                    index=index,
                    action_type=action.type,
                    batch_timeout_ms=batch_timeout_ms,
                    elapsed_ms=0,
                )
                results.append(item)
                batch_timed_out = True
                context.traces.append_action(payload, action, item, call_id=call_id, sequence=index)
                logger.info(
                    "action failed",
                    extra={
                        "extra": {
                            "call_id": call_id,
                            "sequence": index,
                            "action": _redacted_action(action),
                            "ok": False,
                            "elapsed_ms": 0,
                            "error_code": "timeout",
                            "timeout_scope": "batch",
                        }
                    },
                )
                break
            start = time.perf_counter()
            timeout_ms = _effective_action_timeout_ms(action, payload, context)
            timeout_seconds = min(timeout_ms / 1000, remaining_batch_seconds)
            timeout_scope = "batch" if timeout_seconds < (timeout_ms / 1000) else "action"
            if _counts_against_action_budget(action):
                budget_item = _reserve_action_budget(context, index=index, action_type=action.type)
                if budget_item is not None:
                    screenshot_after_blocked = True
                    results.append(budget_item)
                    context.traces.append_action(
                        payload, action, budget_item, call_id=call_id, sequence=index
                    )
                    logger.info(
                        "action failed",
                        extra={
                            "extra": {
                                "call_id": call_id,
                                "sequence": index,
                                "action": _redacted_action(action),
                                "ok": False,
                                "elapsed_ms": 0,
                                "error_code": "budget_exceeded",
                            }
                        },
                    )
                    if not payload.continue_on_error:
                        break
                    continue
            try:
                with context.state.tracer.span(
                    "daemon.action",
                    {
                        "action.type": action.type,
                        "action.sequence": index,
                        "call_id": call_id,
                    },
                ):
                    output = await asyncio.wait_for(
                        _execute_action(
                            action,
                            context,
                            call_id=call_id,
                            payload=payload,
                            batch_deadline=batch_deadline,
                            action_path=f"actions[{index}]",
                        ),
                        timeout=timeout_seconds,
                    )
                elapsed_ms = (time.perf_counter() - start) * 1000
                budget_kinds = _budget_kinds_for_action(action)
                if budget_kinds:
                    context.budget_policy.enforce(*budget_kinds)
                item = ActionItemResult(
                    index=index,
                    type=action.type,
                    ok=True,
                    elapsed_ms=elapsed_ms,
                    output=_with_input_backend(output or {}, action, context),
                )
                results.append(item)
                if item.ok:
                    context.budget_policy.touch_activity()
                context.traces.append_action(payload, action, item, call_id=call_id, sequence=index)
                logger.info(
                    "action executed",
                    extra={
                        "extra": {
                            "call_id": call_id,
                            "sequence": index,
                            "action": _redacted_action(action),
                            "ok": True,
                            "elapsed_ms": elapsed_ms,
                        }
                    },
                )
                if _uses_post_action_delay(action) and (
                    payload.screenshot_after or index < len(payload.actions) - 1
                ):
                    await _post_action_delay(context, batch_deadline)
            except TimeoutError:
                elapsed_ms = (time.perf_counter() - start) * 1000
                cleanup = await _shielded_release_all_cleanup(context)
                effective_timeout_ms = batch_timeout_ms if timeout_scope == "batch" else timeout_ms
                timeout_output: dict[str, Any] = {
                    "code": "timeout",
                    "timeout_ms": effective_timeout_ms,
                    "scope": timeout_scope,
                }
                if cleanup is not None:
                    timeout_output["cleanup"] = cleanup
                item = ActionItemResult(
                    index=index,
                    type=action.type,
                    ok=False,
                    elapsed_ms=elapsed_ms,
                    error_code="timeout",
                    error=f"{timeout_scope} timed out after {effective_timeout_ms} ms",
                    output=timeout_output,
                )
                results.append(item)
                batch_timed_out = timeout_scope == "batch"
                context.traces.append_action(payload, action, item, call_id=call_id, sequence=index)
                logger.info(
                    "action failed",
                    extra={
                        "extra": {
                            "call_id": call_id,
                            "sequence": index,
                            "action": _redacted_action(action),
                            "ok": False,
                            "elapsed_ms": elapsed_ms,
                            "error_code": "timeout",
                            "timeout_scope": timeout_scope,
                        }
                    },
                )
                if handle is not None or batch_timed_out or not payload.continue_on_error:
                    action_phase_failed = True
                    break
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - start) * 1000
                cleanup = (
                    None
                    if _is_completed_release_all_incomplete(action, exc)
                    else await _shielded_release_all_cleanup(context)
                )
                error_code = _exception_code(exc)
                if error_code in {"budget_exceeded", "rate_limited"}:
                    screenshot_after_blocked = True
                error = _action_error_message(action, exc)
                output = _exception_output(exc, action, context=context)
                if cleanup is not None:
                    output["cleanup"] = cleanup
                item = ActionItemResult(
                    index=index,
                    type=action.type,
                    ok=False,
                    elapsed_ms=elapsed_ms,
                    error_code=error_code,
                    error=error,
                    output=output,
                )
                results.append(item)
                context.traces.append_action(payload, action, item, call_id=call_id, sequence=index)
                logger.info(
                    "action failed",
                    extra={
                        "extra": {
                            "call_id": call_id,
                            "sequence": index,
                            "action": _redacted_action(action),
                            "ok": False,
                            "elapsed_ms": elapsed_ms,
                            "error_code": error_code,
                        }
                    },
                )
                if handle is not None and _action_item_is_uncertain(item):
                    action_phase_failed = True
                    break
                if (
                    payload.continue_on_error
                    and _uses_post_action_delay(action)
                    and index < len(payload.actions) - 1
                ):
                    await _post_action_delay(context, batch_deadline)
                if not payload.continue_on_error:
                    action_phase_failed = True
                    break
        action_phase_allows_screenshot = (
            payload.continue_on_error and handle is None
        ) or not action_phase_failed
        if (
            payload.screenshot_after
            and action_phase_allows_screenshot
            and not batch_timed_out
            and not screenshot_after_blocked
        ):
            options = payload.screenshot_options or ScreenshotOptions()
            start = time.perf_counter()
            timeout_ms = _effective_screenshot_after_timeout_ms(payload, context)
            remaining_batch_seconds = _remaining_seconds(batch_deadline)
            if remaining_batch_seconds <= 0:
                item = _screenshot_after_timeout_result(
                    index=len(results),
                    elapsed_ms=0,
                    timeout_ms=batch_timeout_ms,
                    scope="batch",
                )
                results.append(item)
                context.traces.append_screenshot_after(payload, None, item, call_id=call_id)
            else:
                timeout_seconds = min(timeout_ms / 1000, remaining_batch_seconds)
                timeout_scope = "batch" if timeout_seconds < (timeout_ms / 1000) else "action"
                try:
                    _enforce_screenshot_options_pixels(
                        context,
                        source_width=context.state.backend.width,
                        source_height=context.state.backend.height,
                        scale=options.scale,
                    )
                    screenshot_budget_error = context.budget_policy.screenshot_reservation_error()
                    if screenshot_budget_error is not None:
                        raise screenshot_budget_error
                    context.budget_policy.reserve_screenshot()
                    if raw_screenshot_after:
                        screenshot_bytes = await asyncio.wait_for(
                            context.state.backend.screenshot_bytes(
                                options,
                                prefer_native_png=True,
                            ),
                            timeout=timeout_seconds,
                        )
                    else:
                        screenshot = await asyncio.wait_for(
                            context.state.backend.screenshot(
                                options,
                                artifact_store=context.state.artifacts,
                                call_id=call_id,
                                retention_class="trace",
                            ),
                            timeout=timeout_seconds,
                        )
                    context.budget_policy.enforce("screenshots", "artifacts")
                    context.budget_policy.touch_activity()
                    context.traces.append_screenshot_after(
                        payload,
                        screenshot,
                        None,
                        call_id=call_id,
                    )
                except TimeoutError:
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    cleanup = await _release_all_cleanup(context)
                    effective_timeout_ms = (
                        batch_timeout_ms if timeout_scope == "batch" else timeout_ms
                    )
                    item = _screenshot_after_timeout_result(
                        index=len(results),
                        elapsed_ms=elapsed_ms,
                        timeout_ms=effective_timeout_ms,
                        scope=timeout_scope,
                    )
                    if cleanup is not None:
                        item.output["cleanup"] = cleanup
                    results.append(item)
                    context.traces.append_screenshot_after(payload, None, item, call_id=call_id)
                except Exception as exc:
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    cleanup = await _release_all_cleanup(context)
                    error_code = _exception_code(exc)
                    failed_screenshot = screenshot
                    screenshot = None
                    output = _exception_output(exc, context=context)
                    if cleanup is not None:
                        output["cleanup"] = cleanup
                    item = ActionItemResult(
                        index=len(results),
                        type="screenshot_after",
                        ok=False,
                        elapsed_ms=elapsed_ms,
                        error_code=error_code,
                        error=_action_error_message(None, exc),
                        output=output,
                    )
                    results.append(item)
                    context.traces.append_screenshot_after(
                        payload, failed_screenshot, item, call_id=call_id
                    )
        result = ActionBatchResult(
            ok=all(item.ok for item in results),
            call_id=call_id,
            results=results,
            screenshot=screenshot,
            timing=ActionBatchTiming(daemon_ms=(time.perf_counter() - batch_start) * 1000),
        )
        if result.ok:
            mark_desktop_ready(context.state)
        cache_enabled = context.state.settings.idempotency_cache_max_entries > 0
        if effective_idempotency_key and cache_enabled and not raw_screenshot_after:
            cache[effective_idempotency_key] = {
                "fingerprint": request_fingerprint,
                "created_at": time.monotonic(),
                "result": result.model_dump(mode="json"),
            }
            _prune_idempotency_cache(context)
        if handle is not None:
            uncertain_item = next(
                (
                    item
                    for item in results
                    if _action_item_is_uncertain(item)
                ),
                None,
            )
            if uncertain_item is not None:
                uncertain_code = uncertain_item.error_code or "action_failed"
                if uncertain_code == "timeout":
                    receipt_error: BaseException = TimeoutError("action batch timed out")
                else:
                    receipt_error = DaemonError(
                        "action dispatch outcome is uncertain",
                        code=uncertain_code,
                        details=uncertain_item.output,
                    )
                context.receipt_finalized = True
                await finish_mutation_receipt(context.state, handle, receipt_error)
            else:
                retry_safe = bool(results) and all(
                    not item.ok
                    and item.output.get("retry_safe") is True
                    and item.output.get("emission_state") == "not_started"
                    for item in results
                )
                context.receipt_finalized = True
                if retry_safe:
                    await context.state.receipt_journal.abandon(
                        handle,
                        classification="not_started",
                    )
                else:
                    await context.state.receipt_journal.complete(handle)
        return result, screenshot_bytes


def _action_item_is_uncertain(item: ActionItemResult) -> bool:
    if item.type == "screenshot_after":
        return False
    code = item.error_code
    return bool(
        code in {"timeout", "input_may_be_partial", "action_failed"}
        or item.output.get("indeterminate") is True
        or (isinstance(code, str) and code.endswith("_indeterminate"))
    )


def _effective_idempotency_key(payload: ActionBatchRequest, header_key: str | None) -> str | None:
    if header_key and payload.idempotency_key and header_key != payload.idempotency_key:
        raise DaemonError(
            "Idempotency-Key header and body idempotency_key differ",
            status_code=409,
            code="idempotency_key_conflict",
        )
    return header_key or payload.idempotency_key


def _request_fingerprint(payload: ActionBatchRequest) -> str:
    encoded = json.dumps(
        payload.model_dump(mode="json", exclude={"idempotency_key"}),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cached_idempotency_result(
    context: ActionBatchContext, key: str | None, fingerprint: str
) -> ActionBatchResult | None:
    if context.state.settings.idempotency_cache_max_entries <= 0:
        context.state.idempotency_cache.clear()
        return None
    _prune_idempotency_cache(context)
    if not key:
        return None
    cache = context.state.idempotency_cache
    if key not in cache:
        return None
    entry = cache[key]
    if entry["fingerprint"] != fingerprint:
        raise DaemonError(
            "idempotency key was already used with a different context body",
            status_code=409,
            code="idempotency_key_conflict",
        )
    cache.move_to_end(key)
    return ActionBatchResult.model_validate(entry["result"])


def _prune_idempotency_cache(context: ActionBatchContext) -> None:
    cache = context.state.idempotency_cache
    settings = context.state.settings
    now = time.monotonic()
    ttl_seconds = settings.idempotency_cache_ttl_seconds
    if ttl_seconds > 0:
        for key in list(cache.keys()):
            if now - cache[key]["created_at"] <= ttl_seconds:
                continue
            cache.pop(key, None)
    max_entries = settings.idempotency_cache_max_entries
    while max_entries > 0 and len(cache) > max_entries:
        cache.popitem(last=False)


def _validate_action_timeouts(payload: ActionBatchRequest, max_action_timeout_ms: int) -> list[str]:
    errors: list[str] = []
    if payload.max_action_timeout_ms is not None and (
        payload.max_action_timeout_ms > max_action_timeout_ms
    ):
        errors.append(
            "max_action_timeout_ms "
            f"{payload.max_action_timeout_ms} exceeds configured maximum {max_action_timeout_ms}"
        )
    for action_path, action in _iter_timeout_actions(payload.actions):
        timeout_ms = getattr(action, "timeout_ms", None)
        if timeout_ms is not None and timeout_ms > max_action_timeout_ms:
            errors.append(
                f"{action_path} timeout_ms {timeout_ms} exceeds configured maximum "
                f"{max_action_timeout_ms}"
            )
    return errors


def _validate_batch_request(payload: ActionBatchRequest, context: ActionBatchContext) -> list[str]:
    errors = _validate_actions(
        payload.actions,
        width=context.state.backend.width,
        height=context.state.backend.height,
    )
    action_count = _count_action_tree(payload.actions)
    if action_count > context.state.settings.max_batch_actions:
        errors.append(
            "batch exceeds max_batch_actions "
            f"{context.state.settings.max_batch_actions} "
            f"with {action_count} total actions"
        )
    errors.extend(_validate_action_timeouts(payload, context.state.settings.max_action_timeout_ms))
    errors.extend(_validate_screenshot_pixel_budget(payload, context))
    return errors


def _validate_screenshot_pixel_budget(
    payload: ActionBatchRequest, context: ActionBatchContext
) -> list[str]:
    errors: list[str] = []
    backend = context.state.backend
    for action_path, action in _iter_action_tree(payload.actions):
        if isinstance(action, ScreenshotAction):
            options = action.options or ScreenshotOptions()
            errors.extend(
                _screenshot_pixel_errors(
                    context,
                    source_width=backend.width,
                    source_height=backend.height,
                    scale=options.scale,
                    label=action_path,
                )
            )
        elif isinstance(action, ZoomAction):
            errors.extend(
                _screenshot_pixel_errors(
                    context,
                    source_width=action.region.width,
                    source_height=action.region.height,
                    scale=action.scale,
                    label=action_path,
                )
            )
    if payload.screenshot_after:
        options = payload.screenshot_options or ScreenshotOptions()
        errors.extend(
            _screenshot_pixel_errors(
                context,
                source_width=backend.width,
                source_height=backend.height,
                scale=options.scale,
                label="screenshot_after",
            )
        )
    return errors


def _screenshot_pixel_errors(
    context: ActionBatchContext,
    *,
    source_width: int,
    source_height: int,
    scale: float,
    label: str,
) -> list[str]:
    output_pixels = round(source_width * scale) * round(source_height * scale)
    max_pixels = context.state.settings.screenshot_max_pixels
    if output_pixels <= max_pixels:
        return []
    return [
        f"{label} screenshot output {output_pixels} pixels exceeds "
        f"max screenshot pixels {max_pixels}"
    ]


def _enforce_screenshot_options_pixels(
    context: ActionBatchContext,
    *,
    source_width: int,
    source_height: int,
    scale: float,
) -> None:
    output_pixels = round(source_width * scale) * round(source_height * scale)
    max_pixels = context.state.settings.screenshot_max_pixels
    if output_pixels > max_pixels:
        raise DaemonError(
            "screenshot exceeds max pixel budget",
            status_code=413,
            code="screenshot_too_large",
            details={"width": round(source_width * scale), "height": round(source_height * scale)},
        )


def _iter_timeout_actions(actions: list[Any], *, path: str = "actions"):
    yield from _iter_action_tree(actions, path=path)


def _iter_action_tree(actions: list[Any], *, path: str = "actions"):
    for index, action in enumerate(actions):
        action_path = f"{path}[{index}]"
        yield action_path, action
        if isinstance(action, HoldKeyAction) and action.actions:
            nested = []
            for nested_action in action.actions:
                with suppress(Exception):
                    nested.append(parse_action(nested_action))
            yield from _iter_action_tree(nested, path=f"{action_path}.actions")


def _count_action_tree(actions: list[Any]) -> int:
    count = 0
    for action in actions:
        count += 1
        if isinstance(action, HoldKeyAction) and action.actions:
            nested = []
            for nested_action in action.actions:
                with suppress(Exception):
                    nested.append(parse_action(nested_action))
            count += _count_action_tree(nested)
    return count


def _effective_action_timeout_ms(
    action: Any, payload: ActionBatchRequest, context: ActionBatchContext
) -> int:
    if action.timeout_ms is not None:
        return action.timeout_ms
    if payload.max_action_timeout_ms is not None:
        return payload.max_action_timeout_ms
    return context.state.settings.default_action_timeout_ms


def _effective_screenshot_after_timeout_ms(
    payload: ActionBatchRequest, context: ActionBatchContext
) -> int:
    if payload.max_action_timeout_ms is not None:
        return payload.max_action_timeout_ms
    return context.state.settings.default_action_timeout_ms


def _remaining_seconds(deadline: float) -> float:
    return max(0, deadline - time.perf_counter())


def _counts_against_action_budget(action: Any) -> bool:
    return not isinstance(action, ScreenshotAction | ZoomAction | CursorPositionAction)


def _budget_kinds_for_action(action: Any) -> tuple[BudgetKind, ...]:
    if isinstance(action, ScreenshotAction | ZoomAction):
        return ("screenshots", "artifacts")
    return ()


def _preflight_action_budget(context: ActionBatchContext, actions: list[Any]) -> None:
    action_count, screenshot_count = _budget_counts(actions)
    if action_count:
        error = context.budget_policy.action_reservation_error(count=action_count)
        if error is not None:
            raise error
    if screenshot_count:
        error = context.budget_policy.screenshot_reservation_error(count=screenshot_count)
        if error is not None:
            raise error


def _budget_counts(actions: list[Any]) -> tuple[int, int]:
    action_count = 0
    screenshot_count = 0
    for action in actions:
        if isinstance(action, ScreenshotAction | ZoomAction):
            screenshot_count += 1
        elif _counts_against_action_budget(action):
            action_count += 1
        if isinstance(action, HoldKeyAction) and action.actions:
            nested_action_count, nested_screenshot_count = _budget_counts(
                _nested_hold_actions(action)
            )
            action_count += nested_action_count
            screenshot_count += nested_screenshot_count
    return action_count, screenshot_count


def _uses_post_action_delay(action: Any) -> bool:
    return not isinstance(action, WaitAction | ScreenshotAction | ZoomAction | CursorPositionAction)


async def _post_action_delay(context: ActionBatchContext, batch_deadline: float) -> None:
    delay_ms = context.state.settings.post_action_delay_ms
    if delay_ms <= 0:
        return
    remaining_seconds = _remaining_seconds(batch_deadline)
    if remaining_seconds <= 0:
        return
    await asyncio.sleep(min(delay_ms / 1000, remaining_seconds))


async def _release_all_cleanup(context: ActionBatchContext) -> dict[str, Any] | None:
    try:
        result = await context.state.backend.release_all()
    except Exception as exc:
        output = _exception_output(exc, context=context)
        return output or {"code": "release_all_failed"}
    if not isinstance(result, ActionResult) or result.ok:
        return None
    output = sanitize_payload(result.output)
    if not isinstance(output, dict):
        return {"code": "release_all_failed"}
    code = output.get("code")
    if not isinstance(code, str) or not code:
        return {"code": "release_all_failed", **output}
    return output


async def _shielded_release_all_cleanup(
    context: ActionBatchContext,
) -> dict[str, Any] | None:
    cleanup_task = asyncio.create_task(_release_all_cleanup(context))
    try:
        return await asyncio.shield(cleanup_task)
    except asyncio.CancelledError:
        with suppress(Exception):
            await cleanup_task
        raise


def _is_completed_release_all_incomplete(action: Any, exc: Exception) -> bool:
    return (
        isinstance(action, ReleaseAllAction)
        and isinstance(exc, _FailedActionResultError)
        and exc.code == "release_all_incomplete"
    )


def _reserve_action_budget(
    context: ActionBatchContext,
    *,
    index: int,
    action_type: str,
) -> ActionItemResult | None:
    error = context.budget_policy.action_reservation_error()
    if error is not None:
        return ActionItemResult(
            index=index,
            type=action_type,
            ok=False,
            elapsed_ms=0,
            error_code=error.code,
            error=error.message,
            output=sanitize_payload({"code": error.code, **error.details}),
        )
    context.budget_policy.reserve_action()
    return None


def _exception_code(exc: Exception) -> str:
    if isinstance(exc, DaemonError):
        return exc.code
    mapped_error = public_input_error(exc)
    if mapped_error is not None:
        return mapped_error.code
    if isinstance(exc, BudgetExceededError):
        return "budget_exceeded"
    return "action_failed"


def _exception_output(
    exc: Exception, action: Any | None = None, *, context: ActionBatchContext | None = None
) -> dict[str, Any]:
    if isinstance(exc, DaemonError):
        output: dict[str, Any] = {"code": exc.code}
        output.update(exc.details)
        sanitized = sanitize_payload(output)
        if isinstance(action, TypeAction):
            sanitized = sanitize_payload_with_secrets(
                sanitized,
                [(action.text, "[redacted typed text]")],
            )
        return sanitized if isinstance(sanitized, dict) else {"code": exc.code}
    mapped_error = public_input_error(exc)
    if mapped_error is not None:
        return {"code": mapped_error.code, **mapped_error.details}
    if isinstance(exc, BudgetExceededError):
        output = {"code": "budget_exceeded"}
        if context is not None:
            output["budgets"] = context.budget_policy.snapshot()
        return output
    return {}


def _action_error_message(action: Any | None, exc: Exception) -> str:
    mapped_error = public_input_error(exc)
    message = sanitize_text(mapped_error.message if mapped_error is not None else str(exc))
    for text in _typed_texts_for_action(action):
        message = message.replace(text, "[redacted typed text]")
    return message


def _typed_texts_for_action(action: Any) -> list[str]:
    if isinstance(action, TypeAction):
        return [action.text]
    if isinstance(action, HoldKeyAction) and action.actions:
        texts: list[str] = []
        for nested_action in action.actions:
            with suppress(Exception):
                texts.extend(_typed_texts_for_action(parse_action(nested_action)))
        return texts
    return []


def _redact_type_payload(value: Any, action: TypeAction) -> Any:
    if isinstance(value, str):
        return value.replace(action.text, "[redacted typed text]")
    if isinstance(value, list):
        return [_redact_type_payload(item, action) for item in value]
    if isinstance(value, dict):
        return {
            ("redacted_text" if key == "text" else key): _redact_type_payload(item, action)
            for key, item in value.items()
        }
    return value


def _sanitize_secret_output(
    output: dict[str, Any], *, secret: str, replacement: str
) -> dict[str, Any]:
    sanitized = sanitize_payload_with_secrets(output, [(secret, replacement)])
    return sanitized if isinstance(sanitized, dict) else {}


def _checked_action_result(result: ActionResult, action: Any) -> dict[str, Any]:
    output = (
        _sanitize_secret_output(
            result.output,
            secret=action.text,
            replacement="[redacted typed text]",
        )
        if isinstance(action, TypeAction)
        else sanitize_payload(result.output)
    )
    if not isinstance(output, dict):
        output = {}
    if result.ok:
        return output
    code = output.get("code") if isinstance(output.get("code"), str) else "action_failed"
    message = result.message or output.get("message") or f"{action.type} failed"
    if isinstance(action, TypeAction):
        message = sanitize_payload_with_secrets(
            str(message),
            [(action.text, "[redacted typed text]")],
        )
    raise _FailedActionResultError(
        sanitize_text(str(message)),
        code=code,
        details=output,
    )


def _batch_timeout_result(
    *,
    index: int,
    action_type: str,
    batch_timeout_ms: int,
    elapsed_ms: float,
) -> ActionItemResult:
    return ActionItemResult(
        index=index,
        type=action_type,
        ok=False,
        elapsed_ms=elapsed_ms,
        error_code="timeout",
        error=f"batch timed out after {batch_timeout_ms} ms",
        output={"code": "timeout", "timeout_ms": batch_timeout_ms, "scope": "batch"},
    )


def _screenshot_after_timeout_result(
    *,
    index: int,
    elapsed_ms: float,
    timeout_ms: int,
    scope: str,
) -> ActionItemResult:
    return ActionItemResult(
        index=index,
        type="screenshot_after",
        ok=False,
        elapsed_ms=elapsed_ms,
        error_code="timeout",
        error=f"{scope} timed out after {timeout_ms} ms",
        output={"code": "timeout", "timeout_ms": timeout_ms, "scope": scope},
    )


async def _execute_action(
    action: Any,
    context: ActionBatchContext,
    *,
    call_id: str,
    payload: ActionBatchRequest | None = None,
    batch_deadline: float | None = None,
    action_path: str | None = None,
) -> dict[str, Any]:
    backend = context.state.backend
    if isinstance(action, MoveAction):
        point = await backend.mouse_move(action.x, action.y)
        return point.model_dump()
    if isinstance(action, (ClickAction, DoubleClickAction, TripleClickAction)):
        count = 1
        if isinstance(action, DoubleClickAction):
            count = 2
        if isinstance(action, TripleClickAction):
            count = 3
        point = await backend.mouse_click(
            action.x,
            action.y,
            button=action.button,
            count=count,
            modifiers=action.modifiers,
        )
        return point.model_dump()
    if isinstance(action, DragAction):
        start = (
            Point(x=action.start_x, y=action.start_y)
            if action.start_x is not None and action.start_y is not None
            else None
        )
        end = (
            Point(x=action.end_x, y=action.end_y)
            if action.end_x is not None and action.end_y is not None
            else None
        )
        point = await backend.mouse_drag(
            start=start,
            end=end,
            path=action.path,
            button=action.button,
            duration_ms=action.duration_ms,
            modifiers=action.modifiers,
        )
        return point.model_dump()
    if isinstance(action, ScrollAction):
        result = await backend.mouse_scroll(
            action.direction,
            action.amount,
            x=action.x,
            y=action.y,
        )
        return _checked_action_result(result, action)
    if isinstance(action, MouseDownAction | MouseUpAction):
        if action.type == "mouse_down":
            result = await backend.mouse_down(action.button, action.x, action.y)
            return _checked_action_result(result, action)
        result = await backend.mouse_up(action.button, action.x, action.y)
        return _checked_action_result(result, action)
    if isinstance(action, TypeAction):
        result = await backend.keyboard_type(action.text, action.delay_ms, action.method)
        return _checked_action_result(result, action)
    if isinstance(action, KeyPressAction):
        result = await backend.keyboard_press(
            action.key,
            modifiers=action.modifiers,
            duration_ms=action.duration_ms,
        )
        return _checked_action_result(result, action)
    if isinstance(action, HotkeyAction):
        result = await backend.keyboard_hotkey(action.keys, duration_ms=action.duration_ms)
        return _checked_action_result(result, action)
    if isinstance(action, HoldKeyAction):
        nested_actions = _nested_hold_actions(action)
        _preflight_action_budget(context, nested_actions)
        await backend.key_down(action.key)
        nested_results: list[dict[str, Any]] = []
        try:
            for nested_index, nested_action in enumerate(nested_actions):
                if _counts_against_action_budget(nested_action):
                    context.budget_policy.reserve_action()
                nested_start = time.perf_counter()
                nested_call = _execute_action(
                    nested_action,
                    context,
                    call_id=call_id,
                    payload=payload,
                    batch_deadline=batch_deadline,
                    action_path=f"{action_path or 'actions'}.actions[{nested_index}]",
                )
                try:
                    if payload is not None:
                        nested_timeout_ms = _effective_action_timeout_ms(
                            nested_action, payload, context
                        )
                        nested_timeout_seconds = nested_timeout_ms / 1000
                        timeout_scope = "action"
                        if batch_deadline is not None:
                            remaining_seconds = _remaining_seconds(batch_deadline)
                            if remaining_seconds < nested_timeout_seconds:
                                timeout_scope = "batch"
                            nested_timeout_seconds = min(
                                nested_timeout_seconds,
                                remaining_seconds,
                            )
                        nested_output = await asyncio.wait_for(
                            nested_call, timeout=nested_timeout_seconds
                        )
                    else:
                        nested_timeout_ms = None
                        timeout_scope = "action"
                        nested_output = await nested_call
                except TimeoutError as exc:
                    elapsed_ms = (time.perf_counter() - nested_start) * 1000
                    effective_timeout_ms = (
                        context.state.settings.max_batch_duration_ms
                        if timeout_scope == "batch"
                        else nested_timeout_ms
                    )
                    failed = {
                        "index": nested_index,
                        "type": nested_action.type,
                        "ok": False,
                        "elapsed_ms": elapsed_ms,
                        "error_code": "timeout",
                        "error": f"{timeout_scope} timed out after {effective_timeout_ms} ms",
                        "output": {
                            "code": "timeout",
                            "timeout_ms": effective_timeout_ms,
                            "scope": timeout_scope,
                        },
                    }
                    nested_results.append(failed)
                    raise _nested_hold_error(
                        action_path=action_path,
                        nested_index=nested_index,
                        nested_action=nested_action,
                        nested_results=nested_results,
                        code="timeout",
                        message=failed["error"],
                    ) from exc
                except Exception as exc:
                    elapsed_ms = (time.perf_counter() - nested_start) * 1000
                    error_code = _exception_code(exc)
                    failed = {
                        "index": nested_index,
                        "type": nested_action.type,
                        "ok": False,
                        "elapsed_ms": elapsed_ms,
                        "error_code": error_code,
                        "error": _action_error_message(nested_action, exc),
                        "output": _exception_output(exc, nested_action, context=context),
                    }
                    nested_results.append(failed)
                    raise _nested_hold_error(
                        action_path=action_path,
                        nested_index=nested_index,
                        nested_action=nested_action,
                        nested_results=nested_results,
                        code=error_code,
                        message=failed["error"],
                    ) from exc
                nested_results.append(
                    {
                        "index": nested_index,
                        "type": nested_action.type,
                        "ok": True,
                        "elapsed_ms": (time.perf_counter() - nested_start) * 1000,
                        "output": nested_output or {},
                    }
                )
            if action.duration_ms is not None:
                await asyncio.sleep(action.duration_ms / 1000)
        finally:
            await backend.key_up(action.key)
        return {"actions": nested_results} if action.actions else {}
    if isinstance(action, WaitAction):
        await asyncio.sleep(action.duration_ms / 1000)
        return {"duration_ms": action.duration_ms}
    if isinstance(action, ScreenshotAction):
        options = action.options or ScreenshotOptions()
        _enforce_screenshot_options_pixels(
            context,
            source_width=context.state.backend.width,
            source_height=context.state.backend.height,
            scale=options.scale,
        )
        error = context.budget_policy.screenshot_reservation_error()
        if error is not None:
            raise error
        context.budget_policy.reserve_screenshot()
        shot = await backend.screenshot(
            options, artifact_store=context.state.artifacts, call_id=call_id
        )
        return shot.model_dump(mode="json")
    if isinstance(action, ZoomAction):
        options = action.options or ScreenshotOptions(scale=action.scale, show_cursor=True)
        options.scale = action.scale
        region = Region.model_validate(action.region)
        _enforce_screenshot_options_pixels(
            context,
            source_width=region.width,
            source_height=region.height,
            scale=options.scale,
        )
        error = context.budget_policy.screenshot_reservation_error()
        if error is not None:
            raise error
        context.budget_policy.reserve_screenshot()
        shot = await backend.screenshot(
            options,
            region=region,
            artifact_store=context.state.artifacts,
            call_id=call_id,
        )
        return shot.model_dump(mode="json")
    if isinstance(action, CursorPositionAction):
        point = await backend.mouse_position()
        return point.model_dump()
    if isinstance(action, ReleaseAllAction):
        result = await backend.release_all()
        return _checked_action_result(result, action)
    raise DaemonError(
        f"unsupported action type: {getattr(action, 'type', None)}", code="unsupported_action"
    )


def _with_input_backend(
    output: dict[str, Any],
    action: Any,
    context: ActionBatchContext,
) -> dict[str, Any]:
    if not _is_input_backend_action(action):
        return output
    backend = getattr(context.state.backend, "input_backend", None)
    if not isinstance(backend, str) or not backend:
        return output
    return {**output, "input_backend": backend}


def _is_input_backend_action(action: Any) -> bool:
    return isinstance(
        action,
        (
            MoveAction,
            ClickAction,
            DoubleClickAction,
            TripleClickAction,
            DragAction,
            ScrollAction,
            MouseDownAction,
            MouseUpAction,
            TypeAction,
            KeyPressAction,
            HotkeyAction,
            HoldKeyAction,
            ReleaseAllAction,
        ),
    )


def _nested_hold_error(
    *,
    action_path: str | None,
    nested_index: int,
    nested_action: Any,
    nested_results: list[dict[str, Any]],
    code: str,
    message: str,
) -> DaemonError:
    path = f"{action_path or 'actions'}.actions[{nested_index}]"
    return DaemonError(
        message,
        code=code,
        details={
            "actions": nested_results,
            "failed_nested_action": {
                "index": nested_index,
                "type": nested_action.type,
                "path": path,
            },
            "nested_error": message,
        },
    )


def _validate_actions(
    actions: list[Any],
    *,
    width: int | None,
    height: int | None,
    path: str = "actions",
) -> list[str]:
    errors: list[str] = []
    for index, action in enumerate(actions):
        action_path = f"{path}[{index}]"
        key_errors = _key_validation_errors(action, field=action_path)
        errors.extend(key_errors)
        for point in _action_points(action):
            if width is not None and point.x >= width:
                errors.append(f"{action_path} x coordinate {point.x} exceeds desktop width {width}")
            if height is not None and point.y >= height:
                errors.append(
                    f"{action_path} y coordinate {point.y} exceeds desktop height {height}"
                )
        region = getattr(action, "region", None)
        if (
            isinstance(region, Region)
            and width is not None
            and height is not None
            and (region.right > width or region.bottom > height)
        ):
            errors.append(f"{action_path} region extends beyond desktop geometry")
        if isinstance(action, HoldKeyAction) and action.actions:
            parsed_nested = []
            for nested_index, nested_action in enumerate(action.actions):
                try:
                    parsed = parse_action(nested_action)
                except Exception as exc:
                    errors.append(f"{action_path}.actions[{nested_index}] is invalid: {exc}")
                    continue
                parsed_nested.append(parsed)
            errors.extend(
                _validate_actions(
                    parsed_nested,
                    width=width,
                    height=height,
                    path=f"{action_path}.actions",
                )
            )
    return errors


def _key_validation_errors(action: Any, *, field: str) -> list[str]:
    keys: list[tuple[str, str]] = []
    if isinstance(action, KeyPressAction):
        keys.append((f"{field}.key", action.key))
        keys.extend(
            (f"{field}.modifiers[{index}]", key) for index, key in enumerate(action.modifiers)
        )
    elif isinstance(action, HotkeyAction):
        keys.extend((f"{field}.keys[{index}]", key) for index, key in enumerate(action.keys))
    elif isinstance(action, HoldKeyAction):
        keys.append((f"{field}.key", action.key))
    elif isinstance(action, ClickAction | DoubleClickAction | TripleClickAction | DragAction):
        keys.extend(
            (f"{field}.modifiers[{index}]", key) for index, key in enumerate(action.modifiers)
        )
    return [
        f"{path} is not a supported key: {key}" for path, key in keys if not is_supported_key(key)
    ]


def _nested_hold_actions(action: HoldKeyAction) -> list[Any]:
    return [parse_action(item) for item in action.actions or []]


def _action_points(action: Any) -> list[Point]:
    points: list[Point] = []
    x = getattr(action, "x", None)
    y = getattr(action, "y", None)
    if isinstance(x, int) and isinstance(y, int):
        points.append(Point(x=x, y=y))
    for prefix in ("start", "end"):
        px = getattr(action, f"{prefix}_x", None)
        py = getattr(action, f"{prefix}_y", None)
        if isinstance(px, int) and isinstance(py, int):
            points.append(Point(x=px, y=py))
    path = getattr(action, "path", None)
    if path:
        points.extend(path)
    return points
