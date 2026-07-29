from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from modal_computer_use import cli
from modal_computer_use.benchmarks.modal_optimized_ingress_ab import (
    ModalOptimizedIngressABConfig,
    _select_optimized_ingress,
    run_modal_optimized_ingress_ab,
    run_modal_optimized_ingress_ab_in_runner,
    validate_modal_optimized_ingress_ab_artifact,
)

REVISION = "a" * 40


def _config(
    *, iterations: int = 30, warmup_iterations: int = 2, pilot: bool = False
) -> ModalOptimizedIngressABConfig:
    return ModalOptimizedIngressABConfig(
        region="us-west-2",
        image_revision=REVISION,
        cpu=4.0,
        memory_mib=8192,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        pilot=pilot,
    )


class _Response:
    def __init__(self, *, content: bytes = b"", headers: dict[str, str] | None = None):
        self.content = content
        self.headers = headers or {}


class _Transport:
    last_http_version = "HTTP/1.1"


class _Client:
    def __init__(self, arm: str, *, elapsed: dict[str, float], events: list[str]):
        self.arm = arm
        self.elapsed = elapsed
        self.events = events
        self.transport = _Transport()
        self.closed = False

    def post_json(self, path: str, *, json=None):
        self.events.append(f"{self.arm}:{path}")
        if path == "/v1/session/tunnel-authorize":
            return {"token": "test-attested-token"}
        if path == "/v1/actions/run":
            actions = json["actions"]
            return {
                "ok": True,
                "results": [
                    {"ok": True, "output": {"input_backend": "xtest"}}
                    for _ in actions
                ],
                "timing": {"daemon_ms": 1.0},
            }
        raise AssertionError(path)

    def post_bytes_with_headers(self, path: str, *, json=None):
        self.events.append(f"{self.arm}:{path}")
        if path == "/v1/observations/transport-probe":
            assert json == {"size_bytes": 0}
            return b"", {}
        if path == "/v1/screenshots/full/raw":
            assert json == {"format": "png", "show_cursor": False}
            return _png_bytes(), {
                "x-computer-use-width": "1024",
                "x-computer-use-height": "768",
            }
        raise AssertionError(path)

    def get_json(self, path: str):
        self.events.append(f"{self.arm}:{path}")
        assert path == "/healthz"
        return {"ok": True}

    def close(self) -> None:
        self.closed = True


def test_runner_authorizes_before_warm_samples_and_interleaves_both_arms() -> None:
    events: list[str] = []
    clients: list[_Client] = []
    elapsed = {"connect": 1.0, "attested-tunnel": 2.0}

    class Computer:
        client = SimpleNamespace(base_url="https://must-not-serialize.invalid")
        screenshots = SimpleNamespace(full_bytes=lambda **_kwargs: _png_bytes())

        def runtime_placement(self):
            return {"cloud": "CLOUD_PROVIDER_AWS", "region": "us-west-2"}

        def terminate(self, *, wait=False):
            assert wait is True
            events.append("target:terminate")

        def detach(self):
            events.append("target:detach")

    def client_factory(base_url, *, token, http2):
        assert http2 is False
        arm = "connect" if "connect" in base_url else "attested-tunnel"
        client = _Client(arm, elapsed=elapsed, events=events)
        clients.append(client)
        return client

    ticks = iter(float(value) for value in range(10_000))
    result = run_modal_optimized_ingress_ab_in_runner(
        _config(iterations=4, warmup_iterations=1, pilot=True),
        runner_placement={"cloud": "CLOUD_PROVIDER_AWS", "region": "us-west-2"},
        create_computer=lambda **_kwargs: Computer(),
        connect_endpoint_factory=lambda _computer, path: SimpleNamespace(
            path=path,
            base_url="https://connect.invalid",
            token="test-connect-token",
        ),
        client_factory=client_factory,
        clock=lambda: next(ticks),
    )

    assert result["ok"] is True
    assert result["placement_verified"] is True
    assert result["connect_authorization_setup"]["completed"] is True
    authorization_index = events.index("connect:/v1/session/tunnel-authorize")
    first_attested_sample = next(
        index
        for index, event in enumerate(events)
        if event.startswith("attested-tunnel:")
    )
    assert authorization_index < first_attested_sample
    case = result["cases"]["transport_floor_0b"]
    assert case["schedule"] == "alternating paired rounds: connect/tunnel, tunnel/connect"
    assert case["arms"]["connect"]["successful_iterations"] == 4
    assert case["arms"]["attested-tunnel"]["successful_iterations"] == 4
    measured_probe_events = [
        event for event in events if event.endswith("/v1/observations/transport-probe")
    ]
    assert measured_probe_events[:6] == [
        "connect:/v1/observations/transport-probe",
        "attested-tunnel:/v1/observations/transport-probe",
        "connect:/v1/observations/transport-probe",
        "attested-tunnel:/v1/observations/transport-probe",
        "attested-tunnel:/v1/observations/transport-probe",
        "connect:/v1/observations/transport-probe",
    ]
    assert all(client.closed for client in clients)
    assert "https://" not in json.dumps(result)
    assert "test-connect-token" not in json.dumps(result)
    assert "test-attested-token" not in json.dumps(result)


