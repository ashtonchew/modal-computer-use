from __future__ import annotations

import importlib.util
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from modal_computer_use import OperationResultUnavailableError


def _load_example():
    path = Path(__file__).resolve().parents[2] / "examples" / "modal_function_session_handoff.py"
    spec = importlib.util.spec_from_file_location("modal_function_session_handoff_example", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


example = _load_example()


class FakeComputer:
    def __init__(self) -> None:
        self.screenshot_calls = 0
        self.action_calls: list[list[dict[str, object]]] = []
        self.reobservation_calls = 0
        self.result_loss: OperationResultUnavailableError | None = None
        self.screenshots = SimpleNamespace(full=self.full)
        self.actions = SimpleNamespace(run=self.run)

    async def full(self, **_kwargs: object) -> object:
        self.screenshot_calls += 1
        return object()

    async def run(self, actions: list[dict[str, object]]) -> None:
        self.action_calls.append(actions)
        if self.result_loss is not None:
            raise self.result_loss

    async def observe_after_result_loss(self) -> object:
        self.reobservation_calls += 1
        return SimpleNamespace(width=1024, height=768, data_base64="private-frame")


class FakeHandle:
    def __init__(self, computer: FakeComputer, sandbox_id: str = "sandbox-a") -> None:
        self.computer = computer
        self.sandbox_id = sandbox_id
        self.borrow_calls: list[tuple[str, str]] = []
        self.borrow_environments: list[str | None] = []
        self.active = False
        self.active_during_operations: list[bool] = []

    @asynccontextmanager
    async def borrow_async(
        self, *, run_id: str, function_region: str
    ) -> AsyncIterator[FakeComputer]:
        self.borrow_calls.append((run_id, function_region))
        self.borrow_environments.append(os.environ.get("MODAL_ENVIRONMENT"))
        self.active = True
        original_full = self.computer.full
        original_run = self.computer.run

        async def full(**kwargs: object) -> object:
            self.active_during_operations.append(self.active)
            return await original_full(**kwargs)

        async def run(actions: list[dict[str, object]]) -> None:
            self.active_during_operations.append(self.active)
            await original_run(actions)

        self.computer.screenshots.full = full
        self.computer.actions.run = run
        try:
            yield self.computer
        finally:
            self.active = False


@pytest.mark.asyncio
async def test_one_function_body_borrows_once_across_the_complete_repeated_loop(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MODAL_ENVIRONMENT", "main")
    computer = FakeComputer()
    handle = FakeHandle(computer)

    result = await example.run_trajectory_body(
        handle,
        "private task placeholder",
        run_id="run-123",
        function_region="us-west",
        max_turns=3,
    )

    assert result == {"completed": True, "turns": 3}
    assert handle.borrow_calls == [("run-123", "us-west")]
    assert handle.borrow_environments == ["main"]
    assert computer.screenshot_calls == 3
    assert len(computer.action_calls) == 3
    assert handle.active_during_operations == [True] * 6
    assert handle.active is False


@pytest.mark.asyncio
async def test_function_body_reobserves_once_and_returns_only_safe_status_on_result_loss(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MODAL_ENVIRONMENT", "main")
    computer = FakeComputer()
    computer.result_loss = OperationResultUnavailableError(
        sequence=4,
        operation_kind="actions.run",
    )
    handle = FakeHandle(computer)

    result = await example.run_trajectory_body(
        handle,
        "private task placeholder",
        run_id="run-123",
        function_region="us-west",
        max_turns=3,
    )

    assert result == {
        "completed": False,
        "status": "result_unavailable",
        "sequence": 4,
        "operation_kind": "actions.run",
        "reobserved": True,
    }
    assert "private-frame" not in repr(result)
    assert computer.reobservation_calls == 1
    assert len(computer.action_calls) == 1
    assert handle.active is False


@pytest.mark.asyncio
async def test_concurrent_variant_requires_distinct_desktops(monkeypatch) -> None:
    monkeypatch.setenv("MODAL_ENVIRONMENT", "main")
    first = FakeHandle(FakeComputer(), sandbox_id="sandbox-a")
    second = FakeHandle(FakeComputer(), sandbox_id="sandbox-b")

    results = await example.run_distinct_trajectories_body(
        [first, second],
        ["task-a", "task-b"],
        ["run-a", "run-b"],
        function_region="us-west",
    )

    assert results == [
        {"completed": True, "turns": 3},
        {"completed": True, "turns": 3},
    ]
    duplicate = FakeHandle(FakeComputer(), sandbox_id="sandbox-a")
    with pytest.raises(ValueError, match="distinct desktop"):
        await example.run_distinct_trajectories_body(
            [first, duplicate],
            ["task-a", "task-b"],
            ["run-a", "run-b"],
        )


def test_example_preserves_native_modal_invocation_and_cancellation_calls() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "examples" / "modal_function_session_handoff.py"
    ).read_text()

    assert "@app.function(" in source
    assert 'modal.Function.from_name(APP_NAME, "run_trajectory")' in source
    assert "deployed.remote(handle, task, run_id)" in source
    assert "deployed.spawn(handle, task, run_id)" in source
    assert "call.cancel()" in source
    assert "app.run()" not in source
    assert "retries=0" in source
    assert "async with handle.borrow_async" in source
    assert "async def choose_action_with_model" in source
    assert "action = await choose_action_with_model" in source


def test_cancel_waits_for_terminal_call_before_owner_cleanup(monkeypatch) -> None:
    events: list[str] = []

    class Call:
        def cancel(self) -> None:
            events.append("cancel")

        def get(self) -> None:
            events.append("get")
            raise RuntimeError("terminal cancellation")

    class Deployed:
        def spawn(self, *_args: object) -> Call:
            events.append("spawn")
            return Call()

    class Owner:
        def __enter__(self):
            events.append("owner-enter")
            return self

        def __exit__(self, *_args: object) -> None:
            events.append("owner-exit")

        def session_handle(self) -> object:
            return object()

    monkeypatch.setattr(
        example,
        "modal",
        SimpleNamespace(
            Function=SimpleNamespace(from_name=lambda *_args: Deployed())
        ),
    )
    monkeypatch.setattr(example, "app", object())
    monkeypatch.setattr(example, "run_trajectory", object())
    monkeypatch.setattr(example.ComputerSandbox, "create", lambda **_kwargs: Owner())

    result = example.run_example(task="private", spawn=True, cancel_spawned=True)

    assert result == {"mode": "spawn", "cancel_requested": True}
    assert events == ["owner-enter", "spawn", "cancel", "get", "owner-exit"]
