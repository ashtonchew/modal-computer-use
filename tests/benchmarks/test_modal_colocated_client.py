from __future__ import annotations

import ast
import json
from dataclasses import replace
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
        _requested_modal_region = "us-west"
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
        assert env["COMPUTER_USE_DAEMON_BASE_URL"] == "https://target.example.modal.host"
        assert env["COMPUTER_USE_DAEMON_TOKEN"] == "target-token"  # noqa: S105
        assert env["COMPUTER_USE_DAEMON_RUNNER_PATH"] == "inherited"
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


def test_modal_colocated_client_runs_runner_path_matrix() -> None:
    exec_paths: list[str] = []
    target_exec_paths: list[str] = []
    closed: list[str] = []

    class TargetSandbox:
        object_id = "sb-target"

        def create_connect_token(self, *, user_metadata):
            assert user_metadata["sdk"] == "modal-computer-use"
            return SimpleNamespace(url="https://connect.modal.run/sb-target", token="connect-token")

    class CreatedComputer:
        _sandbox = TargetSandbox()
        _requested_modal_region = "us-west"
        _daemon_bearer_token = "test-loopback-token"  # noqa: S105 - synthetic auth fixture.
        client = SimpleNamespace(
            base_url="https://target.example.modal.host",
            transport=SimpleNamespace(token="attested-token"),
        )

        def metadata(self):
            return SimpleNamespace(sandbox_id="sb-target")

        def terminate(self) -> None:
            closed.append("terminate")

        def detach(self) -> None:
            closed.append("detach")

    def fake_surface_benchmark(**kwargs):
        return _surface_result(
            transport_p50=30.0,
            observation_p50=80.0,
            environment=kwargs["environment_metadata"],
            surfaces=kwargs["surfaces"],
        )

    def fake_exec_once(command, **kwargs):
        metadata = json.loads(kwargs["env"]["COMPUTER_USE_BENCHMARK_METADATA_JSON"])
        path = metadata["modal_runner_path"]
        exec_paths.append(path)
        if path == "inherited":
            assert kwargs["env"]["COMPUTER_USE_DAEMON_TOKEN"] == "attested-token"  # noqa: S105
            assert kwargs["env"]["COMPUTER_USE_DAEMON_BASE_URL"] == (
                "https://target.example.modal.host"
            )
        if path == "connect":
            assert kwargs["env"]["COMPUTER_USE_DAEMON_TOKEN"] == "connect-token"  # noqa: S105
            assert kwargs["env"]["COMPUTER_USE_DAEMON_BASE_URL"] == (
                "https://connect.modal.run/sb-target"
            )
        assert kwargs["env"]["COMPUTER_USE_DAEMON_RUNNER_PATH"] == path
        result = _surface_result(
            transport_p50=10.0,
            observation_p50=20.0,
            environment=metadata,
            surfaces=["daemon-transport-floor"],
        )
        return _exec_result(result, sandbox_id=f"sb-runner-{path}")

    def fake_exec_in_target(sandbox, command, **kwargs):
        assert sandbox is CreatedComputer._sandbox
        assert kwargs["env"]["COMPUTER_USE_DAEMON_BASE_URL"] == "http://127.0.0.1:8080"
        assert kwargs["env"]["COMPUTER_USE_DAEMON_TOKEN"] == "test-loopback-token"  # noqa: S105
        assert kwargs["env"]["COMPUTER_USE_DAEMON_RUNNER_PATH"] == "target-loopback"
        metadata = json.loads(kwargs["env"]["COMPUTER_USE_BENCHMARK_METADATA_JSON"])
        path = metadata["modal_runner_path"]
        target_exec_paths.append(path)
        result = _surface_result(
            transport_p50=1.0,
            observation_p50=2.0,
            environment=metadata,
            surfaces=["daemon-transport-floor"],
        )
        return _exec_result(result, sandbox_id="sb-target")

    result = colocated.run_modal_colocated_client_benchmark(
        _config(
            surfaces=["daemon-transport-floor"],
            runner_paths=["inherited", "connect", "target-loopback"],
        ),
        run_id_factory=lambda: "modal_colocated_paths_test",
        create_computer=lambda **kwargs: CreatedComputer(),
        exec_once=fake_exec_once,
        exec_in_target=fake_exec_in_target,
        surface_benchmark=fake_surface_benchmark,
    )

    assert result["ok"] is True
    assert exec_paths == ["inherited", "connect"]
    assert target_exec_paths == ["target-loopback"]
    assert set(result["runs"]["modal_colocated_runner_paths"]) == {
        "inherited",
        "connect",
        "target-loopback",
    }
    assert result["metadata"]["runner_paths"] == ["inherited", "connect", "target-loopback"]
    assert result["metadata"]["primary_runner_path"] == "inherited"
    assert "runner_paths" in result["comparison"]
    assert "attested-token" not in json.dumps(result)
    assert "connect-token" not in json.dumps(result)
    assert closed == ["terminate", "detach"]


