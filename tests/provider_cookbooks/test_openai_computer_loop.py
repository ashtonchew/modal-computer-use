from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from conftest import (
    QueuedProviderResponses,
    RecordingComputer,
    load_example,
    tiny_screenshot,
)

from modal_computer_use.models import (
    ActionBatchResult,
    ActionDecision,
    ActionItemResult,
)


def _response(
    response_id: str,
    *calls: tuple[str, list[dict[str, Any]]],
    output_text: str = "",
) -> Any:
    return SimpleNamespace(
        id=response_id,
        output=[
            SimpleNamespace(
                type="computer_call",
                call_id=call_id,
                actions=actions,
            )
            for call_id, actions in calls
        ],
        output_text=output_text,
    )


def _client(*responses: Any) -> tuple[Any, QueuedProviderResponses]:
    queued = QueuedProviderResponses(list(responses))
    return SimpleNamespace(responses=queued), queued


def test_openai_call_executes_one_ordered_batch() -> None:
    example = load_example("03_openai_computer_loop.py")
    computer = RecordingComputer()
    client, queued = _client(
        _response(
            "resp_1",
            (
                "call_1",
                [
                    {"type": "move", "x": 10, "y": 20},
                    {"type": "click", "x": 10, "y": 20},
                ],
            ),
        ),
        _response("resp_2", output_text="done"),
    )

    response = example.run_openai_computer_loop(
        client=client,
        computer=computer,
        task="Inspect the page",
        max_turns=2,
    )

    assert response.output_text == "done"
    assert [[action.type for action in batch] for batch in computer.actions.batches] == [
        ["move", "click"]
    ]
    assert computer.actions.batch_kwargs == [
        {
            "continue_on_error": False,
            "screenshot_after": True,
            "source": "openai-adapter",
            "max_action_timeout_ms": 10_000,
        }
    ]
    assert computer.screenshots.full_calls == 0
    assert queued.calls[1]["previous_response_id"] == "resp_1"
    assert queued.calls[1]["input"][0]["call_id"] == "call_1"
    assert queued.calls[1]["input"][0]["output"]["detail"] == "original"


def test_openai_outputs_match_all_calls_in_response_order() -> None:
    example = load_example("03_openai_computer_loop.py")
    computer = RecordingComputer()
    client, queued = _client(
        _response(
            "resp_1",
            ("call_2", [{"type": "wait", "duration_ms": 0}]),
            ("call_1", [{"type": "screenshot"}]),
        ),
        _response("resp_2", output_text="done"),
    )

    example.run_openai_computer_loop(
        client=client,
        computer=computer,
        task="Inspect the page",
        max_turns=2,
    )

    assert len(computer.actions.batches) == 2
    assert [item["call_id"] for item in queued.calls[1]["input"]] == [
        "call_2",
        "call_1",
    ]
    assert queued.calls[1]["previous_response_id"] == "resp_1"


def test_openai_preflights_later_invalid_call_before_dispatch() -> None:
    example = load_example("03_openai_computer_loop.py")
    computer = RecordingComputer()
    client, _ = _client(
        _response(
            "resp_1",
            ("call_valid", [{"type": "click", "x": 10, "y": 20}]),
            ("call_invalid", [{"type": "future_action", "secret": "do-not-leak"}]),
        )
    )

    with pytest.raises(RuntimeError, match="preflight failed: UnsupportedActionError") as exc:
        example.run_openai_computer_loop(
            client=client,
            computer=computer,
            task="Inspect the page",
        )

    assert "do-not-leak" not in str(exc.value)
    assert computer.actions.batches == []


def test_openai_preflights_later_invalid_action_in_same_call_before_dispatch() -> None:
    example = load_example("03_openai_computer_loop.py")
    computer = RecordingComputer()
    client, _ = _client(
        _response(
            "resp_1",
            (
                "call_1",
                [
                    {"type": "click", "x": 10, "y": 20},
                    {"type": "future_action", "secret": "do-not-leak"},
                ],
            ),
        )
    )

    with pytest.raises(RuntimeError, match="preflight failed: UnsupportedActionError") as exc:
        example.run_openai_computer_loop(
            client=client,
            computer=computer,
            task="Inspect the page",
        )

    assert "do-not-leak" not in str(exc.value)
    assert computer.actions.batches == []


