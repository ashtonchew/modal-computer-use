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

    assert result == {"status": "succeeded"}
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

    assert result == {"status": "indeterminate"}
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
        {"status": "succeeded"},
        {"status": "succeeded"},
    ]
    duplicate = FakeHandle(FakeComputer(), sandbox_id="sandbox-a")
    with pytest.raises(ValueError, match="distinct desktop"):
        await example.run_distinct_trajectories_body(
            [first, duplicate],
            ["task-a", "task-b"],
            ["run-a", "run-b"],
        )


def test_example_declares_placement_resources_and_native_async_invocation() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "examples" / "modal_function_session_handoff.py"
    ).read_text()

    assert "@app.function(" in source
    assert "cpu=FUNCTION_CPU" in source
    assert "memory=FUNCTION_MEMORY_MIB" in source
    assert "region=FUNCTION_REGION" in source
    assert "python_version=FUNCTION_PYTHON_VERSION" in source
    assert ".pip_install(FUNCTION_PACKAGE_SPEC)" in source
    assert "min_containers=FUNCTION_MIN_CONTAINERS" in source
    assert "max_containers=FUNCTION_MAX_CONTAINERS" in source
    assert "timeout=FUNCTION_TIMEOUT_SECONDS" in source
    assert "retries=FUNCTION_RETRIES" in source
    assert "FUNCTION_MIN_CONTAINERS = 0" in source
    assert "FUNCTION_RETRIES = 0" in source
    assert "SANDBOX_WARM_POOL_CAPACITY = 0" in source
    assert "environment_name=MODAL_ENVIRONMENT" in source
    assert "async with AsyncComputerSandbox.create" in source
    assert "await deployed.remote.aio(handle, task, run_id)" in source
    assert "await deployed.spawn.aio(handle, task, run_id)" in source
    assert "await call.cancel.aio()" in source
    assert "await call.get.aio()" in source
    assert "app.run()" not in source
    assert "async with handle.borrow_async" in source
    assert "async def choose_action_with_model" in source
    assert "action = await choose_action_with_model" in source
    assert "optimized=" not in source
    assert "PERFORMANCE_PROFILE" not in source


def test_example_resolves_every_cost_and_placement_choice_without_secrets() -> None:
    resolved = example.resolved_trajectory_configuration()

    assert resolved == {
        "modal_environment": example.MODAL_ENVIRONMENT,
        "modal_region": example.FUNCTION_REGION,
        "sandbox": {
            "cpu": example.SANDBOX_CPU,
            "memory_mib": example.SANDBOX_MEMORY_MIB,
            "gpu": None,
            "resource_profile": example.SANDBOX_RESOURCE_PROFILE,
            "timeout_seconds": example.SANDBOX_TIMEOUT_SECONDS,
            "idle_timeout_seconds": example.SANDBOX_IDLE_TIMEOUT_SECONDS,
            "readiness_timeout_seconds": example.SANDBOX_READINESS_TIMEOUT_SECONDS,
            "image": {
                "source": example.SANDBOX_IMAGE_SOURCE,
                "revision": None,
                "environment_name": None,
            },
            "browser": {
                "kind": example.SANDBOX_BROWSER_KIND,
                "prewarm": example.SANDBOX_BROWSER_PREWARM,
                "gpu_mode": example.SANDBOX_BROWSER_GPU_MODE,
            },
        },
        "function": {
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
        },
        "warm_capacity": {
            "function_min_containers": example.FUNCTION_MIN_CONTAINERS,
            "sandbox_pool_capacity": example.SANDBOX_WARM_POOL_CAPACITY,
        },
    }

    rendered = repr(resolved)
    for forbidden in (
        "token-canary",
        "https://",
        "typed-text-canary",
        "clipboard-text-canary",
        "screenshot-bytes-canary",
        "artifact-bytes-canary",
    ):
        assert forbidden not in rendered


def test_example_rejects_unimplemented_sandbox_warm_capacity(monkeypatch) -> None:
    monkeypatch.setattr(example, "SANDBOX_WARM_POOL_CAPACITY", 1)

    with pytest.raises(
        ValueError,
        match="use ComputerSandboxManager and WarmPoolPolicy",
    ):
        example.sandbox_configuration()


