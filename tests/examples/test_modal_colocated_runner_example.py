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


def test_colocated_runner_env_includes_only_ephemeral_target_details() -> None:
    env = example.colocated_runner_env(
        example.ColocatedRunnerTarget(
            base_url="https://daemon.example.modal.host",
            token="secret-token",
            sandbox_id="sb-target",
        )
    )

    assert env == {
        "COMPUTER_USE_DAEMON_BASE_URL": "https://daemon.example.modal.host",
        "COMPUTER_USE_DAEMON_TOKEN": "secret-token",
        "COMPUTER_USE_TARGET_SANDBOX_ID": "sb-target",
    }


def test_run_colocated_command_delegates_to_modal_sandbox_exec_once(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_exec_once(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return SimpleNamespace(sandbox_id="sb-runner", returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(example, "modal_sandbox_exec_once", fake_exec_once)

    result = example.run_colocated_command(
        ("python", "-m", "worker"),
        target=example.ColocatedRunnerTarget(
            base_url="https://daemon.example.modal.host",
            token=None,
            sandbox_id="sb-target",
        ),
        app_name="app",
        runner_name="runner",
        modal_region="us-west",
        env={"WORKLOAD": "benchmark"},
        runner_cpu=1.0,
        runner_memory_mib=1024,
        exec_timeout_seconds=60,
    )

    assert result.sandbox_id == "sb-runner"
    assert calls == [
        {
            "command": ("python", "-m", "worker"),
            "app_name": "app",
            "name": "runner",
            "region": "us-west",
            "env": {
                "COMPUTER_USE_DAEMON_BASE_URL": "https://daemon.example.modal.host",
                "COMPUTER_USE_TARGET_SANDBOX_ID": "sb-target",
                "WORKLOAD": "benchmark",
            },
            "tags": {"computer-use.runner": "colocated"},
            "cpu": 1.0,
            "memory_mib": 1024,
            "exec_timeout_seconds": 60,
        }
    ]
