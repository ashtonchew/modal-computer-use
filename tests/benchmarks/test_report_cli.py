from __future__ import annotations

import json

import pytest

from modal_computer_use import cli
from modal_computer_use.benchmarks import (
    TYPE_1000_CHARS_TIMEOUT_MS,
    TYPING_BENCHMARK_TEXT,
    run_benchmark_report,
)


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
    move_sequence = payload["benchmarks"]["move_click_sequence"]
    assert move_sequence["status"] == "ok"
    assert len(move_sequence["samples_ms"]) == 2
    assert move_sequence["summary_ms"]["mean"] is not None
    assert move_sequence["daemon_samples_ms"]
    assert move_sequence["action_count"] == 8
    typing = payload["benchmarks"]["type_100_chars"]
    assert typing["status"] == "ok"
    assert len(typing["samples_ms"]) == 2
    assert typing["summary_ms"]["mean"] is not None
    assert typing["daemon_samples_ms"]
    assert typing["daemon_summary_ms"]["mean"] is not None
    assert typing["overhead_summary_ms"]["mean"] is not None
    assert typing["attribution"]["status"] == "measured"
    assert typing["request"] == {
        "character_count": 100,
        "method": "keystrokes",
        "delay_ms": 0,
    }
    typing_1000 = payload["benchmarks"]["type_1000_chars"]
    assert typing_1000["status"] == "ok"
    assert len(typing_1000["samples_ms"]) == 2
    assert typing_1000["request"] == {
        "character_count": 1000,
        "method": "keystrokes",
        "delay_ms": 0,
        "timeout_ms": TYPE_1000_CHARS_TIMEOUT_MS,
    }
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
    assert payload["failures"] == []
    assert TYPING_BENCHMARK_TEXT not in captured.out
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
    assert written == printed

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

        def post_bytes_with_headers(self, path: str, *, json=None, headers=None):
            if path == "/v1/screenshots/full/raw":
                raise RuntimeError("raw screenshot failed")
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

def test_benchmark_report_redacts_reported_base_url_credentials() -> None:
    class SuccessfulClient:
        base_url = "https://user:secret@example.com/connect?token=secret"
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
                return {"format": "png", "width": 10, "height": 10, "size_bytes": 100}
            if path == "/v1/recordings":
                return {"id": self.recording_id}
            if path == f"/v1/recordings/{self.recording_id}/stop":
                return {"status": "stopped", "format": "mp4", "size_bytes": 100}
            raise AssertionError(path)

        def post_bytes_with_headers(self, path: str, *, json=None, headers=None):
            if path == "/v1/screenshots/full/raw":
                return b"png-bytes", {
                    "x-computer-use-width": "10",
                    "x-computer-use-height": "10",
                    "x-computer-use-timing-ms": '{"total_ms":1.0}',
                }
            raise AssertionError(path)

    payload = run_benchmark_report(
        client=SuccessfulClient(),
        mode="http",
        iterations=1,
        base_url=SuccessfulClient.base_url,
        warmup_iterations=0,
    )

    serialized = json.dumps(payload)
    assert payload["base_url"] == "https://example.com/connect"
    assert "secret" not in serialized
    assert "token" not in serialized

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

        def post_bytes_with_headers(self, path: str, *, json=None, headers=None):
            if path == "/v1/screenshots/full/raw":
                return b"png-bytes", {
                    "x-computer-use-width": "10",
                    "x-computer-use-height": "10",
                    "x-computer-use-timing-ms": '{"total_ms":1.0}',
                }
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

        def post_bytes_with_headers(self, path: str, *, json=None, headers=None):
            if path == "/v1/screenshots/full/raw":
                return b"png-bytes", {
                    "x-computer-use-width": "10",
                    "x-computer-use-height": "10",
                    "x-computer-use-timing-ms": '{"total_ms":1.0}',
                }
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
