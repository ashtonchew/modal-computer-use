from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace


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
        self.screenshots = SimpleNamespace(full=self.full)
        self.actions = SimpleNamespace(run=self.run)

    def full(self, **_kwargs: object) -> object:
        self.screenshot_calls += 1
        return object()

    def run(self, actions: list[dict[str, object]]) -> None:
        self.action_calls.append(actions)


class FakeHandle:
    def __init__(self, computer: FakeComputer) -> None:
        self.computer = computer
        self.borrow_calls: list[str] = []
        self.active = False
        self.active_during_operations: list[bool] = []

    @contextmanager
    def borrow(self, *, function_region: str) -> Iterator[FakeComputer]:
        self.borrow_calls.append(function_region)
        self.active = True
        original_full = self.computer.full
        original_run = self.computer.run

        def full(**kwargs: object) -> object:
            self.active_during_operations.append(self.active)
            return original_full(**kwargs)

        def run(actions: list[dict[str, object]]) -> None:
            self.active_during_operations.append(self.active)
            original_run(actions)

        self.computer.screenshots.full = full
        self.computer.actions.run = run
        try:
            yield self.computer
        finally:
            self.active = False


def test_one_function_body_borrows_once_across_the_complete_repeated_loop() -> None:
    computer = FakeComputer()
    handle = FakeHandle(computer)

    result = example.run_trajectory_body(
        handle,
        "private task placeholder",
        function_region="us-west",
        max_turns=3,
    )

    assert result == {"completed": True, "turns": 3}
    assert handle.borrow_calls == ["us-west"]
    assert computer.screenshot_calls == 3
    assert len(computer.action_calls) == 3
    assert handle.active_during_operations == [True] * 6
    assert handle.active is False


def test_example_preserves_native_modal_invocation_and_cancellation_calls() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "examples" / "modal_function_session_handoff.py"
    ).read_text()

    assert "@app.function(" in source
    assert 'modal.Function.from_name(APP_NAME, "run_trajectory")' in source
    assert "deployed.remote(handle, task)" in source
    assert "deployed.spawn(handle, task)" in source
    assert "call.cancel()" in source
    assert "app.run()" not in source
    assert "retries=0" in source
