from __future__ import annotations

import json

import pytest

from modal_computer_use import cli
from modal_computer_use.benchmarks import (
    run_sandbox_exec_benchmark,
)
from modal_computer_use.errors import ModalNotInstalledError


def test_benchmark_report_include_sandbox_exec_requires_sandbox_id(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "benchmark",
                "report",
                "--base-url",
                "http://127.0.0.1:8080",
                "--include-sandbox-exec",
            ]
        )

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "--include-sandbox-exec requires --sandbox-id" in captured.err

def test_benchmark_report_mock_local_cannot_include_sandbox_exec(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "benchmark",
                "report",
                "--mock-local",
                "--include-sandbox-exec",
                "--sandbox-id",
                "sb-123",
            ]
        )

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "--include-sandbox-exec requires --base-url" in captured.err

def test_sandbox_exec_benchmark_success_uses_safe_metadata() -> None:
    class FakeProcess:
        returncode = 0

        def wait(self) -> None:
            return None

    calls = []

    def run(command: tuple[str, ...], timeout: int) -> FakeProcess:
        calls.append((command, timeout))
        return FakeProcess()

    payload = run_sandbox_exec_benchmark(
        iterations=2,
        warmup_iterations=1,
        runner=run,
    )

    assert payload["status"] == "ok"
    assert payload["successful_iterations"] == 2
    assert len(payload["samples_ms"]) == 2
    assert payload["summary_ms"]["mean"] is not None
    assert payload["command"] == {
        "tool": "xdotool",
        "action_count": 2,
        "actions": [{"type": "move"}, {"type": "click", "button": "left"}],
        "timeout_seconds": 10,
    }
    assert calls[0][0][0] == "sh"
    serialized = json.dumps(payload)
    assert "xdotool mousemove" not in serialized
    assert "stderr" not in serialized.lower()
    assert "stdout" not in serialized.lower()
    assert "/" not in serialized

def test_sandbox_exec_benchmark_nonzero_exit_is_structured() -> None:
    class FakeProcess:
        returncode = 2

        def wait(self) -> None:
            return None

    payload = run_sandbox_exec_benchmark(
        iterations=1,
        warmup_iterations=0,
        runner=lambda command, timeout: FakeProcess(),
    )

    assert payload["status"] == "failed"
    assert payload["successful_iterations"] == 0
    assert payload["failures"][0]["case"] == "sandbox_exec_move_click"
    assert payload["failures"][0]["code"] == "sandbox_exec_nonzero_exit"
    assert payload["failures"][0]["message"] == "Sandbox.exec command exited nonzero"

def test_sandbox_exec_benchmark_missing_tool_is_structured() -> None:
    class FakeProcess:
        returncode = 127

        def wait(self) -> None:
            return None

    payload = run_sandbox_exec_benchmark(
        iterations=1,
        warmup_iterations=0,
        runner=lambda command, timeout: FakeProcess(),
    )

    assert payload["status"] == "failed"
    assert payload["failures"][0]["code"] == "sandbox_exec_missing_tool"
    assert payload["failures"][0]["message"] == (
        "Sandbox.exec command could not find xdotool in the sandbox"
    )

def test_benchmark_report_live_sandbox_exec_setup_failure_is_reported(monkeypatch, capsys) -> None:
    def missing_modal(sandbox_id: str):
        raise ModalNotInstalledError("modal extra missing")

    monkeypatch.setattr(cli, "modal_sandbox_exec_runner_from_id", missing_modal)

    class SuccessfulClient:
        base_url = "http://daemon.example"
        recording_id = "rec_test"

        def close(self) -> None:
            return None

        def get_json(self, path: str, *, params=None):
            if path == "/v1/version":
                return {"daemon_version": "test"}
            if path == "/v1/capabilities":
                return {"image_profile": "browser"}
            raise AssertionError(path)

        def post_json(self, path: str, *, json=None, headers=None):
            if path == "/v1/actions/run":
                return {"ok": True, "results": [{"ok": True}]}
            if path == "/v1/screenshots/full":
                return {"format": "png", "width": 10, "height": 10, "size_bytes": 100}
            if path == "/v1/recordings":
                return {"id": self.recording_id}
            if path == f"/v1/recordings/{self.recording_id}/stop":
                return {"status": "stopped", "format": "mp4", "size_bytes": 100}
            raise AssertionError(path)

    monkeypatch.setattr(cli, "DaemonClient", lambda *args, **kwargs: SuccessfulClient())

    exit_code = cli.main(
        [
            "benchmark",
            "report",
            "--base-url",
            "http://daemon.example",
            "--token",
            "dev-token",
            "--include-sandbox-exec",
            "--sandbox-id",
            "sb-123",
            "--iterations",
            "1",
            "--modal-region",
            "us-east-1",
            "--browser",
            "chromium",
            "--image-profile",
            "browser",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 1
    assert payload["benchmarks"]["sandbox_exec"]["status"] == "failed"
    assert payload["benchmarks"]["sandbox_exec"]["failures"][0]["code"] == "modal_not_installed"
    assert payload["failures"][-1]["benchmark"] == "sandbox_exec"
    assert payload["metadata"]["environment"] == {
        "browser": "chromium",
        "image_profile": "browser",
        "modal_region": "us-east-1",
    }
    assert "dev-token" not in captured.out
    assert "Bearer" not in captured.out
    assert "novnc" not in captured.out.lower()
    assert "stderr" not in captured.out.lower()
    assert "stdout" not in captured.out.lower()
