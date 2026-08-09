from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import (
    AsyncQueuedProviderResponses,
    AsyncRecordingComputer,
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


def _client(*responses: Any) -> tuple[Any, AsyncQueuedProviderResponses]:
    queued = AsyncQueuedProviderResponses(list(responses))
    return SimpleNamespace(responses=queued), queued


@pytest.mark.asyncio
async def test_placed_trajectory_borrows_once_and_keeps_each_model_array_in_one_batch() -> None:
    example = load_example("03_openai_computer_loop.py")
    events: list[str] = []
    batches: list[list[Any]] = []

    class Responses:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.items = iter(
                [
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
                ]
            )

        async def create(self, **kwargs: Any) -> Any:
            events.append("model")
            self.calls.append(kwargs)
            return next(self.items)

    class Computer:
        async def step(self, actions: list[Any], **kwargs: Any) -> Any:
            assert handle.active
            events.append("step")
            batches.append(actions)
            assert kwargs["continue_on_error"] is False
            assert kwargs["call_id"] == "call_1"
            return SimpleNamespace(
                actions=ActionBatchResult(
                    ok=True,
                    results=[
                        ActionItemResult(index=index, type=action.type, ok=True)
                        for index, action in enumerate(actions)
                    ],
                ),
                screenshot=tiny_screenshot(),
            )

    computer = Computer()

    class Handle:
        def __init__(self) -> None:
            self.active = False
            self.borrow_calls: list[tuple[str, str]] = []

        @asynccontextmanager
        async def borrow_async(
            self, *, run_id: str, function_region: str
        ) -> AsyncIterator[Any]:
            self.borrow_calls.append((run_id, function_region))
            self.active = True
            events.append("borrow-enter")
            try:
                yield computer
            finally:
                self.active = False
                events.append("borrow-exit")

    handle = Handle()
    responses = Responses()
    response = await example.run_openai_trajectory_body(
        handle,
        "Inspect the page",
        run_id="run-123",
        function_region="us-west-2",
        client=SimpleNamespace(responses=responses),
        max_turns=2,
    )

    assert response.output_text == "done"
    assert handle.borrow_calls == [("run-123", "us-west-2")]
    assert [[action.type for action in batch] for batch in batches] == [["move", "click"]]
    assert responses.calls[1]["input"][0]["output"]["image_url"].startswith(
        "data:image/png;base64,"
    )
    assert events == [
        "borrow-enter",
        "model",
        "step",
        "model",
        "borrow-exit",
    ]


@pytest.mark.asyncio
async def test_openai_call_executes_one_ordered_batch() -> None:
    example = load_example("03_openai_computer_loop.py")
    computer = AsyncRecordingComputer()
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

    response = await example.run_openai_computer_loop(
        client=client,
        computer=computer,
        task="Inspect the page",
        max_turns=2,
    )

    assert response.output_text == "done"
    assert [[action.type for action in batch] for batch in computer.steps] == [["move", "click"]]
    assert computer.step_kwargs == [
        {
            "continue_on_error": False,
            "call_id": "call_1",
            "screenshot_options": None,
            "max_action_timeout_ms": 10_000,
        }
    ]
    assert computer.screenshots.full_calls == 0
    assert queued.calls[1]["previous_response_id"] == "resp_1"
    assert queued.calls[1]["input"][0]["call_id"] == "call_1"
    assert queued.calls[1]["input"][0]["output"]["detail"] == "original"


@pytest.mark.asyncio
async def test_openai_outputs_match_all_calls_in_response_order() -> None:
    example = load_example("03_openai_computer_loop.py")
    computer = AsyncRecordingComputer()
    client, queued = _client(
        _response(
            "resp_1",
            ("call_2", [{"type": "wait", "duration_ms": 0}]),
            ("call_1", [{"type": "screenshot"}]),
        ),
        _response("resp_2", output_text="done"),
    )

    await example.run_openai_computer_loop(
        client=client,
        computer=computer,
        task="Inspect the page",
        max_turns=2,
    )

    assert len(computer.steps) == 2
    assert [item["call_id"] for item in queued.calls[1]["input"]] == [
        "call_2",
        "call_1",
    ]
    assert queued.calls[1]["previous_response_id"] == "resp_1"


@pytest.mark.asyncio
async def test_openai_preflights_later_invalid_call_before_dispatch() -> None:
    example = load_example("03_openai_computer_loop.py")
    computer = AsyncRecordingComputer()
    client, _ = _client(
        _response(
            "resp_1",
            ("call_valid", [{"type": "click", "x": 10, "y": 20}]),
            ("call_invalid", [{"type": "future_action", "secret": "do-not-leak"}]),
        )
    )

    with pytest.raises(RuntimeError, match="preflight failed: UnsupportedActionError") as exc:
        await example.run_openai_computer_loop(
            client=client,
            computer=computer,
            task="Inspect the page",
        )

    assert "do-not-leak" not in str(exc.value)
    assert computer.steps == []


@pytest.mark.asyncio
async def test_openai_preflights_later_invalid_action_in_same_call_before_dispatch() -> None:
    example = load_example("03_openai_computer_loop.py")
    computer = AsyncRecordingComputer()
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
        await example.run_openai_computer_loop(
            client=client,
            computer=computer,
            task="Inspect the page",
        )

    assert "do-not-leak" not in str(exc.value)
    assert computer.steps == []


@pytest.mark.asyncio
async def test_openai_preflights_response_wide_trajectory_budget() -> None:
    example = load_example("03_openai_computer_loop.py")
    computer = AsyncRecordingComputer()
    client, _ = _client(
        _response(
            "resp_1",
            ("call_1", [{"type": "move", "x": 10, "y": 20}]),
            ("call_2", [{"type": "click", "x": 10, "y": 20}]),
        )
    )

    with pytest.raises(RuntimeError, match="exceeded 1 trajectory actions"):
        await example.run_openai_computer_loop(
            client=client,
            computer=computer,
            task="Inspect the page",
            max_trajectory_actions=1,
        )

    assert computer.steps == []


@pytest.mark.asyncio
async def test_openai_preflights_later_policy_denial_before_dispatch() -> None:
    example = load_example("03_openai_computer_loop.py")
    computer = AsyncRecordingComputer()
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
        await example.run_openai_computer_loop(
            client=client,
            computer=computer,
            task="Inspect the page",
            before_action=policy,
        )

    assert "private policy reason" not in str(exc.value)
    assert computer.steps == []


@pytest.mark.asyncio
async def test_openai_expanded_batch_bound_counts_modifier_action_trees() -> None:
    example = load_example("03_openai_computer_loop.py")
    computer = AsyncRecordingComputer()
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
        await example.run_openai_computer_loop(
            client=client,
            computer=computer,
            task="Inspect the page",
            max_batch_actions=5,
        )

    assert computer.steps == []


@pytest.mark.asyncio
async def test_openai_trajectory_bound_counts_expanded_provider_actions() -> None:
    example = load_example("03_openai_computer_loop.py")
    computer = AsyncRecordingComputer()
    client, _ = _client(
        _response(
            "resp_1",
            ("call_1", [{"type": "keypress", "keys": ["CTRL", "C"]}]),
        )
    )

    with pytest.raises(RuntimeError, match="exceeded 1 trajectory actions"):
        await example.run_openai_computer_loop(
            client=client,
            computer=computer,
            task="Inspect the page",
            max_trajectory_actions=1,
        )

    assert computer.steps == []


@pytest.mark.asyncio
async def test_openai_allocates_batch_deadline_across_expanded_actions_and_frame() -> None:
    example = load_example("03_openai_computer_loop.py")
    example.monotonic = lambda: 0.0
    computer = AsyncRecordingComputer()
    client, _ = _client(
        _response(
            "resp_1",
            ("call_1", [{"type": "keypress", "keys": ["CTRL", "C"]}]),
        ),
        _response("resp_2", output_text="done"),
    )

    await example.run_openai_computer_loop(
        client=client,
        computer=computer,
        task="Inspect the page",
        max_turns=2,
        max_elapsed_seconds=1.0,
    )

    assert computer.step_kwargs[0]["max_action_timeout_ms"] == 333
    assert [action.timeout_ms for action in computer.steps[0]] == [333, 333]


@pytest.mark.asyncio
async def test_openai_caps_deadline_allocation_by_daemon_batch_duration() -> None:
    example = load_example("03_openai_computer_loop.py")
    example.monotonic = lambda: 0.0
    computer = AsyncRecordingComputer()
    client, _ = _client(
        _response(
            "resp_1",
            ("call_1", [{"type": "click", "x": 10, "y": 20}]),
        ),
        _response("resp_2", output_text="done"),
    )

    await example.run_openai_computer_loop(
        client=client,
        computer=computer,
        task="Inspect the page",
        max_turns=2,
        max_elapsed_seconds=100.0,
        max_batch_duration_ms=900,
    )

    assert computer.step_kwargs[0]["max_action_timeout_ms"] == 450


@pytest.mark.asyncio
async def test_openai_stops_before_capture_when_action_batch_fails() -> None:
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
    computer = AsyncRecordingComputer(batch_results=[batch_result])
    client, queued = _client(
        _response("resp_1", ("call_1", [{"type": "screenshot"}])),
        _response("resp_2", output_text="done"),
    )

    with pytest.raises(RuntimeError, match="batch failed at index 1"):
        await example.run_openai_computer_loop(
            client=client,
            computer=computer,
            task="Inspect the page",
            max_turns=2,
        )

    assert len(queued.calls) == 1
    assert computer.screenshots.full_calls == 0


@pytest.mark.asyncio
async def test_openai_batch_failure_is_sanitized() -> None:
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
    computer = AsyncRecordingComputer(batch_results=[batch_result])
    client, _ = _client(_response("resp_1", ("call_1", [{"type": "type", "text": "secret"}])))

    with pytest.raises(RuntimeError, match="failed at index 0") as exc:
        await example.run_openai_computer_loop(
            client=client,
            computer=computer,
            task="Inspect the page",
        )

    assert "secret" not in str(exc.value)
    assert "daemon_token" not in str(exc.value)


@pytest.mark.asyncio
async def test_openai_dispatch_exception_is_sanitized() -> None:
    example = load_example("03_openai_computer_loop.py")
    computer = AsyncRecordingComputer()

    async def fail_batch(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("private daemon token")

    computer.step = fail_batch
    client, _ = _client(_response("resp_1", ("call_1", [{"type": "click", "x": 10, "y": 20}])))

    with pytest.raises(RuntimeError, match="batch failed: RuntimeError") as exc:
        await example.run_openai_computer_loop(
            client=client,
            computer=computer,
            task="Inspect the page",
        )

    assert "private daemon token" not in str(exc.value)


@pytest.mark.asyncio
async def test_openai_final_allowed_turn_stops_before_dispatch() -> None:
    example = load_example("03_openai_computer_loop.py")
    computer = AsyncRecordingComputer()
    client, _ = _client(_response("resp_1", ("call_1", [{"type": "click", "x": 10, "y": 20}])))

    with pytest.raises(RuntimeError, match="exceeded 1 turns"):
        await example.run_openai_computer_loop(
            client=client,
            computer=computer,
            task="Inspect the page",
            max_turns=1,
        )

    assert computer.steps == []


@pytest.mark.asyncio
async def test_async_owner_hands_one_versioned_handle_to_the_placed_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    example = load_example("03_openai_computer_loop.py")
    events: list[str] = []
    owner_create_kwargs: list[dict[str, Any]] = []
    remote_arguments: list[tuple[Any, ...]] = []
    handle = SimpleNamespace(protocol_version="1")

    class Owner:
        async def __aenter__(self) -> Owner:
            events.append("owner-enter")
            return self

        async def __aexit__(self, *_args: Any) -> None:
            events.append("owner-exit")

        def session_handle(self) -> Any:
            events.append("handle")
            return handle

    class Deployed:
        def __init__(self) -> None:
            self.remote = SimpleNamespace(aio=self.remote_async)

        async def remote_async(self, *args: Any) -> str:
            events.append("remote")
            remote_arguments.append(args)
            return "done"

    def create_owner(**kwargs: Any) -> Owner:
        owner_create_kwargs.append(kwargs)
        return Owner()

    environments: list[str | None] = []

    def from_name(*_args: Any, environment_name: str | None = None) -> Deployed:
        environments.append(environment_name)
        return Deployed()

    monkeypatch.setattr(
        example,
        "modal",
        SimpleNamespace(Function=SimpleNamespace(from_name=from_name)),
    )
    monkeypatch.setattr(example, "app", object())
    monkeypatch.setattr(example, "run_openai_trajectory", object())
    monkeypatch.setattr(example.AsyncComputerSandbox, "create", create_owner)

    result = await example.run_example(task="Inspect the page", model="gpt-test")

    assert result == "done"
    assert environments == [example.MODAL_ENVIRONMENT]
    assert events == ["owner-enter", "handle", "remote", "owner-exit"]
    assert len(remote_arguments) == 1
    assert remote_arguments[0][:3] == (handle, "Inspect the page", "gpt-test")
    assert str(remote_arguments[0][3]).startswith("openai_trajectory_")
    assert len(owner_create_kwargs) == 1
    config = owner_create_kwargs[0]["config"]
    assert config.runtime.modal_environment == example.MODAL_ENVIRONMENT
    assert config.runtime.modal_region == example.MODAL_REGION
    assert owner_create_kwargs[0]["app_name"] == example.APP_NAME


def test_openai_example_makes_placement_cost_and_transport_choices_explicit() -> None:
    example = load_example("03_openai_computer_loop.py")
    config = example.sandbox_configuration()
    resolved = example.resolved_trajectory_configuration()
    assert example.__spec__.origin is not None
    source = Path(example.__spec__.origin).read_text()

    assert config.ingress == "attested-tunnel"
    assert config.desktop.resolution == example.SANDBOX_RESOLUTION
    assert config.resources.profile == example.SANDBOX_RESOURCE_PROFILE
    assert config.resources.cpu == example.SANDBOX_CPU
    assert config.resources.memory_mib == example.SANDBOX_MEMORY_MIB
    assert config.image.source == example.SANDBOX_IMAGE_SOURCE
    assert config.browser.kind == example.SANDBOX_BROWSER_KIND
    assert config.browser.prewarm is example.SANDBOX_BROWSER_PREWARM
    assert config.browser.gpu_mode == example.SANDBOX_BROWSER_GPU_MODE
    assert config.runtime.timeout_seconds == example.SANDBOX_TIMEOUT_SECONDS
    assert config.runtime.idle_timeout_seconds == example.SANDBOX_IDLE_TIMEOUT_SECONDS
    assert config.runtime.readiness_timeout_seconds == example.SANDBOX_READINESS_TIMEOUT_SECONDS
    assert example.FUNCTION_MIN_CONTAINERS == 0
    assert example.FUNCTION_RETRIES == 0
    assert example.SANDBOX_WARM_POOL_CAPACITY == 0
    assert resolved["modal_environment"] == example.MODAL_ENVIRONMENT
    assert resolved["modal_region"] == example.MODAL_REGION
    assert resolved["function"] == {
        "cpu": example.FUNCTION_CPU,
        "memory_mib": example.FUNCTION_MEMORY_MIB,
        "image": {
            "base": "debian_slim",
            "python_version": example.FUNCTION_PYTHON_VERSION,
            "package": example.FUNCTION_PACKAGE_SPEC,
        },
        "timeout_seconds": example.FUNCTION_TIMEOUT_SECONDS,
        "retries": example.FUNCTION_RETRIES,
        "min_containers": example.FUNCTION_MIN_CONTAINERS,
        "max_containers": example.FUNCTION_MAX_CONTAINERS,
    }
    assert resolved["warm_capacity"] == {
        "function_min_containers": 0,
        "sandbox_pool_capacity": 0,
    }
    assert "region=MODAL_REGION" in source
    assert "cpu=FUNCTION_CPU" in source
    assert "memory=FUNCTION_MEMORY_MIB" in source
    assert "min_containers=FUNCTION_MIN_CONTAINERS" in source
    assert "retries=FUNCTION_RETRIES" in source
    assert "async with handle.borrow_async" in source
    assert "await computer.step(" in source
    assert "await computer.screenshots.full()" not in source
    assert "full_bytes(" not in source
    assert "optimized=" not in source


def test_openai_main_prints_only_the_placed_trajectory_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    example = load_example("03_openai_computer_loop.py")

    async def run_example(**_kwargs: Any) -> str:
        return "done"

    monkeypatch.setattr(example, "run_example", run_example)

    example.main()

    assert capsys.readouterr().out == "done\n"
