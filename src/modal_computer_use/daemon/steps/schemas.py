from __future__ import annotations

from pydantic import Field, field_validator

from modal_computer_use.models import (
    ActionBatchRequest,
    ComputerAction,
    ScreenshotOptions,
    StrictBaseModel,
)


class StepRequest(StrictBaseModel):
    """The deliberately small public request for one computer step.

    A step always captures its trailing observation.  Transport, idempotency,
    and provider-loop controls are intentionally not part of this boundary.
    """

    actions: list[ComputerAction]
    screenshot_options: ScreenshotOptions | None = None
    continue_on_error: bool = False
    call_id: str | None = None
    max_action_timeout_ms: int | None = Field(default=None, gt=0)

    @field_validator("screenshot_options")
    @classmethod
    def _inline_observation_only(
        cls, value: ScreenshotOptions | None
    ) -> ScreenshotOptions | None:
        if value is not None and value.storage != "inline":
            raise ValueError("step screenshot_options.storage must be inline")
        if value is not None and value.processing == "client":
            raise ValueError("step screenshot_options.processing must use daemon processing")
        return value

    def to_action_batch(self) -> ActionBatchRequest:
        return ActionBatchRequest(
            actions=self.actions,
            screenshot_after=True,
            screenshot_options=self.screenshot_options or ScreenshotOptions(),
            continue_on_error=self.continue_on_error,
            source="sdk",
            call_id=self.call_id,
            max_action_timeout_ms=self.max_action_timeout_ms,
        )