def test_openai_preflights_response_wide_trajectory_budget() -> None:
    example = load_example("03_openai_computer_loop.py")
    computer = RecordingComputer()
    client, _ = _client(
        _response(
            "resp_1",
            ("call_1", [{"type": "move", "x": 10, "y": 20}]),
            ("call_2", [{"type": "click", "x": 10, "y": 20}]),
        )
    )

    with pytest.raises(RuntimeError, match="exceeded 1 trajectory actions"):
        example.run_openai_computer_loop(
            client=client,
            computer=computer,
            task="Inspect the page",
            max_trajectory_actions=1,
        )

    assert computer.actions.batches == []


def test_openai_preflights_later_policy_denial_before_dispatch() -> None:
    example = load_example("03_openai_computer_loop.py")
    computer = RecordingComputer()
    client, _ = _client(
        _response(
            "resp_1",
            ("call_1", [{"type": "move", "x": 10, "y": 20}]),
            ("call_2", [{"type": "click", "x": 99, "y": 20}]),
        )
    )

    def policy(action: Any, context: dict[str, Any]) -> ActionDecision:
        assert context["source"] == "openai-adapter"
        return ActionDecision(
            decision="deny" if getattr(action, "x", None) == 99 else "allow",
            reason="private policy reason",
        )

    with pytest.raises(RuntimeError, match="preflight failed: UnsupportedActionError") as exc:
        example.run_openai_computer_loop(
            client=client,
            computer=computer,
            task="Inspect the page",
            before_action=policy,
        )

    assert "private policy reason" not in str(exc.value)
    assert computer.actions.batches == []


def test_openai_expanded_batch_bound_counts_modifier_action_trees() -> None:
    example = load_example("03_openai_computer_loop.py")
    computer = RecordingComputer()
    client, _ = _client(
        _response(
            "resp_1",
            (
                "call_1",
                [
                    {
                        "type": "scroll",
                        "x": 10,
                        "y": 20,
                        "scroll_x": 100,
                        "scroll_y": 100,
                        "keys": ["SHIFT", "CTRL"],
                    }
                ],
            ),
        )
    )

    with pytest.raises(RuntimeError, match="exceeded 5 expanded batch actions"):
        example.run_openai_computer_loop(
            client=client,
            computer=computer,
            task="Inspect the page",
            max_batch_actions=5,
        )

    assert computer.actions.batches == []


def test_openai_trajectory_bound_counts_expanded_provider_actions() -> None:
    example = load_example("03_openai_computer_loop.py")
    computer = RecordingComputer()
    client, _ = _client(
        _response(
            "resp_1",
            ("call_1", [{"type": "keypress", "keys": ["CTRL", "C"]}]),
        )
    )

    with pytest.raises(RuntimeError, match="exceeded 1 trajectory actions"):
        example.run_openai_computer_loop(
            client=client,
            computer=computer,
            task="Inspect the page",
            max_trajectory_actions=1,
        )

    assert computer.actions.batches == []


def test_openai_allocates_batch_deadline_across_expanded_actions_and_frame() -> None:
    example = load_example("03_openai_computer_loop.py")
    example.monotonic = lambda: 0.0
    computer = RecordingComputer()
    client, _ = _client(
        _response(
            "resp_1",
            ("call_1", [{"type": "keypress", "keys": ["CTRL", "C"]}]),
        ),
        _response("resp_2", output_text="done"),
    )

    example.run_openai_computer_loop(
        client=client,
        computer=computer,
        task="Inspect the page",
        max_turns=2,
        max_elapsed_seconds=1.0,
    )

    assert computer.actions.batch_kwargs[0]["max_action_timeout_ms"] == 333
    assert [action.timeout_ms for action in computer.actions.batches[0]] == [333, 333]


def test_openai_caps_deadline_allocation_by_daemon_batch_duration() -> None:
    example = load_example("03_openai_computer_loop.py")
    example.monotonic = lambda: 0.0
    computer = RecordingComputer()
    client, _ = _client(
        _response(
            "resp_1",
            ("call_1", [{"type": "click", "x": 10, "y": 20}]),
        ),
        _response("resp_2", output_text="done"),
    )

    example.run_openai_computer_loop(
        client=client,
        computer=computer,
        task="Inspect the page",
        max_turns=2,
        max_elapsed_seconds=100.0,
        max_batch_duration_ms=900,
    )

    assert computer.actions.batch_kwargs[0]["max_action_timeout_ms"] == 450


