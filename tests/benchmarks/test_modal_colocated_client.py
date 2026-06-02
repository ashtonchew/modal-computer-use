from __future__ import annotations

import ast
import json
from types import SimpleNamespace

import pytest

from modal_computer_use.benchmarks import modal_colocated_client as colocated
from modal_computer_use.config import ComputerConfig
from modal_computer_use.errors import SandboxUnavailableError


def test_modal_colocated_client_runs_selected_surfaces_for_external_and_runner() -> None:
    created: dict[str, object] = {}
    closed: list[str] = []
    exec_calls: list[dict[str, object]] = []
    surfaces = ["daemon-transport-floor", "daemon-observation-stream"]

    class CreatedComputer:
        client = SimpleNamespace(
            base_url="https://target.example.modal.host",
            transport=SimpleNamespace(token="target-token"),
        )

        def metadata(self):
            return SimpleNamespace(sandbox_id="sb-target")

        def terminate(self) -> None:
            closed.append("terminate")

        def detach(self) -> None:
            closed.append("detach")

    def fake_create(**kwargs):
        created.update(kwargs)
        return CreatedComputer()

    def fake_surface_benchmark(**kwargs):
        assert kwargs["surfaces"] == surfaces
        assert kwargs["observation_cases"] is None
        environment = kwargs["environment_metadata"]
        assert environment["modal_colocation_role"] == "external-caller"
        return _surface_result(
            transport_p50=30.0,
            observation_p50=80.0,
            environment=environment,
            surfaces=surfaces,
        )

    def fake_exec_once(command, **kwargs):
        exec_calls.append({"command": command, **kwargs})
        assert kwargs["region"] == "us-west"
        assert kwargs["name"] == "colocated-runner"
        assert kwargs["exec_timeout_seconds"] == 900
        env = kwargs["env"]
        assert env["COMPUTER_USE_BENCHMARK_TOKEN"] == "target-token"  # noqa: S105
        assert json.loads(env["COMPUTER_USE_BENCHMARK_SURFACES_JSON"]) == surfaces
        assert json.loads(env["COMPUTER_USE_BENCHMARK_OBSERVATION_CASES_JSON"]) is None
        metadata = json.loads(env["COMPUTER_USE_BENCHMARK_METADATA_JSON"])
        assert metadata["modal_colocation_role"] == "modal-colocated-runner"
        result = _surface_result(
            transport_p50=12.0,
            observation_p50=25.0,
            environment=metadata,
            surfaces=surfaces,
        )
        stdout = (
            f"{colocated.MODAL_COLOCATED_RESULT_START}\n"
            f"{json.dumps(result)}\n"
            f"{colocated.MODAL_COLOCATED_RESULT_END}\n"
        )
        return SimpleNamespace(
            sandbox_id="sb-runner",
            returncode=0,
            stdout=stdout,
            stderr="",
        )

    result = colocated.run_modal_colocated_client_benchmark(
        _config(surfaces=surfaces),
        run_id_factory=lambda: "modal_colocated_test",
        create_computer=fake_create,
        exec_once=fake_exec_once,
        surface_benchmark=fake_surface_benchmark,
    )

    assert result["ok"] is True
    assert created["tags"] == {
        "benchmark": "modal-colocated-client",
        "benchmark_run_id": "modal_colocated_test",
        "role": "target",
    }
    assert result["comparison"]["surfaces"]["daemon-transport-floor"] == {
        "surface": "daemon-transport-floor",
        "metric": "fastest_floor_p50_ms",
        "case": "transport_floor_websocket_binary_envelope_0b",
        "external_p50_ms": 30.0,
        "colocated_p50_ms": 12.0,
        "delta_ms": -18.0,
        "ratio_vs_external": 0.4,
    }
    assert result["comparison"]["surfaces"]["daemon-observation-stream"] == {
        "surface": "daemon-observation-stream",
        "metric": "causal_action_to_frame_p50_ms",
        "case": "observation_action_click_act_and_observe_auto_signal_production",
        "external_p50_ms": 80.0,
        "colocated_p50_ms": 25.0,
        "delta_ms": -55.0,
        "ratio_vs_external": 0.3125,
    }
    runner_environment = result["runs"]["modal_colocated_runner"]["metadata"]["environment"]
    assert runner_environment["modal_runner_sandbox_id"] == "sb-runner"
    assert runner_environment["caller_region_label"] == "modal-runner:us-west"
    assert "target-token" not in json.dumps(result)
    assert exec_calls[0]["app_tags"] == {
        "benchmark": "modal-colocated-client",
        "benchmark_run_id": "modal_colocated_test",
    }
    assert closed == ["terminate", "detach"]


