from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_example():
    path = Path(__file__).resolve().parents[2] / "examples" / "modal_colocated_runner.py"
    spec = importlib.util.spec_from_file_location("modal_colocated_runner_example", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


example = _load_example()


def test_run_colocated_command_delegates_to_sdk_runner_helper(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    computer = SimpleNamespace()

    def fake_run_modal_daemon_command_with_fallback(active_computer, command, **kwargs):
        calls.append({"computer": active_computer, "command": command, **kwargs})
        return SimpleNamespace(
            result=SimpleNamespace(sandbox_id="sb-runner", returncode=0, stdout="ok", stderr=""),
            selected_path="same-region-connect",
            fallback_used=False,
        )

    monkeypatch.setattr(
        example,
        "run_modal_daemon_command_with_fallback",
        fake_run_modal_daemon_command_with_fallback,
    )

    result = example.run_colocated_command(
        ["python", "-m", "worker"],
        computer=computer,
        app_name="app",
        runner_name="runner",
        modal_region="us-west",
        env={"WORKLOAD": "benchmark"},
        runner_cpu=1.0,
        runner_memory_mib=1024,
        exec_timeout_seconds=60,
    )

    assert result.result.sandbox_id == "sb-runner"
    assert calls == [
        {
            "computer": computer,
            "command": ("python", "-m", "worker"),
            "app_name": "app",
            "modal_region": "us-west",
            "runner_name": "runner",
            "env": {"WORKLOAD": "benchmark"},
            "runner_cpu": 1.0,
            "runner_memory_mib": 1024,
            "exec_timeout_seconds": 60,
            "external_runner": None,
        }
    ]