@pytest.mark.asyncio
async def test_cancel_waits_for_terminal_call_before_async_owner_cleanup(monkeypatch) -> None:
    events: list[str] = []

    class Call:
        def __init__(self) -> None:
            self.cancel = SimpleNamespace(aio=self.cancel_async)
            self.get = SimpleNamespace(aio=self.get_async)

        async def cancel_async(self) -> None:
            events.append("cancel")

        async def get_async(self) -> None:
            events.append("get")
            raise RuntimeError("terminal cancellation")

    class Deployed:
        def __init__(self) -> None:
            self.spawn = SimpleNamespace(aio=self.spawn_async)

        async def spawn_async(self, *_args: object) -> Call:
            events.append("spawn")
            return Call()

    class Owner:
        async def __aenter__(self):
            events.append("owner-enter")
            return self

        async def __aexit__(self, *_args: object) -> None:
            events.append("owner-exit")

        def session_handle(self) -> object:
            return object()

    monkeypatch.setattr(
        example,
        "modal",
        SimpleNamespace(Function=SimpleNamespace(from_name=lambda *_args, **_kwargs: Deployed())),
    )
    monkeypatch.setattr(example, "app", object())
    monkeypatch.setattr(example, "run_trajectory", object())
    monkeypatch.setattr(
        example.AsyncComputerSandbox,
        "create",
        lambda **_kwargs: Owner(),
    )

    result = await example.run_example(
        task="private",
        spawn=True,
        cancel_spawned=True,
    )

    assert result == {"mode": "spawn", "cancel_requested": True}
    assert events == ["owner-enter", "spawn", "cancel", "get", "owner-exit"]


@pytest.mark.asyncio
async def test_external_owner_invokes_only_the_placed_function(monkeypatch) -> None:
    events: list[str] = []
    received_environment: list[str | None] = []
    owner_create_kwargs: list[dict[str, object]] = []

    class Deployed:
        def __init__(self) -> None:
            self.remote = SimpleNamespace(aio=self.remote_async)

        async def remote_async(self, *_args: object) -> dict[str, str]:
            events.append("remote")
            return {"status": "succeeded"}

    class Owner:
        async def __aenter__(self):
            events.append("owner-enter")
            return self

        async def __aexit__(self, *_args: object) -> None:
            events.append("owner-exit")

        def session_handle(self) -> object:
            events.append("handle")
            return object()

    def from_name(
        *_args: object,
        environment_name: str | None = None,
    ) -> Deployed:
        received_environment.append(environment_name)
        return Deployed()

    def create_owner(**kwargs: object) -> Owner:
        owner_create_kwargs.append(kwargs)
        return Owner()

    monkeypatch.setattr(
        example,
        "modal",
        SimpleNamespace(Function=SimpleNamespace(from_name=from_name)),
    )
    monkeypatch.setattr(example, "app", object())
    monkeypatch.setattr(example, "run_trajectory", object())
    monkeypatch.setattr(
        example.AsyncComputerSandbox,
        "create",
        create_owner,
    )

    result = await example.run_example(task="private")

    assert result == {"mode": "remote", "result": {"status": "succeeded"}}
    assert received_environment == [example.MODAL_ENVIRONMENT]
    assert len(owner_create_kwargs) == 1
    config = owner_create_kwargs[0]["config"]
    assert config.runtime.modal_environment == example.MODAL_ENVIRONMENT
    assert config.runtime.modal_region == example.FUNCTION_REGION
    assert config.runtime.timeout_seconds == example.SANDBOX_TIMEOUT_SECONDS
    assert config.runtime.idle_timeout_seconds == example.SANDBOX_IDLE_TIMEOUT_SECONDS
    assert (
        config.runtime.readiness_timeout_seconds
        == example.SANDBOX_READINESS_TIMEOUT_SECONDS
    )
    assert config.resources.cpu == example.SANDBOX_CPU
    assert config.resources.memory_mib == example.SANDBOX_MEMORY_MIB
    assert config.resources.profile == example.SANDBOX_RESOURCE_PROFILE
    assert config.image.source == example.SANDBOX_IMAGE_SOURCE
    assert config.browser.kind == example.SANDBOX_BROWSER_KIND
    assert config.browser.prewarm is example.SANDBOX_BROWSER_PREWARM
    assert config.browser.gpu_mode == example.SANDBOX_BROWSER_GPU_MODE
    assert owner_create_kwargs[0]["app_name"] == example.APP_NAME
    assert events == ["owner-enter", "handle", "remote", "owner-exit"]
