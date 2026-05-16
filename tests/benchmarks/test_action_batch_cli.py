from __future__ import annotations

import json

import pytest

from modal_computer_use import cli
from modal_computer_use.benchmarks import (
    run_action_batch_benchmark,
)


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

def test_benchmark_action_batch_redacts_reported_base_url_credentials() -> None:
    class SuccessfulClient:
        base_url = "https://user:secret@example.com:443/daemon?token=secret#frag"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            return {"ok": True, "results": [{"ok": True}]}

    payload = run_action_batch_benchmark(
        client=SuccessfulClient(),
        mode="http",
        iterations=1,
        base_url=SuccessfulClient.base_url,
        warmup_iterations=0,
    )

    serialized = json.dumps(payload)
    assert payload["base_url"] == "https://example.com:443/daemon"
    assert "secret" not in serialized
    assert "token" not in serialized

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
