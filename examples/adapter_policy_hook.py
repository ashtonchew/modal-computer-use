from __future__ import annotations

from typing import Any

from modal_computer_use.adapters.openai import OpenAIAdapter
from modal_computer_use.models import (
    ActionBatchResult,
    ActionDecision,
    ActionItemResult,
    ActionResult,
)


class RecordingActions:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def apply(self, action: Any) -> ActionResult:
        self.executed.append(action.type)
        return ActionResult(ok=True, output={"type": action.type})

    def run(
        self,
        actions: list[Any],
        *,
        continue_on_error: bool = False,
        screenshot_after: bool = False,
        source: str = "sdk",
    ) -> ActionBatchResult:
        del continue_on_error, screenshot_after, source
        self.executed.extend(action.type for action in actions)
        return ActionBatchResult(
            ok=True,
            results=[
                ActionItemResult(index=index, type=action.type, ok=True)
                for index, action in enumerate(actions)
            ],
        )


class RecordingComputer:
    def __init__(self) -> None:
        self.actions = RecordingActions()


def require_confirmation_for_text(action: Any, context: dict[str, Any]) -> ActionDecision:
    del context
    if action.type == "type":
        return ActionDecision(decision="ask_user", reason="typing requires app confirmation")
    if action.type == "click" and action.button != "left":
        return ActionDecision(decision="deny", reason="only left clicks are allowed")
    return ActionDecision(decision="allow")


def main() -> None:
    computer = RecordingComputer()
    adapter = OpenAIAdapter(computer, before_action=require_confirmation_for_text)

    adapter.apply({"type": "click", "x": 100, "y": 200})
    try:
        adapter.apply({"type": "type", "text": "do not log this"})
    except Exception as exc:
        print(type(exc).__name__, str(exc))

    print("executed action types:", computer.actions.executed)


if __name__ == "__main__":
    main()
