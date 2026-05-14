from __future__ import annotations

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
        normalized = [parse_action(action) for action in actions]
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
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
        return ActionBatchResult.model_validate(
            self._client.post_json("/v1/actions/run", json=payload, headers=headers)
        )

    def validate(self, actions: list[ComputerAction | dict[str, Any]]) -> ValidationResult:
        normalized = [parse_action(action) for action in actions]
        return ValidationResult.model_validate(
            self._client.post_json(
                "/v1/actions/validate",
                json={"actions": [action.model_dump(mode="json") for action in normalized]},
            )
        )
