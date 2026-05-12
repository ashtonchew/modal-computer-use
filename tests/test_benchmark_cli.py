from __future__ import annotations

import json

import pytest

from modal_computer_use import cli
from modal_computer_use.benchmarks import (
    run_action_batch_benchmark,
    run_benchmark_report,
    run_sandbox_exec_benchmark,
)
from modal_computer_use.errors import ModalNotInstalledError


def test_benchmark_action_batch_mock_local_outputs_json(capsys) -> None:
    exit_code = cli.main(["benchmark", "action-batch", "--mock-local", "--iterations", "2"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["benchmark"] == "action-batch"
    assert payload["mode"] == "mock-local"
    assert payload["iterations"] == 2
    assert payload["action_count"] == 5
    assert len(payload["cases"]["batch_5_actions"]["samples_ms"]) == 2
    assert len(payload["cases"]["separate_5_actions"]["samples_ms"]) == 2
    assert payload["cases"]["batch_5_actions"]["summary_ms"]["mean"] is not None
    assert payload["cases"]["separate_5_actions"]["summary_ms"]["p95"] is not None
    assert payload["cases"]["sandbox_exec"]["status"] == "not_measured"
    assert payload["comparison"]["status"] == "measured"
    assert "hello" not in captured.out
    assert "secret" not in captured.out.lower()
    assert '"text"' not in captured.out


def test_benchmark_action_batch_invalid_iterations_rejected(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["benchmark", "action-batch", "--mock-local", "--iterations", "0"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "must be >= 1" in captured.err


def test_benchmark_action_batch_requires_one_mode(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["benchmark", "action-batch"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "one of the arguments" in captured.err


def test_benchmark_action_batch_failure_is_structured(monkeypatch, capsys) -> None:
    def fail(*, iterations: int):
        return {
            "ok": False,
            "benchmark": "action-batch",
            "iterations": iterations,
            "action_count": 5,
            "cases": {},
            "comparison": {"status": "not_available"},
            "failures": [
                {
                    "case": "batch_5_actions",
                    "phase": "measure",
                    "iteration": 0,
                    "type": "RuntimeError",
                    "message": "boom",
                }
            ],
        }

    monkeypatch.setattr(cli, "run_action_batch_benchmark_mock_local", fail)

    exit_code = cli.main(["benchmark", "action-batch", "--mock-local", "--iterations", "1"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["failures"][0]["case"] == "batch_5_actions"
    assert payload["failures"][0]["message"] == "boom"


def test_benchmark_action_batch_non_ok_response_is_structured() -> None:
    class FailingClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            assert headers is None
            return {"ok": False, "results": []}

    payload = run_action_batch_benchmark(
        client=FailingClient(),
        mode="mock-local",
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["ok"] is False
    assert payload["cases"]["batch_5_actions"]["status"] == "failed"
    assert payload["failures"][0]["case"] == "batch_5_actions"
    assert payload["failures"][0]["phase"] == "measure"
    assert payload["failures"][0]["type"] == "RuntimeError"


def test_benchmark_action_batch_records_daemon_timing_attribution() -> None:
    class TimedClient:
        base_url = "http://testserver"
        calls = 0

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            assert headers is None
            self.calls += 1
            return {
                "ok": True,
                "results": [{"ok": True}],
                "timing": {"daemon_ms": float(self.calls)},
            }

    payload = run_action_batch_benchmark(
        client=TimedClient(),
        mode="mock-local",
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["ok"] is True
    batch = payload["cases"]["batch_5_actions"]
    separate = payload["cases"]["separate_5_actions"]
    assert batch["attribution"]["status"] == "measured"
    assert batch["daemon_samples_ms"] == [1.0]
    assert batch["daemon_summary_ms"]["mean"] == 1.0
    assert batch["overhead_summary_ms"]["mean"] is not None
    assert separate["attribution"]["status"] == "measured"
    assert separate["daemon_samples_ms"] == [20.0]


def test_benchmark_action_batch_missing_timing_is_unavailable_not_failure() -> None:
    class OldClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            return {"ok": True, "results": [{"ok": True}]}

    payload = run_action_batch_benchmark(
        client=OldClient(),
        mode="mock-local",
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["ok"] is True
    assert payload["cases"]["batch_5_actions"]["attribution"] == {
        "status": "unavailable",
        "reason": "daemon response did not include timing.daemon_ms",
    }
    assert payload["cases"]["batch_5_actions"]["daemon_samples_ms"] == []
    assert payload["failures"] == []


def test_benchmark_action_batch_malformed_timing_is_structured_failure() -> None:
    class MalformedTimingClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            return {"ok": True, "results": [{"ok": True}], "timing": {"daemon_ms": "fast"}}

    payload = run_action_batch_benchmark(
        client=MalformedTimingClient(),
        mode="mock-local",
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["ok"] is False
    assert payload["cases"]["batch_5_actions"]["status"] == "failed"
    assert payload["failures"][0]["case"] == "batch_5_actions"
    assert payload["failures"][0]["type"] == "RuntimeError"
    assert payload["failures"][0]["message"] == "daemon action timing.daemon_ms was malformed"


def test_benchmark_report_mock_local_outputs_release_report(capsys) -> None:
    exit_code = cli.main(["benchmark", "report", "--mock-local", "--iterations", "2"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["mode"] == "mock-local"
    assert payload["iterations"] == 2
    assert payload["warmup_iterations"] == 1
    assert payload["package_version"]
    assert payload["generated_at"]
    assert payload["metadata"]["version"]["daemon_version"]
    assert payload["metadata"]["capabilities"]["image_profile"]
    assert len(payload["benchmarks"]["action_batch"]["cases"]["batch_5_actions"]["samples_ms"]) == 2
    assert len(payload["benchmarks"]["screenshot_full"]["samples_ms"]) == 2
    assert len(payload["benchmarks"]["screenshot_compressed"]["samples_ms"]) == 2
    assert len(payload["benchmarks"]["move_click"]["samples_ms"]) == 2
    assert payload["benchmarks"]["move_click"]["summary_ms"]["mean"] is not None
    assert payload["benchmarks"]["move_click"]["attribution"]["status"] == "measured"
    assert len(payload["benchmarks"]["move_click"]["daemon_samples_ms"]) == 2
    assert payload["benchmarks"]["move_click"]["daemon_summary_ms"]["mean"] is not None
    assert payload["benchmarks"]["move_click"]["overhead_summary_ms"]["mean"] is not None
    assert payload["benchmarks"]["move_click"]["action_count"] == 2
    assert (
        payload["benchmarks"]["action_batch"]["cases"]["separate_5_actions"]["attribution"][
            "status"
        ]
        == "measured"
    )
    assert (
        len(
            payload["benchmarks"]["action_batch"]["cases"]["separate_5_actions"][
                "daemon_samples_ms"
            ]
        )
        == 2
    )
    recording = payload["benchmarks"]["recording_start_stop"]
    assert len(recording["start_samples_ms"]) == 2
    assert len(recording["stop_samples_ms"]) == 2
    assert recording["start_summary_ms"]["mean"] is not None
    assert recording["stop_summary_ms"]["p95"] is not None
    assert recording["last_result"]["status"] == "stopped"
    assert recording["last_result"]["format"] == "mp4"
    assert recording["last_result"]["size_bytes"] > 0
    assert recording["last_result"]["duration_seconds"] is not None
    assert payload["benchmarks"]["screenshot_full"]["last_result"]["size_bytes"] > 0
    assert payload["benchmarks"]["screenshot_compressed"]["summary_bytes"]["mean"] is not None
    assert payload["benchmarks"]["sandbox_exec"]["status"] == "not_measured"
    assert payload["benchmarks"]["cold_create_to_ready"]["status"] == "not_measured"
    assert payload["benchmarks"]["type_100_chars"]["status"] == "not_measured"
    assert payload["failures"] == []
    assert "data_base64" not in captured.out
    assert '"bytes"' not in captured.out
    assert '"path"' not in captured.out
    assert "artifact://" not in captured.out
    assert "mock recording" not in captured.out
    assert "hello" not in captured.out
    assert "clipboard text" not in captured.out.lower()
    assert "dev-token" not in captured.out
    assert "Bearer" not in captured.out
    assert "novnc" not in captured.out.lower()


def test_benchmark_report_writes_output_and_prints_json(tmp_path, capsys) -> None:
    output = tmp_path / "benchmark-report.json"

    exit_code = cli.main(
        [
            "benchmark",
            "report",
            "--mock-local",
            "--iterations",
            "1",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    printed = json.loads(captured.out)
    written = json.loads(output.read_text())
    assert exit_code == 0
    assert printed["ok"] is True
    assert written["benchmarks"].keys() == printed["benchmarks"].keys()


def test_benchmark_report_invalid_iterations_rejected(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["benchmark", "report", "--mock-local", "--iterations", "0"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "must be >= 1" in captured.err


def test_benchmark_report_failure_is_structured(monkeypatch, capsys) -> None:
    def fail(*, iterations: int):
        return {
            "ok": False,
            "generated_at": "2026-05-11T00:00:00+00:00",
            "package_version": "0.0.0",
            "mode": "mock-local",
            "base_url": "http://testserver",
            "iterations": iterations,
            "warmup_iterations": 1,
            "metadata": {},
            "benchmarks": {
                "action_batch": {"status": "ok"},
                "screenshot_full": {
                    "status": "failed",
                    "samples_ms": [],
                    "summary_ms": {},
                    "failures": [
                        {
                            "case": "screenshot_full",
                            "phase": "measure",
                            "iteration": 0,
                            "type": "RuntimeError",
                            "message": "boom",
                        }
                    ],
                },
            },
            "failures": [
                {
                    "benchmark": "screenshot_full",
                    "case": "screenshot_full",
                    "phase": "measure",
                    "iteration": 0,
                    "type": "RuntimeError",
                    "message": "boom",
                }
            ],
        }

    monkeypatch.setattr(cli, "run_benchmark_report_mock_local", fail)

    exit_code = cli.main(["benchmark", "report", "--mock-local", "--iterations", "1"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["failures"][0]["benchmark"] == "screenshot_full"
    assert payload["failures"][0]["message"] == "boom"


def test_benchmark_report_screenshot_failure_is_structured() -> None:
    class FailingClient:
        base_url = "http://testserver"
        recording_id = "rec_test"

        def get_json(self, path: str, *, params=None):
            if path == "/v1/version":
                return {"daemon_version": "test"}
            if path == "/v1/capabilities":
                return {"image_profile": "standard"}
            raise AssertionError(path)

        def post_json(self, path: str, *, json=None, headers=None):
            if path == "/v1/actions/run":
                return {"ok": True, "results": [{"ok": True}]}
            if path == "/v1/screenshots/full":
                return {"format": "png", "width": 10}
            if path == "/v1/recordings":
                return {"id": self.recording_id}
            if path == f"/v1/recordings/{self.recording_id}/stop":
                return {"status": "stopped", "format": "mp4", "size_bytes": 100}
            raise AssertionError(path)

    payload = run_benchmark_report(
        client=FailingClient(),
        mode="mock-local",
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["ok"] is False
    assert payload["benchmarks"]["action_batch"]["status"] == "ok"
    assert payload["benchmarks"]["screenshot_full"]["status"] == "failed"
    assert payload["failures"][0]["benchmark"] == "screenshot_full"
    assert payload["failures"][0]["type"] == "RuntimeError"


def test_benchmark_report_move_click_failure_is_structured() -> None:
    class FailingMoveClient:
        base_url = "http://testserver"
        recording_id = "rec_test"

        def get_json(self, path: str, *, params=None):
            if path == "/v1/version":
                return {"daemon_version": "test"}
            if path == "/v1/capabilities":
                return {"image_profile": "standard"}
            raise AssertionError(path)

        def post_json(self, path: str, *, json=None, headers=None):
            if path == "/v1/actions/run":
                if len(json["actions"]) == 2:
                    return {"ok": False, "results": []}
                return {"ok": True, "results": [{"ok": True}]}
            if path == "/v1/screenshots/full":
                return {"format": "png", "width": 10, "height": 10, "size_bytes": 100}
            if path == "/v1/recordings":
                return {"id": self.recording_id}
            if path == f"/v1/recordings/{self.recording_id}/stop":
                return {"status": "stopped", "format": "mp4", "size_bytes": 100}
            raise AssertionError(path)

    payload = run_benchmark_report(
        client=FailingMoveClient(),
        mode="mock-local",
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["ok"] is False
    assert payload["benchmarks"]["move_click"]["status"] == "failed"
    assert payload["failures"][0]["benchmark"] == "move_click"
    assert payload["failures"][0]["case"] == "move_click"


def test_benchmark_report_recording_failure_is_structured() -> None:
    class FailingRecordingClient:
        base_url = "http://testserver"

        def get_json(self, path: str, *, params=None):
            if path == "/v1/version":
                return {"daemon_version": "test"}
            if path == "/v1/capabilities":
                return {"image_profile": "standard"}
            raise AssertionError(path)

        def post_json(self, path: str, *, json=None, headers=None):
            if path == "/v1/actions/run":
                return {"ok": True, "results": [{"ok": True}]}
            if path == "/v1/screenshots/full":
                return {"format": "png", "width": 10, "height": 10, "size_bytes": 100}
            if path == "/v1/recordings":
                return {"id": "rec_test"}
            if path == "/v1/recordings/rec_test/stop":
                return {"status": "failed", "format": "mp4", "size_bytes": 0}
            raise AssertionError(path)

    payload = run_benchmark_report(
        client=FailingRecordingClient(),
        mode="mock-local",
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["ok"] is False
    assert payload["benchmarks"]["recording_start_stop"]["status"] == "failed"
    assert payload["failures"][0]["benchmark"] == "recording_start_stop"
    assert payload["failures"][0]["case"] == "recording_stop"
    assert payload["failures"][0]["type"] == "RuntimeError"
    assert payload["failures"][0]["message"] == "daemon recording status was failed"


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
        "modal_region": "us-east-1",
    }
    assert "dev-token" not in captured.out
    assert "Bearer" not in captured.out
    assert "novnc" not in captured.out.lower()
    assert "stderr" not in captured.out.lower()
    assert "stdout" not in captured.out.lower()
