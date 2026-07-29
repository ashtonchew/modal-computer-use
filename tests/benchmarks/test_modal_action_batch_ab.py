from __future__ import annotations

import copy
import inspect
import json
from pathlib import Path

import pytest

from modal_computer_use import cli
from modal_computer_use.benchmarks.modal_action_batch_ab import (
    ModalActionBatchABConfig,
    run_modal_action_batch_ab,
    run_modal_action_batch_ab_in_runner,
    validate_modal_action_batch_ab_artifact,
    validate_modal_action_batch_output_path,
)

REVISION = "a" * 40


class _Client:
    base_url = "https://sensitive-target.invalid/path?token=secret"

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.transport = type("Transport", (), {"last_http_version": "HTTP/1.1"})()

    def post_json(self, path: str, *, json=None, headers=None):
        assert path == "/v1/actions/run"
        self.requests.append(json)
        return {
            "ok": True,
            "results": [
                {"ok": True, "output": {"input_backend": "xtest"}}
                for _ in json["actions"]
            ],
            "timing": {"daemon_ms": 1.0},
        }


class _Screenshots:
    def full_bytes(self, **kwargs):
        return b"unreported-frame"


class _Computer:
    def __init__(self) -> None:
        self.client = _Client()
        self.screenshots = _Screenshots()
        self.events: list[str] = []

    def runtime_placement(self):
        return {"cloud": "aws", "region": "us-west-2"}

    def terminate(self, *, wait: bool):
        assert wait is True
        self.events.append("terminate")

    def detach(self):
        self.events.append("detach")


def test_runner_measures_only_four_click_ab_and_cleans_up(monkeypatch) -> None:
    computer = _Computer()
    monkeypatch.setattr(
        "modal_computer_use.benchmarks.modal_action_batch_ab.validate_first_frame",
        lambda *args, **kwargs: b"validated",
    )

    result = run_modal_action_batch_ab_in_runner(
        ModalActionBatchABConfig(
            region="us-west-2",
            image_revision=REVISION,
            iterations=2,
            warmup_iterations=1,
            pilot=True,
        ),
        run_tag="test-run",
        runner_placement={"cloud": "aws", "region": "us-west-2"},
        create_computer=lambda **kwargs: computer,
    )

    assert result["ok"] is True
    assert result["placement_verified"] is True
    assert result["placement"] == {
        "requested_region": "us-west-2",
        "runner": {"cloud": "aws", "region": "us-west-2"},
        "target": {"cloud": "aws", "region": "us-west-2"},
    }
    assert set(result["benchmark"]["cases"]) == {"batch_4_clicks", "separate_4_clicks"}
    assert len(computer.client.requests) == 15
    assert [len(request["actions"]) for request in computer.client.requests] == [4] * 3 + [1] * 12
    assert computer.events == ["terminate", "detach"]
    serialized = json.dumps(result).lower()
    assert "sensitive-target" not in serialized
    assert "token" not in serialized
    assert "unreported-frame" not in serialized


def test_publishable_wrapper_enforces_retries_and_terminal_cleanup() -> None:
    captured: dict = {}

    def launcher(function, **kwargs):
        captured.update(kwargs)
        assert "run_tag" in kwargs
        assert "run_id" not in kwargs
        assert "run_tag" in inspect.signature(function).parameters
        return _successful_runner_result(iterations=30)

    result = run_modal_action_batch_ab(
        ModalActionBatchABConfig(region="us-west-2", image_revision=REVISION),
        function_launcher=launcher,
        cleanup_sweep=lambda **kwargs: {
            "cleanup_succeeded": True,
            "remaining_sandboxes": 0,
        },
        run_id_factory=lambda: "safe-run-id",
    )

    assert captured["retries"] == 0
    assert result["ok"] is True
    assert result["eligibility"] == "publishable"
    assert result["replacement_samples"] == 0
    validate_modal_action_batch_ab_artifact(result)


def test_pilot_counts_are_ineligible() -> None:
    result = run_modal_action_batch_ab(
        ModalActionBatchABConfig(
            region="us-west-2",
            image_revision=REVISION,
            iterations=2,
            pilot=True,
        ),
        function_launcher=lambda function, **kwargs: _successful_runner_result(iterations=2),
        cleanup_sweep=lambda **kwargs: {
            "cleanup_succeeded": True,
            "remaining_sandboxes": 0,
        },
        run_id_factory=lambda: "safe-run-id",
    )

    assert result["ok"] is True
    assert result["eligibility"] == "pilot_ineligible"
    with pytest.raises(ValueError, match="not publishable"):
        validate_modal_action_batch_ab_artifact(result)


def test_output_path_must_stay_under_ignored_benchmark_results() -> None:
    validate_modal_action_batch_output_path(Path("benchmark-results/action-batch/final.json"))
    for invalid in (
        Path("result.json"),
        Path("docs/result.json"),
        Path("benchmark-results/../docs/result.json"),
        Path("/tmp/result.json"),
    ):
        with pytest.raises(ValueError, match="output"):
            validate_modal_action_batch_output_path(invalid)


