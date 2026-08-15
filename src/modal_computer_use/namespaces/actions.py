from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from modal_computer_use.models import (
    ActionBatchResult,
    ActionResult,
    ComputerAction,
    Region,
    ScreenshotOptions,
    ValidationResult,
    parse_action,
)

from .base import AsyncNamespace, Namespace


@dataclass(frozen=True)
class ActionScreenshotBytesResult:
    result: ActionBatchResult
    data: bytes
    format: str
    width: int | None
    height: int | None
    size_bytes: int
    change_result: dict[str, Any] | None = None
    change_timing_ms: dict[str, float] | None = None


class ActionsNamespace(Namespace):
    def apply(
        self,
        action: ComputerAction | dict[str, Any],
        *,
        source: str = "sdk",
    ) -> ActionResult:
        result = self.run([action], source=source)
        first = result.results[0]
        return ActionResult(
            ok=first.ok, message=first.error, elapsed_ms=first.elapsed_ms, output=first.output
        )

    def run(
        self,
        actions: list[ComputerAction | dict[str, Any]],
        *,
        continue_on_error: bool = False,
        screenshot_after: bool = False,
        screenshot_options: ScreenshotOptions | None = None,
        max_action_timeout_ms: int | None = None,
        idempotency_key: str | None = None,
        call_id: str | None = None,
        run_id: str | None = None,
        sequence: int | None = None,
        source: str = "sdk",
    ) -> ActionBatchResult:
        payload = self._run_payload(
            actions,
            continue_on_error=continue_on_error,
            screenshot_after=screenshot_after,
            screenshot_options=screenshot_options,
            max_action_timeout_ms=max_action_timeout_ms,
            call_id=call_id,
            run_id=run_id,
            sequence=sequence,
            source=source,
        )
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return ActionBatchResult.model_validate(
            self._client.post_json(
                "/v1/actions/run", json=payload, headers=headers, _mutation=True
            )
        )

    def run_and_screenshot_bytes(
        self,
        actions: list[ComputerAction | dict[str, Any]],
        *,
        continue_on_error: bool = False,
        screenshot_options: ScreenshotOptions | None = None,
        max_action_timeout_ms: int | None = None,
        call_id: str | None = None,
        run_id: str | None = None,
        sequence: int | None = None,
        source: str = "sdk",
    ) -> ActionScreenshotBytesResult:
        payload = self._run_payload(
            actions,
            continue_on_error=continue_on_error,
            screenshot_after=True,
            screenshot_options=screenshot_options,
            max_action_timeout_ms=max_action_timeout_ms,
            call_id=call_id,
            run_id=run_id,
            sequence=sequence,
            source=source,
        )
        data, response_headers = self._client.post_bytes_with_headers(
            "/v1/actions/run/raw-screenshot",
            json=payload,
            _mutation=True,
        )
        result_header = response_headers.get("x-computer-use-action-result")
        result = ActionBatchResult.model_validate(
            json.loads(base64.b64decode(result_header or "e30=").decode("utf-8"))
        )
        return ActionScreenshotBytesResult(
            result=result,
            data=data,
            format=str(response_headers.get("content-type", "image/png")).removeprefix("image/"),
            width=_int_header(response_headers, "x-computer-use-width"),
            height=_int_header(response_headers, "x-computer-use-height"),
            size_bytes=len(data),
        )

    def run_and_observe_change_screenshot_bytes(
        self,
        actions: list[ComputerAction | dict[str, Any]],
        *,
        previous_source_sha256: str | None = None,
        capture_delay_ms: int = 0,
        change_timeout_ms: int = 100,
        poll_interval_ms: int = 8,
        poll_strategy: str = "fixed",
        change_detection: str = "full",
        change_detection_region: Region | dict[str, Any] | None = None,
        change_region_radius: int = 192,
        change_signal: str = "auto",
        continue_on_error: bool = False,
        screenshot_options: ScreenshotOptions | None = None,
        max_action_timeout_ms: int | None = None,
        call_id: str | None = None,
        run_id: str | None = None,
        sequence: int | None = None,
        source: str = "sdk",
    ) -> ActionScreenshotBytesResult:
        payload = self._run_payload(
            actions,
            continue_on_error=continue_on_error,
            screenshot_after=False,
            screenshot_options=screenshot_options,
            max_action_timeout_ms=max_action_timeout_ms,
            call_id=call_id,
            run_id=run_id,
            sequence=sequence,
            source=source,
        )
        payload.update(
            {
                "previous_source_sha256": previous_source_sha256,
                "capture_delay_ms": capture_delay_ms,
                "change_timeout_ms": change_timeout_ms,
                "poll_interval_ms": poll_interval_ms,
                "poll_strategy": poll_strategy,
                "change_detection": change_detection,
                "change_detection_region": change_detection_region.model_dump(mode="json")
                if isinstance(change_detection_region, Region)
                else change_detection_region,
                "change_region_radius": change_region_radius,
                "change_signal": change_signal,
            }
        )
        data, response_headers = self._client.post_bytes_with_headers(
            "/v1/actions/run/observe-change/raw-screenshot",
            json=payload,
            _mutation=True,
        )
        result = _action_result_header(response_headers)
        return ActionScreenshotBytesResult(
            result=result,
            data=data,
            format=str(response_headers.get("content-type", "image/png")).removeprefix("image/"),
            width=_int_header(response_headers, "x-computer-use-width"),
            height=_int_header(response_headers, "x-computer-use-height"),
            size_bytes=len(data),
            change_result=_json_response_header(response_headers, "x-computer-use-change-result"),
            change_timing_ms=_float_json_response_header(
                response_headers,
                "x-computer-use-change-timing-ms",
            ),
        )

    def validate(
        self,
        actions: list[ComputerAction | dict[str, Any]],
        *,
        continue_on_error: bool = False,
        screenshot_after: bool = False,
        screenshot_options: ScreenshotOptions | None = None,
        max_action_timeout_ms: int | None = None,
        call_id: str | None = None,
        run_id: str | None = None,
        sequence: int | None = None,
        source: str = "sdk",
    ) -> ValidationResult:
        normalized = [parse_action(action) for action in actions]
        payload = {
            "actions": [action.model_dump(mode="json") for action in normalized],
            "continue_on_error": continue_on_error,
            "screenshot_after": screenshot_after,
            "screenshot_options": screenshot_options.model_dump(mode="json")
            if screenshot_options
            else None,
            "max_action_timeout_ms": max_action_timeout_ms,
            "call_id": call_id,
            "run_id": run_id,
            "sequence": sequence,
            "source": source,
        }
        return ValidationResult.model_validate(
            self._client.post_json(
                "/v1/actions/validate",
                json=payload,
            )
        )

    @staticmethod
    def _run_payload(
        actions: list[ComputerAction | dict[str, Any]],
        *,
        continue_on_error: bool,
        screenshot_after: bool,
        screenshot_options: ScreenshotOptions | None,
        max_action_timeout_ms: int | None,
        call_id: str | None,
        run_id: str | None,
        sequence: int | None,
        source: str,
    ) -> dict[str, Any]:
        normalized = [parse_action(action) for action in actions]
        return {
            "actions": [action.model_dump(mode="json") for action in normalized],
            "continue_on_error": continue_on_error,
            "screenshot_after": screenshot_after,
            "screenshot_options": screenshot_options.model_dump(mode="json")
            if screenshot_options
            else None,
            "max_action_timeout_ms": max_action_timeout_ms,
            "call_id": call_id,
            "run_id": run_id,
            "sequence": sequence,
            "source": source,
        }