def test_runner_records_authorization_failure_without_fallback_or_measurement() -> None:
    class Computer:
        client = SimpleNamespace(base_url="https://tunnel.invalid")
        screenshots = SimpleNamespace(full_bytes=lambda **_kwargs: _png_bytes())

        def runtime_placement(self):
            return {"cloud": "CLOUD_PROVIDER_AWS", "region": "us-west-2"}

        def terminate(self, *, wait=False):
            pass

        def detach(self):
            pass

    class FailingClient:
        transport = _Transport()

        def post_json(self, *_args, **_kwargs):
            raise RuntimeError("token=must-not-serialize")

        def close(self):
            pass

    result = run_modal_optimized_ingress_ab_in_runner(
        _config(iterations=1, warmup_iterations=0, pilot=True),
        runner_placement={"cloud": "CLOUD_PROVIDER_AWS", "region": "us-west-2"},
        create_computer=lambda **_kwargs: Computer(),
        connect_endpoint_factory=lambda *_args: SimpleNamespace(
            base_url="https://connect.invalid", token="secret"
        ),
        client_factory=lambda *_args, **_kwargs: FailingClient(),
    )

    assert result["ok"] is False
    assert result["connect_authorization_setup"]["completed"] is False
    assert result["cases"] == {}
    assert result["failures"] == [
        {"phase": "authorization", "iteration": -1, "exception_type": "RuntimeError"}
    ]
    assert "must-not-serialize" not in json.dumps(result)


@pytest.mark.parametrize(
    ("recurring", "expected"),
    [
        (
            {
                "screenshot_full_png": (20.0, 30.0),
                "move_and_click": (5.0, 8.0),
                "ordered_four_click_batch": (8.0, 12.0),
            },
            "connect",
        ),
        (
            {
                "screenshot_full_png": (30.0, 20.0),
                "move_and_click": (8.0, 5.0),
                "ordered_four_click_batch": (12.0, 8.0),
            },
            "attested-tunnel",
        ),
        (
            {
                "screenshot_full_png": (20.0, 19.0),
                "move_and_click": (5.0, 4.9),
                "ordered_four_click_batch": (8.0, 7.9),
            },
            None,
        ),
        (
            {
                "screenshot_full_png": (20.0, 30.0),
                "move_and_click": (5.0, 4.0),
                "ordered_four_click_batch": (8.0, 12.0),
            },
            None,
        ),
    ],
)
def test_selection_gate_is_fixed_and_does_not_use_transport_floor(recurring, expected) -> None:
    cases = {
        name: _comparison_case(connect, tunnel)
        for name, (connect, tunnel) in recurring.items()
    }
    cases["transport_floor_0b"] = _comparison_case(100.0, 1.0)
    selection = _select_optimized_ingress(cases)
    assert selection["selected_ingress"] == expected
    assert selection["requires_confirmation"] is (expected is None)
    assert selection["gate"] == {
        "minimum_recurring_score_improvement_percent": 10.0,
        "minimum_recurring_case_wins": 2,
        "maximum_losing_case_regression_percent": 5.0,
        "transport_floor_decides_selection": False,
    }


def test_outer_benchmark_dispatches_once_cleans_up_and_emits_aggregate_only() -> None:
    runner = _runner_result(iterations=30)
    result = run_modal_optimized_ingress_ab(
        _config(),
        function_launcher=lambda *_args, **_kwargs: runner,
        cleanup_sweep=lambda **_kwargs: {
            "cleanup_succeeded": True,
            "remaining_sandboxes": 0,
        },
        run_id_factory=lambda: "safe-run",
    )

    assert result["ok"] is True
    assert result["eligibility"] == "publishable"
    assert result["metadata"]["runner_invocations"] == 1
    assert result["metadata"]["target_count"] == 1
    assert result["selection"]["selected_ingress"] == "connect"
    assert "samples_ms" not in json.dumps(result)
    validate_modal_optimized_ingress_ab_artifact(result)


