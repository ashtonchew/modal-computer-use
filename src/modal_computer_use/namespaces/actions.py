from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from modal_computer_use.models import (
    ActionBatchResult,
    ActionResult,
    ComputerAction,
    ScreenshotOptions,
    ValidationResult,
    parse_action,
)

from .base import Namespace


@dataclass(frozen=True)
class ActionScreenshotBytesResult:
    result: ActionBatchResult
    data: bytes
    format: str
    width: int | None
    height: int | None
    size_bytes: int


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
            self._client.post_json("/v1/actions/run", json=payload, headers=headers)
        )

    def run_and_screenshot_bytes(
        self,
        actions: list[ComputerAction | dict[str, Any]],
        *,
        continue_on_error: bool = False,
        screenshot_options: ScreenshotOptions | None = None,
        max_action_timeout_ms: int | None = None,
        idempotency_key: str | None = None,
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
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        data, response_headers = self._client.post_bytes_with_headers(
            "/v1/actions/run/raw-screenshot",
            json=payload,
            headers=headers,
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


def _int_header(headers: Any, name: str) -> int | None:
    value = headers.get(name) if hasattr(headers, "get") else None
    if not isinstance(value, str) or not value.isdigit():
        return None
    return int(value)