class AsyncActionsNamespace(AsyncNamespace):
    async def apply(
        self,
        action: ComputerAction | dict[str, Any],
        *,
        source: str = "sdk",
    ) -> ActionResult:
        result = await self.run([action], source=source)
        first = result.results[0]
        return ActionResult(
            ok=first.ok,
            message=first.error,
            elapsed_ms=first.elapsed_ms,
            output=first.output,
        )

    async def run(
        self,
        actions: list[ComputerAction | dict[str, Any]],
        *,
        continue_on_error: bool = False,
        screenshot_after: bool = False,
        screenshot_options: ScreenshotOptions | None = None,
        max_action_timeout_ms: int | None = None,
        idempotency_key: str | None = None,
        call_id: str | None = None,
        run_id: str | None = None,
        sequence: int | None = None,
        source: str = "sdk",
    ) -> ActionBatchResult:
        payload = ActionsNamespace._run_payload(
            actions,
            continue_on_error=continue_on_error,
            screenshot_after=screenshot_after,
            screenshot_options=screenshot_options,
            max_action_timeout_ms=max_action_timeout_ms,
            call_id=call_id,
            run_id=run_id,
            sequence=sequence,
            source=source,
        )
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return ActionBatchResult.model_validate(
            await self._client.post_json(
                "/v1/actions/run", json=payload, headers=headers, _mutation=True
            )
        )

    async def run_and_screenshot_bytes(
        self,
        actions: list[ComputerAction | dict[str, Any]],
        *,
        continue_on_error: bool = False,
        screenshot_options: ScreenshotOptions | None = None,
        max_action_timeout_ms: int | None = None,
        call_id: str | None = None,
        run_id: str | None = None,
        sequence: int | None = None,
        source: str = "sdk",
    ) -> ActionScreenshotBytesResult:
        payload = ActionsNamespace._run_payload(
            actions,
            continue_on_error=continue_on_error,
            screenshot_after=True,
            screenshot_options=screenshot_options,
            max_action_timeout_ms=max_action_timeout_ms,
            call_id=call_id,
            run_id=run_id,
            sequence=sequence,
            source=source,
        )
        data, response_headers = await self._client.post_bytes_with_headers(
            "/v1/actions/run/raw-screenshot",
            json=payload,
            _mutation=True,
        )
        result_header = response_headers.get("x-computer-use-action-result")
        result = ActionBatchResult.model_validate(
            json.loads(base64.b64decode(result_header or "e30=").decode("utf-8"))
        )
        return ActionScreenshotBytesResult(
            result=result,
            data=data,
            format=str(response_headers.get("content-type", "image/png")).removeprefix("image/"),
            width=_int_header(response_headers, "x-computer-use-width"),
            height=_int_header(response_headers, "x-computer-use-height"),
            size_bytes=len(data),
        )

    async def run_and_observe_change_screenshot_bytes(
        self,
        actions: list[ComputerAction | dict[str, Any]],
        *,
        previous_source_sha256: str | None = None,
        capture_delay_ms: int = 0,
        change_timeout_ms: int = 100,
        poll_interval_ms: int = 8,
        poll_strategy: str = "fixed",
        change_detection: str = "full",
        change_detection_region: Region | dict[str, Any] | None = None,
        change_region_radius: int = 192,
        change_signal: str = "auto",
        continue_on_error: bool = False,
        screenshot_options: ScreenshotOptions | None = None,
        max_action_timeout_ms: int | None = None,
        call_id: str | None = None,
        run_id: str | None = None,
        sequence: int | None = None,
        source: str = "sdk",
    ) -> ActionScreenshotBytesResult:
        payload = ActionsNamespace._run_payload(
            actions,
            continue_on_error=continue_on_error,
            screenshot_after=False,
            screenshot_options=screenshot_options,
            max_action_timeout_ms=max_action_timeout_ms,
            call_id=call_id,
            run_id=run_id,
            sequence=sequence,
            source=source,
        )
        payload.update(
            {
                "previous_source_sha256": previous_source_sha256,
                "capture_delay_ms": capture_delay_ms,
                "change_timeout_ms": change_timeout_ms,
                "poll_interval_ms": poll_interval_ms,
                "poll_strategy": poll_strategy,
                "change_detection": change_detection,
                "change_detection_region": change_detection_region.model_dump(mode="json")
                if isinstance(change_detection_region, Region)
                else change_detection_region,
                "change_region_radius": change_region_radius,
                "change_signal": change_signal,
            }
        )
        data, response_headers = await self._client.post_bytes_with_headers(
            "/v1/actions/run/observe-change/raw-screenshot",
            json=payload,
            _mutation=True,
        )
        result = _action_result_header(response_headers)
        return ActionScreenshotBytesResult(
            result=result,
            data=data,
            format=str(response_headers.get("content-type", "image/png")).removeprefix("image/"),
            width=_int_header(response_headers, "x-computer-use-width"),
            height=_int_header(response_headers, "x-computer-use-height"),
            size_bytes=len(data),
            change_result=_json_response_header(response_headers, "x-computer-use-change-result"),
            change_timing_ms=_float_json_response_header(
                response_headers,
                "x-computer-use-change-timing-ms",
            ),
        )

    async def validate(
        self,
        actions: list[ComputerAction | dict[str, Any]],
        *,
        continue_on_error: bool = False,
        screenshot_after: bool = False,
        screenshot_options: ScreenshotOptions | None = None,
        max_action_timeout_ms: int | None = None,
        call_id: str | None = None,
        run_id: str | None = None,
        sequence: int | None = None,
        source: str = "sdk",
    ) -> ValidationResult:
        payload = ActionsNamespace._run_payload(
            actions,
            continue_on_error=continue_on_error,
            screenshot_after=screenshot_after,
            screenshot_options=screenshot_options,
            max_action_timeout_ms=max_action_timeout_ms,
            call_id=call_id,
            run_id=run_id,
            sequence=sequence,
            source=source,
        )
        return ValidationResult.model_validate(
            await self._client.post_json("/v1/actions/validate", json=payload)
        )


def _int_header(headers: Any, name: str) -> int | None:
    value = headers.get(name) if hasattr(headers, "get") else None
    if not isinstance(value, str) or not value.isdigit():
        return None
    return int(value)


def _action_result_header(headers: Any) -> ActionBatchResult:
    data = _json_response_header(headers, "x-computer-use-action-result") or {}
    return ActionBatchResult.model_validate(data)


def _json_response_header(headers: Any, name: str) -> dict[str, Any] | None:
    value = headers.get(name) if hasattr(headers, "get") else None
    if not isinstance(value, str) or not value:
        return None
    return json.loads(base64.b64decode(value).decode("utf-8"))


def _float_json_response_header(headers: Any, name: str) -> dict[str, float] | None:
    value = headers.get(name) if hasattr(headers, "get") else None
    if not isinstance(value, str) or not value:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        return None
    return {
        str(key): float(value)
        for key, value in parsed.items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }
