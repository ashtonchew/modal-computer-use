from __future__ import annotations

from pydantic import Field

from ..models import ActionBatchResult, Screenshot, StrictBaseModel


class ComputerStepTiming(StrictBaseModel):
    """Timing fields returned by one action-to-observation step.

    ``daemon_ms`` is the stable aggregate.  The phase fields are optional so
    daemons can expose useful diagnostics without changing the public result
    shape; they are never used to establish readiness or task success.
    """

    daemon_ms: float = Field(ge=0)
    action_ms: float | None = Field(default=None, ge=0)
    screenshot_ms: float | None = Field(default=None, ge=0)
    total_ms: float | None = Field(default=None, ge=0)


class ComputerStepResult(StrictBaseModel):
    """The ordered action result and its immediate post-action observation."""

    actions: ActionBatchResult
    screenshot: Screenshot
    timing: ComputerStepTiming