def test_modal_colocated_runner_code_compiles_and_records_preflight() -> None:
    code = colocated.modal_colocated_runner_code()

    ast.parse(code)

    assert "def _runner_preflight(client):" in code
    assert 'result.setdefault("metadata", {})["runner_preflight"] = runner_preflight' in code
    assert '"route": name' in code
    assert "COMPUTER_USE_BENCHMARK_TOKEN" in code


def test_modal_colocated_runner_failure_is_structured_without_output() -> None:
    result = colocated.modal_colocated_runner_failure(
        SimpleNamespace(returncode=2, stdout="secret", stderr="secret"),
        metadata={"modal_region": "us-west", "token": None},
        surfaces=["daemon-transport-floor", "daemon-observation-stream"],
    )

    assert result["ok"] is False
    assert [failure["surface"] for failure in result["failures"]] == [
        "daemon-transport-floor",
        "daemon-observation-stream",
    ]
    assert "secret" not in json.dumps(result)


def test_modal_colocated_latency_diagnosis_identifies_daemon_bound() -> None:
    external = _surface_result(
        transport_p50=50.0,
        observation_p50=80.0,
        environment={},
        surfaces=["daemon-transport-floor", "daemon-observation-stream"],
    )
    colocated_result = _surface_result(
        transport_p50=2.0,
        observation_p50=60.0,
        environment={},
        surfaces=["daemon-transport-floor", "daemon-observation-stream"],
    )

    comparison = colocated.modal_colocated_comparison(external, colocated_result)

    assert comparison["diagnosis"]["likely_bound"] == "daemon_action_capture_or_change_detection"
    assert (
        comparison["diagnosis"]["interpretation"]
        == "co-location reduced raw transport more than causal action-observe; daemon action, "
        "damage, capture, or diff work remains material."
    )


def test_modal_colocated_latency_diagnosis_identifies_placement_bound() -> None:
    external = _surface_result(
        transport_p50=50.0,
        observation_p50=80.0,
        environment={},
        surfaces=["daemon-transport-floor", "daemon-observation-stream"],
    )
    colocated_result = _surface_result(
        transport_p50=20.0,
        observation_p50=40.0,
        environment={},
        surfaces=["daemon-transport-floor", "daemon-observation-stream"],
    )

    comparison = colocated.modal_colocated_comparison(external, colocated_result)

    assert comparison["diagnosis"]["likely_bound"] == "caller_placement_or_modal_receive_floor"


def test_modal_colocated_latency_diagnosis_identifies_framing_win() -> None:
    external = _surface_result(
        transport_p50=50.0,
        observation_p50=80.0,
        environment={},
        surfaces=["daemon-transport-floor", "daemon-observation-stream"],
        envelope_observation_p50=60.0,
    )
    colocated_result = _surface_result(
        transport_p50=40.0,
        observation_p50=70.0,
        environment={},
        surfaces=["daemon-transport-floor", "daemon-observation-stream"],
        envelope_observation_p50=55.0,
    )

    comparison = colocated.modal_colocated_comparison(external, colocated_result)

    diagnosis = comparison["diagnosis"]
    assert diagnosis["likely_bound"] == "websocket_message_framing"
    assert diagnosis["causal_framing"]["material_envelope_win"] is True
    assert (
        diagnosis["causal_framing"]["cases"]["auto_signal"]["external"][
            "binary_envelope_p50_ms"
        ]
        == 60.0
    )