def test_modal_colocated_runner_code_compiles_and_records_preflight() -> None:
    code = colocated.modal_colocated_runner_code()

    ast.parse(code)

    assert "def _runner_preflight(client):" in code
    assert 'result.setdefault("metadata", {})["runner_preflight"] = runner_preflight' in code
    assert '"route": name' in code
    assert "COMPUTER_USE_DAEMON_BASE_URL" in code
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


def test_daemon_http_runner_timeout_covers_full_input_matrix() -> None:
    config = replace(
        _config(surfaces=["daemon-http"]),
        iterations=30,
    )

    assert colocated._runner_exec_timeout_seconds(config) >= 450


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


def test_modal_colocated_comparison_prefers_sdk_default_causal_case() -> None:
    external = _surface_result(
        transport_p50=50.0,
        observation_p50=80.0,
        sdk_default_observation_p50=45.0,
        environment={},
        surfaces=["daemon-transport-floor", "daemon-observation-stream"],
    )
    colocated_result = _surface_result(
        transport_p50=2.0,
        observation_p50=60.0,
        sdk_default_observation_p50=18.0,
        environment={},
        surfaces=["daemon-transport-floor", "daemon-observation-stream"],
    )

    comparison = colocated.modal_colocated_comparison(external, colocated_result)

    assert comparison["surfaces"]["daemon-observation-stream"] == {
        "surface": "daemon-observation-stream",
        "metric": "causal_action_to_frame_p50_ms",
        "case": "observation_action_click_act_and_observe_sdk_default_production",
        "external_p50_ms": 45.0,
        "colocated_p50_ms": 18.0,
        "delta_ms": -27.0,
        "ratio_vs_external": 0.4,
    }


def test_modal_colocated_comparison_reports_boundary_matched_daemon_http_cases() -> None:
    external = _surface_result(
        transport_p50=50.0,
        observation_p50=80.0,
        environment={},
        surfaces=["daemon-http"],
        daemon_http_p50={
            "screenshot_full": 240.0,
            "move_click": 169.0,
            "move_click_sequence": 173.0,
            "type_100_chars": 975.0,
            "type_1000_chars": 8589.0,
            "command_echo": 250.0,
        },
    )
    colocated_result = _surface_result(
        transport_p50=2.0,
        observation_p50=30.0,
        environment={},
        surfaces=["daemon-http"],
        daemon_http_p50={
            "screenshot_full": 25.0,
            "move_click": 4.0,
            "move_click_sequence": 18.0,
            "type_100_chars": 810.0,
            "type_1000_chars": 8010.0,
            "command_echo": 82.0,
        },
    )

    comparison = colocated.modal_colocated_comparison(external, colocated_result)

    cases = comparison["surfaces"]["daemon-http"]["cases"]
    assert cases["move_click"] == {
        "surface": "daemon-http",
        "metric": "operation_p50_ms",
        "case": "move_click",
        "external_p50_ms": 169.0,
        "colocated_p50_ms": 4.0,
        "delta_ms": -165.0,
        "ratio_vs_external": pytest.approx(4.0 / 169.0),
    }
    assert cases["move_click_sequence"]["colocated_p50_ms"] == 18.0
    assert cases["command_echo"]["external_p50_ms"] == 250.0


