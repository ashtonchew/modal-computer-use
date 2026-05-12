from __future__ import annotations

import json

import pytest

from modal_computer_use import cli
from modal_computer_use.benchmarks import run_action_batch_benchmark, run_benchmark_report


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

        def post_json(self, path: str, *, json, headers=None):
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
    assert payload["benchmarks"]["screenshot_full"]["last_result"]["size_bytes"] > 0
    assert payload["benchmarks"]["screenshot_compressed"]["summary_bytes"]["mean"] is not None
    assert payload["benchmarks"]["sandbox_exec"]["status"] == "not_measured"
    assert payload["benchmarks"]["recording_start_stop"]["status"] == "not_measured"
    assert payload["benchmarks"]["cold_create_to_ready"]["status"] == "not_measured"
    assert payload["failures"] == []
    assert "data_base64" not in captured.out
    assert '"bytes"' not in captured.out
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

        def get_json(self, path: str, *, params=None):
            if path == "/v1/version":
                return {"daemon_version": "test"}
            if path == "/v1/capabilities":
                return {"image_profile": "standard"}
            raise AssertionError(path)

        def post_json(self, path: str, *, json, headers=None):
            if path == "/v1/actions/run":
                return {"ok": True, "results": [{"ok": True}]}
            if path == "/v1/screenshots/full":
                return {"format": "png", "width": 10}
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
