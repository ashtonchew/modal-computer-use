from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_RUNNER_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "benchmarks"
    / "x11_shm_direct_vs_spawned_cpu_ablation_runner.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "x11_shm_direct_vs_spawned_cpu_ablation_runner", _RUNNER_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)


def test_base_runner_is_a_static_sibling_import_for_modal_automount() -> None:
    assert runner.base_runner.__name__ == "x11_shm_direct_vs_spawned_runner"
    assert "/opt/mcu-scripts/benchmarks" in runner._SCRIPT_DIRECTORIES


def test_outer_deadline_leaves_cleanup_slack_for_both_profiles() -> None:
    readiness_seconds_per_profile = 180
    bounded_work = 2 * (
        runner.PROFILE_CHILD_TIMEOUT_SECONDS + readiness_seconds_per_profile
    )
    assert bounded_work < runner.OUTER_TIMEOUT_SECONDS


def test_cpu_context_binds_exact_target_resources_and_distinct_tag(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def status() -> dict[str, object]:
        return {
            "configured_browser": "chromium",
            "prewarm": True,
            "prewarm_result": {"ok": True},
            "open_url_on_start": runner.base_runner._FIXTURE_DATA_URL,
            "windows": 1,
        }

    computer = SimpleNamespace(browser=SimpleNamespace(status=status))

    class FakeContext:
        async def __aenter__(self):
            return computer

        async def __aexit__(self, *_args: object) -> None:
            return None

    def create(**kwargs: object) -> FakeContext:
        captured.update(kwargs)
        return FakeContext()

    monkeypatch.setattr(runner.base_runner.AsyncComputerSandbox, "create", create)

    async def exercise() -> None:
        context = runner.base_runner._TargetContext(cpu=2.0, run_tag="cpu-2")
        await context.__aenter__()
        await context.__aexit__(None, None, None)

    asyncio.run(exercise())

    config = captured["config"]
    assert config.resources.cpu == 2.0
    assert config.resources.memory_mib == runner.MEMORY_MIB
    assert captured["cpu"] == (2.0, 2.0)
    assert captured["tags"] == {"benchmark_run": "cpu-2"}


def test_child_invocation_binds_authoritative_cpu_and_overwrites_claim(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def exec_child(*args: object, **kwargs: object):
        captured["args"] = args
        captured["kwargs"] = kwargs

        async def wait() -> int:
            return 0

        async def read() -> str:
            return '{"passed": true, "configured_resources": {"cpu": 1.0, "memory_bytes": 1}}'

        return SimpleNamespace(
            wait=SimpleNamespace(aio=wait),
            stdout=SimpleNamespace(read=SimpleNamespace(aio=read)),
        )

    computer = SimpleNamespace(_sandbox=SimpleNamespace(exec=SimpleNamespace(aio=exec_child)))
    result = asyncio.run(runner.base_runner._run_child(computer, cpu=2.0))

    assert result["configured_resources"] == {
        "cpu": 2.0,
        "memory_bytes": runner.MEMORY_MIB * 1024**2,
    }
    args = captured["args"]
    assert "--cpu" in args
    assert args[args.index("--cpu") + 1] == "2.0"


def test_measure_profile_uses_fresh_context_for_each_cpu(monkeypatch) -> None:
    contexts: list[tuple[float, str]] = []

    class FakeContext:
        def __init__(self, *, cpu: float, run_tag: str) -> None:
            contexts.append((cpu, run_tag))
            self.computer = SimpleNamespace(
                metadata=lambda: SimpleNamespace(sandbox_id=f"sb-{int(cpu)}")
            )

        async def __aenter__(self):
            return self.computer

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def fake_child(
        _computer: object, *, cpu: float, timeout_seconds: int
    ) -> dict[str, object]:
        assert timeout_seconds == runner.PROFILE_CHILD_TIMEOUT_SECONDS
        return {
            "passed": False,
            "configured_resources": {"cpu": cpu, "memory_bytes": 2048 * 1024**2},
        }

    async def fake_cleanup(run_tag: str) -> dict[str, object]:
        return {
            "succeeded": True,
            "remaining_sandboxes": 0,
            "survivors_before_sweep": 0,
        }

    monkeypatch.setattr(runner.base_runner, "_TargetContext", FakeContext)
    monkeypatch.setattr(runner.base_runner, "_run_child", fake_child)
    monkeypatch.setattr(runner.base_runner, "_terminal_cleanup", fake_cleanup)

    async def exercise() -> list[dict[str, object]]:
        return [
            await runner._measure_profile(
                label,
                resources,
                invocation_tag="test-invocation",
            )
            for label, resources in runner.probe.CPU_RUNS.items()
        ]

    results = asyncio.run(exercise())

    assert contexts == [
        (1.0, "test-invocation-cpu_1"),
        (2.0, "test-invocation-cpu_2"),
    ]
    assert [result["sandbox_id"] for result in results] == ["sb-1", "sb-2"]


def test_main_refuses_existing_output_before_remote_dispatch(monkeypatch, tmp_path: Path) -> None:
    output = tmp_path / "existing.json"
    output.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(
        runner,
        "run",
        pytest.fail,
    )

    with pytest.raises(SystemExit, match="already exists"):
        runner.main(str(output))

    assert output.read_text(encoding="utf-8") == "keep"