def test_modal_colocated_comparison_surfaces_paired_observation_cases() -> None:
    external = _surface_result(
        transport_p50=50.0,
        observation_p50=80.0,
        environment={},
        surfaces=["daemon-observation-stream"],
        paired_region_radius=True,
    )
    colocated_result = _surface_result(
        transport_p50=2.0,
        observation_p50=60.0,
        environment={},
        surfaces=["daemon-observation-stream"],
        paired_region_radius=True,
    )

    comparison = colocated.modal_colocated_comparison(external, colocated_result)

    paired = comparison["paired_observation_cases"][
        "observation_action_click_act_and_observe_paired_region_radius_ab_production"
    ]
    assert paired["external"]["paired_comparison"]["variant_win_rate"] == 0.6
    assert paired["colocated"]["paired_comparison"]["variant_win_rate"] == 0.6
    assert paired["colocated"]["baseline"]["change_region_radius"] == 96
    assert paired["colocated"]["baseline"]["dirty_frame_capture_region"]["area_px"] == {
        "p50": 65536.0
    }
    assert paired["colocated"]["baseline"]["stage_p50_ms"]["server_pre_emit_ms"] == 7.0
    assert paired["colocated"]["variant"]["change_region_radius"] == 64
    assert paired["colocated"]["variant"]["dirty_frame_capture_region"]["width_px"] == {
        "p50": 128.0
    }
    assert paired["colocated"]["variant"]["stage_p50_ms"][
        "dirty_region_confirmation_ms"
    ] == 2.2


def test_modal_colocated_latency_diagnosis_includes_selected_case_stages() -> None:
    external = _surface_result(
        transport_p50=50.0,
        observation_p50=80.0,
        sdk_default_observation_p50=45.0,
        environment={},
        surfaces=["daemon-transport-floor", "daemon-observation-stream"],
    )
    colocated_result = _surface_result(
        transport_p50=2.0,
        observation_p50=60.0,
        sdk_default_observation_p50=18.0,
        environment={},
        surfaces=["daemon-transport-floor", "daemon-observation-stream"],
    )
    colocated_case = colocated_result["surfaces"]["daemon-observation-stream"]["cases"][
        "observation_action_click_act_and_observe_sdk_default_production"
    ]
    colocated_case["latency_diagnosis"] = {
        "bottleneck": "capture_diff_or_encode",
        "reason": "capture-to-delta-ready is material",
    }
    colocated_case["sample_stability"] = {
        "status": "outlier_sensitive",
        "high_outlier_count": 1,
    }
    colocated_case["change_stage_timing_summary_ms"] = {
        "server_pre_emit_ms": {"p50": 9.0},
        "dirty_producer_wait_ms": {"p50": 2.2},
        "dirty_region_confirmation_ms": {"p50": 4.3},
        "dirty_region_confirmation_capture_ms": {"p50": 3.1},
        "dirty_region_confirmation_native_ms": {"p50": 1.2},
        "frame_poll_ms": {"p50": 0.0},
    }
    colocated_case["dirty_frame_capture_region_sources"] = ["action_region"]
    colocated_case["dirty_frame_capture_region_width_summary_px"] = {"p50": 256.0}
    colocated_case["dirty_frame_capture_region_height_summary_px"] = {"p50": 256.0}
    colocated_case["dirty_frame_capture_region_area_summary_px"] = {"p50": 65536.0}

    comparison = colocated.modal_colocated_comparison(external, colocated_result)

    stage = comparison["diagnosis"]["causal_stage_diagnosis"]
    assert stage["case"] == "observation_action_click_act_and_observe_sdk_default_production"
    assert stage["external"] is None
    assert stage["colocated"]["latency_diagnosis"]["bottleneck"] == "capture_diff_or_encode"
    assert stage["colocated"]["sample_stability"]["status"] == "outlier_sensitive"
    assert stage["colocated"]["dirty_frame_capture_region"] == {
        "width_px": {"p50": 256.0},
        "height_px": {"p50": 256.0},
        "area_px": {"p50": 65536.0},
        "sources": ["action_region"],
    }
    assert stage["colocated"]["stage_p50_ms"]["dirty_region_confirmation_ms"] == 4.3
    assert stage["colocated"]["dominant_stage"] == {
        "name": "dirty_region_confirmation_ms",
        "p50_ms": 4.3,
    }


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