def test_modal_colocated_latency_diagnosis_does_not_overclaim_partial_framing() -> None:
    external = _surface_result(
        transport_p50=50.0,
        observation_p50=80.0,
        environment={},
        surfaces=["daemon-transport-floor", "daemon-observation-stream"],
        envelope_observation_p50=60.0,
    )
    colocated_result = _surface_result(
        transport_p50=40.0,
        observation_p50=70.0,
        environment={},
        surfaces=["daemon-transport-floor", "daemon-observation-stream"],
    )

    comparison = colocated.modal_colocated_comparison(external, colocated_result)

    diagnosis = comparison["diagnosis"]
    assert diagnosis["likely_bound"] == "partial_websocket_message_framing_evidence"
    assert diagnosis["causal_framing"]["material_envelope_wins"] == [
        {"case_group": "auto_signal", "caller_path": "external"}
    ]


def test_extract_modal_colocated_result_rejects_missing_markers() -> None:
    with pytest.raises(SandboxUnavailableError):
        colocated.extract_modal_colocated_result("{}")


def _config(*, surfaces: list[str]) -> colocated.ModalColocatedClientBenchmarkConfig:
    return colocated.ModalColocatedClientBenchmarkConfig(
        app_name="modal-computer-use",
        name="colocated",
        target_config_factory=lambda run_id: ComputerConfig(run_id=run_id),
        modal_region="us-west",
        caller_region_label="dev-laptop-us-west",
        modal_ingress="attested-tunnel",
        daemon_http_version="1.1",
        resource_profile="standard",
        browser=None,
        gpu=None,
        modal_cpu=None,
        modal_memory_mib=None,
        runner_cpu=None,
        runner_memory_mib=None,
        input_rate_limit_per_sec=0,
        image_profile=None,
        surfaces=surfaces,  # type: ignore[arg-type]
        observation_cases=None,
        iterations=1,
    )


def _surface_result(
    *,
    transport_p50: float,
    observation_p50: float,
    environment: dict[str, object],
    surfaces: list[str],
    envelope_observation_p50: float | None = None,
) -> dict[str, object]:
    surface_results: dict[str, object] = {}
    if "daemon-transport-floor" in surfaces:
        surface_results["daemon-transport-floor"] = {
            "status": "ok",
            "metadata": {"environment": environment},
            "transport_floor_summary": {
                "fastest_floor_case": {
                    "case": "transport_floor_websocket_binary_envelope_0b",
                    "requested_size_bytes": 0,
                    "p50_ms": transport_p50,
                    "inlier_mean_ms": transport_p50,
                    "outlier_count": 0,
                    "transport_encoding": "websocket_binary_envelope",
                }
            },
        }
    if "daemon-observation-stream" in surfaces:
        cases: dict[str, object] = {
            "observation_action_click_act_and_observe_auto_signal_production": {
                "status": "ok",
                "action_to_frame_summary_ms": {"p50": observation_p50},
            }
        }
        if envelope_observation_p50 is not None:
            cases[
                "observation_action_click_act_and_observe_auto_signal_binary_envelope_production"
            ] = {
                "status": "ok",
                "action_to_frame_summary_ms": {"p50": envelope_observation_p50},
            }
        surface_results["daemon-observation-stream"] = {
            "status": "ok",
            "metadata": {"environment": environment},
            "cases": cases,
        }
    return {
        "ok": True,
        "benchmark": "sdk-surfaces",
        "mode": "http",
        "metadata": {"environment": environment, "surfaces": surfaces},
        "surfaces": surface_results,
        "failures": [],
    }
