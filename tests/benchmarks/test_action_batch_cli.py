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
            return {"ok": True, "results": [{"ok": True} for _ in json["actions"]]}

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
                "results": [{"ok": True} for _ in json["actions"]],
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
            return {"ok": True, "results": [{"ok": True} for _ in json["actions"]]}

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


def test_four_click_ab_uses_one_batch_request_and_four_sequential_requests() -> None:
    class RecordingClient:
        base_url = "http://testserver"

        def __init__(self) -> None:
            self.requests: list[dict] = []

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            assert headers is None
            self.requests.append(json)
            return {
                "ok": True,
                "results": [
                    {"ok": True, "output": {"input_backend": "xtest"}}
                    for _ in json["actions"]
                ],
                "timing": {"daemon_ms": 1.0},
            }

    client = RecordingClient()
    payload = run_action_batch_benchmark(
        client=client,
        mode="mock-local",
        iterations=2,
        warmup_iterations=1,
        include_legacy_cases=False,
        include_four_click_cases=True,
    )

    assert payload["ok"] is True
    assert len(client.requests) == 15
    assert [len(request["actions"]) for request in client.requests] == [4, 4, 4] + [1] * 12
    expected = [(16, 16), (128, 16), (128, 128), (16, 128)]
    assert [
        (action["x"], action["y"])
        for request in client.requests[:3]
        for action in request["actions"]
    ] == expected * 3
    assert [
        (request["actions"][0]["x"], request["actions"][0]["y"])
        for request in client.requests[3:]
    ] == expected * 3
    batch = payload["cases"]["batch_4_clicks"]
    separate = payload["cases"]["separate_4_clicks"]
    assert batch["sdk_call_count"] == batch["transport_request_count"] == 1
    assert separate["sdk_call_count"] == separate["transport_request_count"] == 4
    assert payload["measurement_policy"]["replacement_samples"] == 0
    assert payload["four_click_comparison"]["metric"] == "p50"


def test_four_click_ab_records_failed_iteration_without_replacement() -> None:
    class FailsSecondMeasuredBatch:
        base_url = "http://testserver"

        def __init__(self) -> None:
            self.calls = 0

        def post_json(self, path: str, *, json=None, headers=None):
            self.calls += 1
            if self.calls == 2:
                return {"ok": False, "results": [{"ok": False}]}
            return {
                "ok": True,
                "results": [
                    {"ok": True, "output": {"input_backend": "xtest"}}
                    for _ in json["actions"]
                ],
            }

    payload = run_action_batch_benchmark(
        client=FailsSecondMeasuredBatch(),
        mode="mock-local",
        iterations=2,
        warmup_iterations=0,
        include_legacy_cases=False,
        include_four_click_cases=True,
    )

    assert payload["ok"] is False
    assert payload["cases"]["batch_4_clicks"]["successful_iterations"] == 1
    assert len(payload["cases"]["batch_4_clicks"]["samples_ms"]) == 1
    assert payload["failures"][0]["case"] == "batch_4_clicks"
    assert payload["failures"][0]["iteration"] == 1


def test_four_click_ab_rejects_missing_per_action_backend_evidence() -> None:
    class MissingBackendClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            return {
                "ok": True,
                "results": [
                    {"ok": True, "output": {"input_backend": "xtest"}},
                    *[{"ok": True} for _ in json["actions"][1:]],
                ],
            }

    payload = run_action_batch_benchmark(
        client=MissingBackendClient(),
        mode="mock-local",
        iterations=1,
        warmup_iterations=0,
        include_legacy_cases=False,
        include_four_click_cases=True,
    )

    assert payload["ok"] is False
    assert payload["cases"]["batch_4_clicks"]["successful_iterations"] == 0
    assert payload["failures"][0]["type"] == "RuntimeError"


def test_four_click_only_cli_omits_historical_cases(capsys) -> None:
    exit_code = cli.main(
        [
            "benchmark",
            "action-batch",
            "--mock-local",
            "--iterations",
            "1",
            "--warmup-iterations",
            "0",
            "--four-click-only",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert set(payload["cases"]) == {"batch_4_clicks", "separate_4_clicks"}
    assert payload["comparison"] == {"status": "not_measured"}
    serialized = json.dumps(payload).lower()
    assert "token" not in serialized
    assert "authorization" not in serialized