def test_openai_reuses_native_final_screenshot_when_fused_capture_fails() -> None:
    example = load_example("03_openai_computer_loop.py")
    native = tiny_screenshot()
    batch_result = ActionBatchResult(
        ok=False,
        results=[
            ActionItemResult(
                index=0,
                type="screenshot",
                ok=True,
                output=native.model_dump(mode="json"),
            ),
            ActionItemResult(
                index=1,
                type="screenshot_after",
                ok=False,
                error_code="capture_failed",
                error="private capture failure",
            ),
        ],
    )
    computer = RecordingComputer(batch_results=[batch_result])
    client, queued = _client(
        _response("resp_1", ("call_1", [{"type": "screenshot"}])),
        _response("resp_2", output_text="done"),
    )

    example.run_openai_computer_loop(
        client=client,
        computer=computer,
        task="Inspect the page",
        max_turns=2,
    )

    assert queued.calls[1]["input"][0]["output"]["image_url"].endswith(native.data_base64)
    assert computer.screenshots.full_calls == 0


def test_openai_batch_failure_is_sanitized() -> None:
    example = load_example("03_openai_computer_loop.py")
    batch_result = ActionBatchResult(
        ok=False,
        results=[
            ActionItemResult(
                index=0,
                type="type",
                ok=False,
                error_code="action_failed",
                error="secret typed value",
                output={"daemon_token": "secret-token"},
            )
        ],
    )
    computer = RecordingComputer(batch_results=[batch_result])
    client, _ = _client(_response("resp_1", ("call_1", [{"type": "type", "text": "secret"}])))

    with pytest.raises(RuntimeError, match="failed at index 0") as exc:
        example.run_openai_computer_loop(
            client=client,
            computer=computer,
            task="Inspect the page",
        )

    assert "secret" not in str(exc.value)
    assert "daemon_token" not in str(exc.value)


def test_openai_dispatch_exception_is_sanitized() -> None:
    example = load_example("03_openai_computer_loop.py")
    computer = RecordingComputer()

    def fail_batch(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("private daemon token")

    computer.actions.run = fail_batch
    client, _ = _client(_response("resp_1", ("call_1", [{"type": "click", "x": 10, "y": 20}])))

    with pytest.raises(RuntimeError, match="batch failed: RuntimeError") as exc:
        example.run_openai_computer_loop(
            client=client,
            computer=computer,
            task="Inspect the page",
        )

    assert "private daemon token" not in str(exc.value)


def test_openai_final_allowed_turn_stops_before_dispatch() -> None:
    example = load_example("03_openai_computer_loop.py")
    computer = RecordingComputer()
    client, _ = _client(_response("resp_1", ("call_1", [{"type": "click", "x": 10, "y": 20}])))

    with pytest.raises(RuntimeError, match="exceeded 1 turns"):
        example.run_openai_computer_loop(
            client=client,
            computer=computer,
            task="Inspect the page",
            max_turns=1,
        )

    assert computer.actions.batches == []


def test_openai_main_selects_and_prewarms_chromium(monkeypatch: pytest.MonkeyPatch) -> None:
    example = load_example("03_openai_computer_loop.py")
    created: dict[str, Any] = {}

    class FakeComputer:
        def __enter__(self) -> FakeComputer:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def ensure_browser_ready(self, config: Any) -> None:
            created["ready_config"] = config

    class FakeSandbox:
        @classmethod
        def create(cls, **kwargs: Any) -> FakeComputer:
            created.update(kwargs)
            return FakeComputer()

    fake_openai = ModuleType("openai")
    fake_openai.OpenAI = lambda: object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    monkeypatch.setattr(example, "ComputerSandbox", FakeSandbox)
    monkeypatch.setattr(
        example,
        "run_openai_computer_loop",
        lambda **kwargs: SimpleNamespace(output_text="done"),
    )

    example.main()

    config = created["config"]
    assert created["wait"] is True
    assert config.resources.profile == "browser"
    assert config.browser.kind == "chromium"
    assert config.browser.prewarm is True
    assert created["ready_config"] is config
