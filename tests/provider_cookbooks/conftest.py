from __future__ import annotations

import importlib.util
from collections import deque
from pathlib import Path
from types import ModuleType
from typing import Any

from modal_computer_use.models import (
    ActionBatchResult,
    ActionItemResult,
    ActionResult,
    CoordinateSpace,
    Screenshot,
)

ROOT = Path(__file__).parents[2]


def load_example(filename: str) -> ModuleType:
    path = ROOT / "examples" / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tiny_screenshot() -> Screenshot:
    return Screenshot(
        format="png",
        width=1,
        height=1,
        size_bytes=68,
        data_base64=(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4"
            "z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
        ),
        sha256="synthetic",
        artifact_uri="artifact://screenshots/tiny.png",
        coordinate_space=CoordinateSpace.from_dimensions(
            desktop_width=1,
            desktop_height=1,
        ),
    )


class RecordingScreenshots:
    def __init__(self) -> None:
        self.full_calls = 0

    def full(self) -> Screenshot:
        self.full_calls += 1
        return tiny_screenshot()


class RecordingActions:
    def __init__(
        self,
        *,
        apply_results: list[ActionResult] | None = None,
        batch_results: list[ActionBatchResult] | None = None,
    ) -> None:
        self.applied: list[tuple[Any, str]] = []
        self.batches: list[list[Any]] = []
        self.batch_kwargs: list[dict[str, Any]] = []
        self._apply_results = deque(apply_results or [])
        self._batch_results = deque(batch_results or [])

    def apply(self, action: Any, *, source: str = "sdk") -> ActionResult:
        self.applied.append((action, source))
        if self._apply_results:
            return self._apply_results.popleft()
        if action.type == "cursor_position":
            return ActionResult(ok=True, output={"x": 12, "y": 34})
        return ActionResult(ok=True, output={"type": action.type})

    def run(
        self,
        actions: list[Any],
        *,
        continue_on_error: bool = False,
        screenshot_after: bool = False,
        source: str = "sdk",
        max_action_timeout_ms: int | None = None,
    ) -> ActionBatchResult:
        self.batches.append(actions)
        self.batch_kwargs.append(
            {
                "continue_on_error": continue_on_error,
                "screenshot_after": screenshot_after,
                "source": source,
                "max_action_timeout_ms": max_action_timeout_ms,
            }
        )
        if self._batch_results:
            return self._batch_results.popleft()
        return ActionBatchResult(
            ok=True,
            results=[
                ActionItemResult(
                    index=index,
                    type=action.type,
                    ok=True,
                    output={"type": action.type},
                )
                for index, action in enumerate(actions)
            ],
            screenshot=tiny_screenshot() if screenshot_after else None,
        )


class RecordingComputer:
    def __init__(
        self,
        *,
        apply_results: list[ActionResult] | None = None,
        batch_results: list[ActionBatchResult] | None = None,
    ) -> None:
        self.actions = RecordingActions(
            apply_results=apply_results,
            batch_results=batch_results,
        )
        self.screenshots = RecordingScreenshots()


class AsyncRecordingScreenshots(RecordingScreenshots):
    async def full(self) -> Screenshot:
        return super().full()


class AsyncRecordingActions(RecordingActions):
    async def run(
        self,
        actions: list[Any],
        *,
        continue_on_error: bool = False,
        screenshot_after: bool = False,
        source: str = "sdk",
        max_action_timeout_ms: int | None = None,
    ) -> ActionBatchResult:
        return super().run(
            actions,
            continue_on_error=continue_on_error,
            screenshot_after=screenshot_after,
            source=source,
            max_action_timeout_ms=max_action_timeout_ms,
        )


class AsyncRecordingComputer:
    def __init__(
        self,
        *,
        apply_results: list[ActionResult] | None = None,
        batch_results: list[ActionBatchResult] | None = None,
    ) -> None:
        self.actions = AsyncRecordingActions(
            apply_results=apply_results,
            batch_results=batch_results,
        )
        self.screenshots = AsyncRecordingScreenshots()


class QueuedProviderResponses:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = deque(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.responses.popleft()


class AsyncQueuedProviderResponses(QueuedProviderResponses):
    async def create(self, **kwargs: Any) -> Any:
        return super().create(**kwargs)