def test_artifact_validator_rejects_unsafe_key_suffixes_and_urls() -> None:
    base = run_modal_action_batch_ab(
        ModalActionBatchABConfig(
            region="us-west-2",
            image_revision=REVISION,
            iterations=2,
            pilot=True,
        ),
        function_launcher=lambda function, **kwargs: _successful_runner_result(iterations=2),
        cleanup_sweep=lambda **kwargs: {
            "cleanup_succeeded": True,
            "remaining_sandboxes": 0,
        },
        run_id_factory=lambda: "safe-run-id",
    )
    for key, value in (
        ("accessToken", "redacted"),
        ("provider-resource-id", "redacted"),
        ("detail", "https://private.invalid/path"),
    ):
        payload = {**base, "unsafe_probe": {key: value}}
        with pytest.raises(ValueError, match=r"unsafe|forbidden"):
            validate_modal_action_batch_ab_artifact(payload, require_publishable=False)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda payload: payload["run"]["benchmark"]["measurement_policy"].__setitem__(
                "retries", 1
            ),
            "measurement policy",
        ),
        (
            lambda payload: payload["run"]["benchmark"]["cases"][
                "batch_4_clicks"
            ]["actions"].reverse(),
            "success contract",
        ),
        (
            lambda payload: payload["run"]["benchmark"]["cases"][
                "batch_4_clicks"
            ].__setitem__("input_backends", ["xdotool"]),
            "success contract",
        ),
        (
            lambda payload: payload["run"]["benchmark"]["cases"][
                "separate_4_clicks"
            ]["summary_ms"].__setitem__("p50", 3.0),
            "p50 does not match",
        ),
        (
            lambda payload: payload["run"]["benchmark"]["comparison"].__setitem__(
                "speedup", 99.0
            ),
            "comparison speedup",
        ),
        (
            lambda payload: payload["run"]["placement"]["target"].__setitem__(
                "region", "us-east-1"
            ),
            "target region",
        ),
    ],
)
def test_publishable_validator_rejects_semantic_mutations(mutation, message) -> None:
    payload = _publishable_artifact()
    mutation(payload)
    with pytest.raises(ValueError, match=message):
        validate_modal_action_batch_ab_artifact(payload)


def test_live_cli_writes_allowlisted_result_without_running_provider_suite(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    payload = {
        "schema_version": 1,
        "benchmark": "modal-action-batching-ab",
        "ok": True,
        "eligibility": "publishable",
    }
    monkeypatch.setattr(cli, "run_modal_action_batch_ab", lambda config: payload)
    validated: list[bool] = []
    monkeypatch.setattr(
        cli,
        "validate_modal_action_batch_ab_artifact",
        lambda result, *, require_publishable: validated.append(require_publishable),
    )

    exit_code = cli.main(
        [
            "benchmark",
            "modal-action-batching-ab",
            "--modal-region",
            "us-west-2",
            "--image-revision",
            REVISION,
            "--output",
            "benchmark-results/action-batch/final.json",
        ]
    )

    assert exit_code == 0
    assert validated == [True]
    assert json.loads(capsys.readouterr().out) == payload
    written = json.loads(Path("benchmark-results/action-batch/final.json").read_text())
    assert written == payload


def _successful_runner_result(*, iterations: int) -> dict:
    case_base = {
        "status": "ok",
        "iterations": iterations,
        "successful_iterations": iterations,
        "logical_action_count": 4,
        "timer_boundary": "before first SDK call through validation of final response",
        "actions": [
            {"type": "click", "x": 16, "y": 16, "button": "left"},
            {"type": "click", "x": 128, "y": 16, "button": "left"},
            {"type": "click", "x": 128, "y": 128, "button": "left"},
            {"type": "click", "x": 16, "y": 128, "button": "left"},
        ],
        "input_backends": ["xtest"],
        "transport_http_versions": ["HTTP/1.1"],
        "failures": [],
    }
    return {
        "ok": True,
        "placement_verified": True,
        "placement": {
            "requested_region": "us-west-2",
            "runner": {"cloud": "aws", "region": "us-west-2"},
            "target": {"cloud": "aws", "region": "us-west-2"},
        },
        "benchmark": {
            "status": "ok",
            "measurement_policy": {
                "timer_boundary": "complete arm at caller",
                "retries": 0,
                "replacement_samples": 0,
                "fixed_action_order": True,
            },
            "cases": {
                "batch_4_clicks": {
                    **case_base,
                    "samples_ms": [1.0] * iterations,
                    "summary_ms": {"p50": 1.0, "p95": 1.0},
                    "sdk_call_count": 1,
                    "transport_request_count": 1,
                    "batching_semantics": (
                        "one ordered action batch, validated before execution, "
                        "stop on first error"
                    ),
                },
                "separate_4_clicks": {
                    **case_base,
                    "samples_ms": [4.0] * iterations,
                    "summary_ms": {"p50": 4.0, "p95": 4.0},
                    "sdk_call_count": 4,
                    "transport_request_count": 4,
                    "batching_semantics": (
                        "four sequential requests in fixed order, stop on first error"
                    ),
                },
            },
            "comparison": {
                "status": "measured",
                "metric": "p50",
                "batch_p50_ms": 1.0,
                "separate_p50_ms": 4.0,
                "speedup": 4.0,
                "delta_ms": 3.0,
                "batch_faster": True,
            },
            "failures": [],
        },
        "target_cleanup": {"attempted": True, "succeeded": True, "error_type": None},
        "failures": [],
    }


def _publishable_artifact() -> dict:
    return {
        "schema_version": 1,
        "benchmark": "modal-action-batching-ab",
        "ok": True,
        "eligibility": "publishable",
        "iterations": 30,
        "warmup_iterations": 1,
        "replacement_samples": 0,
        "metadata": {"modal_region": "us-west-2"},
        "run": copy.deepcopy(_successful_runner_result(iterations=30)),
        "final_cleanup": {"cleanup_succeeded": True, "remaining_sandboxes": 0},
        "failures": [],
    }