def _config(
    *,
    surfaces: list[str],
    runner_paths: list[str] | None = None,
) -> colocated.ModalColocatedClientBenchmarkConfig:
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
        runner_paths=runner_paths or list(colocated.DEFAULT_MODAL_COLOCATED_RUNNER_PATHS),  # type: ignore[arg-type]
        iterations=1,
    )


def _exec_result(result: dict[str, object], *, sandbox_id: str):
    stdout = (
        f"{colocated.MODAL_COLOCATED_RESULT_START}\n"
        f"{json.dumps(result)}\n"
        f"{colocated.MODAL_COLOCATED_RESULT_END}\n"
    )
    return SimpleNamespace(
        sandbox_id=sandbox_id,
        returncode=0,
        stdout=stdout,
        stderr="",
    )


def _surface_result(
    *,
    transport_p50: float,
    observation_p50: float,
    environment: dict[str, object],
    surfaces: list[str],
    sdk_default_observation_p50: float | None = None,
    envelope_observation_p50: float | None = None,
    paired_region_radius: bool = False,
    daemon_http_p50: dict[str, float] | None = None,
) -> dict[str, object]:
    surface_results: dict[str, object] = {}
    if "daemon-http" in surfaces:
        surface_results["daemon-http"] = {
            "status": "ok",
            "metadata": {"environment": environment},
            "cases": {
                case_name: {
                    "status": "ok",
                    "summary_ms": {"p50": p50},
                }
                for case_name, p50 in (daemon_http_p50 or {}).items()
            },
        }
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
        if sdk_default_observation_p50 is not None:
            cases["observation_action_click_act_and_observe_sdk_default_production"] = {
                "status": "ok",
                "action_to_frame_summary_ms": {"p50": sdk_default_observation_p50},
            }
        if envelope_observation_p50 is not None:
            cases[
                "observation_action_click_act_and_observe_auto_signal_binary_envelope_production"
            ] = {
                "status": "ok",
                "action_to_frame_summary_ms": {"p50": envelope_observation_p50},
            }
        if paired_region_radius:
            cases[
                "observation_action_click_act_and_observe_paired_region_radius_ab_production"
            ] = _paired_region_radius_case()
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


def _paired_region_radius_case() -> dict[str, object]:
    return {
        "status": "ok",
        "metric": "paired_delta_ms",
        "delta_direction": "variant_minus_baseline",
        "negative_delta_interpretation": "variant_faster",
        "sample_stability": {"status": "stable"},
        "baseline": {
            "label": "region-radius-96",
            "frame_encoding": "binary-envelope",
            "dirty_frame_producer": "auto",
            "full_frame_fallback": False,
            "change_region_radius": 96,
            "summary_ms": {"p50": 8.5},
            "dirty_frame_capture_region_width_summary_px": {"p50": 256.0},
            "dirty_frame_capture_region_height_summary_px": {"p50": 256.0},
            "dirty_frame_capture_region_area_summary_px": {"p50": 65536.0},
            "dirty_frame_capture_region_sources": ["action_region"],
            "change_stage_timing_summary_ms": {
                "server_pre_emit_ms": {"p50": 7.0},
                "dirty_region_confirmation_ms": {"p50": 3.0},
            },
        },
        "variant": {
            "label": "region-radius-64",
            "frame_encoding": "binary-envelope",
            "dirty_frame_producer": "auto",
            "full_frame_fallback": False,
            "change_region_radius": 64,
            "summary_ms": {"p50": 7.5},
            "dirty_frame_capture_region_width_summary_px": {"p50": 128.0},
            "dirty_frame_capture_region_height_summary_px": {"p50": 128.0},
            "dirty_frame_capture_region_area_summary_px": {"p50": 16384.0},
            "dirty_frame_capture_region_sources": ["action_region"],
            "change_stage_timing_summary_ms": {
                "server_pre_emit_ms": {"p50": 6.4},
                "dirty_region_confirmation_ms": {"p50": 2.2},
            },
        },
        "paired_comparison": {
            "status": "measured",
            "samples": 10,
            "variant_wins": 6,
            "baseline_wins": 4,
            "ties": 0,
            "variant_win_rate": 0.6,
            "baseline_win_rate": 0.4,
            "delta_summary_ms": {"p50": -0.5},
        },
    }