def test_cli_requires_publishable_counts_and_writes_only_under_ignored_results(
    monkeypatch, tmp_path, capsys
) -> None:
    seen: dict[str, object] = {}
    result = {
        "schema_version": 1,
        "benchmark": "modal-optimized-ingress-ab",
        "ok": False,
        "eligibility": "pilot_ineligible",
        "iterations": 1,
        "warmup_iterations": 0,
        "metadata": {},
        "run": {},
        "selection": {},
        "final_cleanup": {"cleanup_succeeded": True, "remaining_sandboxes": 0},
        "failures": [],
    }

    def fake_run(config):
        seen["config"] = config
        return result

    monkeypatch.setattr(cli, "run_modal_optimized_ingress_ab", fake_run)
    monkeypatch.chdir(tmp_path)
    output = "benchmark-results/ingress/final.json"
    exit_code = cli.main(
        [
            "benchmark",
            "modal-optimized-ingress-ab",
            "--modal-region",
            "us-west-2",
            "--image-revision",
            REVISION,
            "--iterations",
            "1",
            "--warmup-iterations",
            "0",
            "--pilot",
            "--output",
            output,
        ]
    )

    assert exit_code == 1
    assert seen["config"].region == "us-west-2"
    assert json.loads((tmp_path / output).read_text()) == result
    assert "modal-optimized-ingress-ab" in capsys.readouterr().out

    with pytest.raises(SystemExit, match="output must be repository-relative"):
        cli.main(
            [
                "benchmark",
                "modal-optimized-ingress-ab",
                "--modal-region",
                "us-west-2",
                "--image-revision",
                REVISION,
                "--output",
                str(tmp_path / "outside.json"),
            ]
        )


@pytest.mark.parametrize(
    "unsafe",
    [
        {"base_url": "https://secret.invalid"},
        {"token": "secret"},
        {"sandbox_id": "sb-1"},
        {"screenshot_bytes": "bytes"},
        {"typed_text": "secret"},
    ],
)
def test_artifact_validator_rejects_unsafe_fields(unsafe) -> None:
    payload = {
        "schema_version": 1,
        "benchmark": "modal-optimized-ingress-ab",
        "ok": False,
        "eligibility": "pilot_ineligible",
        "iterations": 1,
        "warmup_iterations": 0,
        "metadata": {},
        "run": {},
        "selection": {},
        "final_cleanup": {"cleanup_succeeded": True, "remaining_sandboxes": 0},
        "failures": [],
        **unsafe,
    }
    with pytest.raises(ValueError, match="unsafe"):
        validate_modal_optimized_ingress_ab_artifact(payload, require_publishable=False)


def _comparison_case(connect: float, tunnel: float) -> dict[str, object]:
    return {
        "arms": {
            "connect": {
                "status": "ok",
                "successful_iterations": 30,
                "failures": [],
                "summary_ms": {"p50": connect, "p95": connect},
            },
            "attested-tunnel": {
                "status": "ok",
                "successful_iterations": 30,
                "failures": [],
                "summary_ms": {"p50": tunnel, "p95": tunnel},
            },
        }
    }


def _runner_result(*, iterations: int) -> dict[str, object]:
    cases = {
        "transport_floor_0b": _comparison_case(2.0, 1.0),
        "screenshot_full_png": _comparison_case(20.0, 30.0),
        "move_and_click": _comparison_case(5.0, 8.0),
        "ordered_four_click_batch": _comparison_case(8.0, 12.0),
    }
    for case in cases.values():
        case["schedule"] = "alternating paired rounds: connect/tunnel, tunnel/connect"
        case["iterations_per_arm"] = iterations
        for arm in case["arms"].values():
            arm["iterations"] = iterations
            arm["successful_iterations"] = iterations
    return {
        "ok": True,
        "placement_verified": True,
        "placement": {
            "requested_region": "us-west-2",
            "runner": {"cloud": "CLOUD_PROVIDER_AWS", "region": "us-west-2"},
            "target": {"cloud": "CLOUD_PROVIDER_AWS", "region": "us-west-2"},
        },
        "connect_authorization_setup": {"completed": True, "elapsed_ms": 1.0},
        "cases": cases,
        "target_cleanup": {"attempted": True, "succeeded": True, "error_type": None},
        "failures": [],
    }


def _png_bytes() -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (1024, 768), color=(1, 2, 3)).save(buffer, format="PNG")
    return buffer.getvalue()
