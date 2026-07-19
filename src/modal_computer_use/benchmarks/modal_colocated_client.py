from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from textwrap import dedent
from typing import Any, Literal

from ..errors import SandboxUnavailableError
from ..sandbox import (
    ComputerSandbox,
    modal_sandbox_exec_in_place,
    modal_sandbox_exec_once,
    run_modal_daemon_command,
)
from ..state import new_run_id
from .constants import BenchmarkSurface
from .provenance import benchmark_provenance
from .surfaces import run_sdk_surface_benchmark

ModalColocatedRunnerPath = Literal["inherited", "connect", "target-loopback"]

DEFAULT_MODAL_COLOCATED_SURFACES: tuple[BenchmarkSurface, ...] = ("daemon-transport-floor",)
MODAL_COLOCATED_ALLOWED_SURFACES: tuple[BenchmarkSurface, ...] = (
    "daemon-transport-floor",
    "daemon-observation-stream",
)
DEFAULT_MODAL_COLOCATED_RUNNER_PATHS: tuple[ModalColocatedRunnerPath, ...] = ("inherited",)
MODAL_COLOCATED_ALLOWED_RUNNER_PATHS: tuple[ModalColocatedRunnerPath, ...] = (
    "inherited",
    "connect",
    "target-loopback",
)
MODAL_COLOCATED_RESULT_START = "__MODAL_COMPUTER_USE_RESULT_START__"
MODAL_COLOCATED_RESULT_END = "__MODAL_COMPUTER_USE_RESULT_END__"


@dataclass(frozen=True)
class ModalColocatedClientBenchmarkConfig:
    app_name: str
    name: str | None
    target_config_factory: Callable[[str], Any]
    modal_region: str
    caller_region_label: str | None
    modal_ingress: str
    daemon_http_version: str
    resource_profile: str | None
    browser: str | None
    gpu: str | None
    modal_cpu: float | None
    modal_memory_mib: int | None
    runner_cpu: float | None
    runner_memory_mib: int | None
    input_rate_limit_per_sec: int
    image_profile: str | None
    surfaces: list[BenchmarkSurface]
    observation_cases: list[str] | None
    runner_paths: list[ModalColocatedRunnerPath]
    iterations: int


def run_modal_colocated_client_benchmark(
    config: ModalColocatedClientBenchmarkConfig,
    *,
    run_id_factory: Callable[[], str] = new_run_id,
    create_computer: Callable[..., ComputerSandbox] = ComputerSandbox.create,
    exec_once: Callable[..., Any] = modal_sandbox_exec_once,
    exec_in_target: Callable[..., Any] = modal_sandbox_exec_in_place,
    surface_benchmark: Callable[..., dict[str, Any]] = run_sdk_surface_benchmark,
) -> dict[str, Any]:
    if config.runner_cpu is not None and config.runner_cpu <= 0:
        raise ValueError("runner_cpu must be greater than 0")
    if config.runner_memory_mib is not None and config.runner_memory_mib < 128:
        raise ValueError("runner_memory_mib must be at least 128")
    if not config.surfaces:
        raise ValueError("at least one surface is required")
    if not config.runner_paths:
        raise ValueError("at least one runner path is required")
    invalid = [
        surface for surface in config.surfaces if surface not in MODAL_COLOCATED_ALLOWED_SURFACES
    ]
    if invalid:
        raise ValueError(f"unsupported co-located benchmark surface: {', '.join(invalid)}")
    invalid_paths = [
        path for path in config.runner_paths if path not in MODAL_COLOCATED_ALLOWED_RUNNER_PATHS
    ]
    if invalid_paths:
        raise ValueError(f"unsupported co-located runner path: {', '.join(invalid_paths)}")

    run_id = run_id_factory()
    target_run_id = f"{run_id}-target"
    app_tags = {"benchmark": "modal-colocated-client", "benchmark_run_id": run_id}
    tags = {
        "benchmark": "modal-colocated-client",
        "benchmark_run_id": run_id,
        "role": "target",
    }
    started = time.perf_counter()
    computer = create_computer(
        config=config.target_config_factory(target_run_id),
        app_name=config.app_name,
        name=config.name,
        app_tags=app_tags,
        tags=tags,
        wait=True,
    )
    cold_create_to_ready_ms = (time.perf_counter() - started) * 1000
    try:
        target_metadata = {
            **_modal_colocated_environment_metadata(config),
            "modal_colocation_role": "external-caller",
            "modal_cold_create_to_ready_ms": cold_create_to_ready_ms,
            "modal_run_id": target_run_id,
            "modal_ab_run_id": run_id,
            "modal_app_name": config.app_name,
            "modal_sandbox_id": computer.metadata().sandbox_id if computer.metadata() else None,
            "modal_cpu_count": config.modal_cpu,
            "modal_memory_gib": (
                config.modal_memory_mib / 1024
                if config.modal_memory_mib is not None
                else None
            ),
        }
        external_result = surface_benchmark(
            surfaces=config.surfaces,
            client=computer.client,
            mode="http",
            iterations=config.iterations,
            base_url=computer.client.base_url,
            environment_metadata=target_metadata,
            observation_cases=config.observation_cases,
        )
        runner_results = run_modal_colocated_runner_paths(
            config,
            run_id=run_id,
            computer=computer,
            target_sandbox_id=target_metadata["modal_sandbox_id"],
            exec_once=exec_once,
            exec_in_target=exec_in_target,
        )
        primary_runner_path = config.runner_paths[0]
        colocated_result = runner_results[primary_runner_path]
        comparison = modal_colocated_comparison(external_result, colocated_result)
        if len(runner_results) > 1:
            comparison["runner_paths"] = {
                path: modal_colocated_comparison(external_result, result)
                for path, result in runner_results.items()
            }
        return {
            "ok": bool(external_result.get("ok"))
            and all(bool(result.get("ok")) for result in runner_results.values()),
            "benchmark": "modal-colocated-client",
            "generated_at": datetime.now(UTC).isoformat(),
            "iterations": config.iterations,
            "metadata": {
                "modal_region": config.modal_region,
                "caller_region_label": config.caller_region_label,
                "modal_ingress": config.modal_ingress,
                "daemon_http_version": config.daemon_http_version,
                "target_sandbox_id": target_metadata["modal_sandbox_id"],
                "surfaces": config.surfaces,
                "runner_paths": config.runner_paths,
                "primary_runner_path": primary_runner_path,
                "comparison": "external caller versus same-region Modal runner",
            },
            "runs": {
                "external_caller": external_result,
                "modal_colocated_runner": colocated_result,
                "modal_colocated_runner_paths": runner_results,
            },
            "comparison": comparison,
            "failures": [
                *external_result.get("failures", []),
                *[
                    failure
                    for result in runner_results.values()
                    for failure in result.get("failures", [])
                ],
            ],
        }
    finally:
        computer.terminate()
        computer.detach()


def run_modal_colocated_runner_paths(
    config: ModalColocatedClientBenchmarkConfig,
    *,
    run_id: str,
    computer: ComputerSandbox,
    target_sandbox_id: str | None,
    exec_once: Callable[..., Any] = modal_sandbox_exec_once,
    exec_in_target: Callable[..., Any] = modal_sandbox_exec_in_place,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for path in config.runner_paths:
        result = run_modal_colocated_runner_benchmark(
            config,
            run_id=run_id,
            runner_path=path,
            computer=computer,
            target_sandbox_id=target_sandbox_id,
            exec_once=exec_once,
            exec_in_target=exec_in_target,
        )
        results[path] = result
    return results


def run_modal_colocated_runner_benchmark(
    config: ModalColocatedClientBenchmarkConfig,
    *,
    run_id: str,
    runner_path: ModalColocatedRunnerPath,
    computer: ComputerSandbox,
    target_sandbox_id: str | None,
    exec_once: Callable[..., Any] = modal_sandbox_exec_once,
    exec_in_target: Callable[..., Any] = modal_sandbox_exec_in_place,
) -> dict[str, Any]:
    runner_region_label = (
        f"modal-target-loopback:{config.modal_region}"
        if runner_path == "target-loopback"
        else f"modal-runner:{config.modal_region}"
    )
    colocation_role = (
        "modal-target-loopback"
        if runner_path == "target-loopback"
        else "modal-colocated-runner"
    )
    metadata = {
        **_modal_colocated_environment_metadata(config),
        "caller_region_label": runner_region_label,
        "modal_colocation_role": colocation_role,
        "modal_runner_path": runner_path,
        "modal_runner_region": config.modal_region,
        "modal_target_sandbox_id": target_sandbox_id,
    }
    env = build_modal_colocated_runner_env(
        iterations=config.iterations,
        http2=False if runner_path == "target-loopback" else config.daemon_http_version == "2",
        surfaces=config.surfaces,
        observation_cases=config.observation_cases,
        metadata=metadata,
    )
    exec_result = run_modal_daemon_command(
        computer,
        ("python", "-c", modal_colocated_runner_code()),
        path=runner_path,
        app_name=config.app_name,
        runner_name=modal_colocated_runner_name(config.name),
        modal_region=config.modal_region,
        env=env,
        app_tags={"benchmark": "modal-colocated-client", "benchmark_run_id": run_id},
        tags={
            "benchmark": "modal-colocated-client",
            "benchmark_run_id": run_id,
            "role": "runner",
        },
        runner_cpu=config.runner_cpu,
        runner_memory_mib=config.runner_memory_mib,
        exec_timeout_seconds=_runner_exec_timeout_seconds(config),
        exec_once=exec_once,
        exec_in_target=exec_in_target,
    )
    if getattr(exec_result, "returncode", None) not in (0, None):
        return modal_colocated_runner_failure(
            exec_result,
            metadata=metadata,
            surfaces=config.surfaces,
        )
    result = extract_modal_colocated_result(getattr(exec_result, "stdout", ""))
    result_metadata = result.setdefault("metadata", {})
    if isinstance(result_metadata, dict):
        environment = result_metadata.setdefault("environment", {})
        if isinstance(environment, dict):
            environment["modal_runner_sandbox_id"] = getattr(exec_result, "sandbox_id", None)
            environment["modal_runner_region"] = config.modal_region
    return result


def build_modal_colocated_runner_env(
    *,
    iterations: int,
    http2: bool,
    surfaces: list[BenchmarkSurface],
    observation_cases: list[str] | None,
    metadata: dict[str, Any],
) -> dict[str, str]:
    env = {
        "COMPUTER_USE_BENCHMARK_ITERATIONS": str(iterations),
        "COMPUTER_USE_BENCHMARK_HTTP2": str(http2).lower(),
        "COMPUTER_USE_BENCHMARK_SURFACES_JSON": json.dumps(surfaces),
        "COMPUTER_USE_BENCHMARK_OBSERVATION_CASES_JSON": json.dumps(observation_cases),
        "COMPUTER_USE_BENCHMARK_METADATA_JSON": json.dumps(metadata, sort_keys=True),
    }
    return env


def modal_colocated_runner_code() -> str:
    return dedent(f"""\
import json
import os
import time

from modal_computer_use import DaemonClient
from modal_computer_use.benchmarks.surfaces import run_sdk_surface_benchmark


def _runner_preflight(client):
    probes = []
    for name, path in (
        ("healthz", "/healthz"),
        ("version", "/v1/version"),
        ("capabilities", "/v1/capabilities"),
    ):
        started = time.perf_counter()
        try:
            client.get_json(path)
        except Exception as exc:
            probes.append(
                {{
                    "route": name,
                    "ok": False,
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                    "error_type": type(exc).__name__,
                    "status_code": getattr(exc, "status_code", None),
                    "error_code": getattr(exc, "code", None),
                }}
            )
        else:
            probes.append(
                {{
                    "route": name,
                    "ok": True,
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                    "http_version": getattr(client.transport, "last_http_version", None),
                }}
            )
    return {{
        "ok": all(probe.get("ok") for probe in probes),
        "probes": probes,
    }}


base_url = os.environ.get("COMPUTER_USE_BENCHMARK_BASE_URL") or os.environ[
    "COMPUTER_USE_DAEMON_BASE_URL"
]
token = (
    os.environ.get("COMPUTER_USE_BENCHMARK_TOKEN")
    or os.environ.get("COMPUTER_USE_DAEMON_TOKEN")
    or None
)
iterations = int(os.environ["COMPUTER_USE_BENCHMARK_ITERATIONS"])
http2 = os.environ.get("COMPUTER_USE_BENCHMARK_HTTP2") == "true"
surfaces = json.loads(os.environ["COMPUTER_USE_BENCHMARK_SURFACES_JSON"])
observation_cases = json.loads(os.environ["COMPUTER_USE_BENCHMARK_OBSERVATION_CASES_JSON"])
metadata = json.loads(os.environ["COMPUTER_USE_BENCHMARK_METADATA_JSON"])
client = DaemonClient(base_url, token=token, http2=http2)
try:
    runner_preflight = _runner_preflight(client)
    result = run_sdk_surface_benchmark(
        surfaces=surfaces,
        client=client,
        mode="http",
        iterations=iterations,
        base_url=base_url,
        environment_metadata=metadata,
        observation_cases=observation_cases,
    )
finally:
    client.close()
result.setdefault("metadata", {{}})["runner_preflight"] = runner_preflight
print("{MODAL_COLOCATED_RESULT_START}")
print(json.dumps(result, sort_keys=True))
print("{MODAL_COLOCATED_RESULT_END}")
""")


def modal_colocated_runner_name(name: str | None) -> str | None:
    return None if name is None else f"{name}-runner"


def _runner_exec_timeout_seconds(config: ModalColocatedClientBenchmarkConfig) -> int:
    if "daemon-observation-stream" in config.surfaces:
        return max(900, config.iterations * 90)
    return 240


def extract_modal_colocated_result(stdout: str) -> dict[str, Any]:
    _, start, rest = stdout.partition(MODAL_COLOCATED_RESULT_START)
    if not start:
        raise SandboxUnavailableError("co-located runner did not emit benchmark result")
    payload, end, _ = rest.partition(MODAL_COLOCATED_RESULT_END)
    if not end:
        raise SandboxUnavailableError("co-located runner emitted an incomplete benchmark result")
    result = json.loads(payload.strip())
    if not isinstance(result, dict):
        raise SandboxUnavailableError("co-located runner emitted a non-object benchmark result")
    return result


def modal_colocated_runner_failure(
    exec_result: object,
    *,
    metadata: dict[str, Any],
    surfaces: list[BenchmarkSurface],
) -> dict[str, Any]:
    failures = [
        {
            "surface": surface,
            "phase": "modal_colocated_runner",
            "code": "modal_runner_failed",
            "returncode": getattr(exec_result, "returncode", None),
        }
        for surface in surfaces
    ]
    return {
        "ok": False,
        "benchmark": "sdk-surfaces",
        "mode": "http",
        "surfaces": {
            surface: {
                "status": "failed",
                "metadata": {"environment": {k: v for k, v in metadata.items() if v is not None}},
                "failures": [failure],
            }
            for surface, failure in zip(surfaces, failures, strict=True)
        },
        "failures": failures,
    }


def modal_colocated_comparison(
    external_result: dict[str, Any],
    colocated_result: dict[str, Any],
) -> dict[str, Any]:
    surfaces: dict[str, Any] = {}
    transport = _metric_comparison(
        external_result,
        colocated_result,
        surface="daemon-transport-floor",
        metric_name="fastest_floor_p50_ms",
        extractor=_transport_floor_fastest_p50,
    )
    if transport is not None:
        surfaces["daemon-transport-floor"] = transport

    observation = _metric_comparison(
        external_result,
        colocated_result,
        surface="daemon-observation-stream",
        metric_name="causal_action_to_frame_p50_ms",
        extractor=_observation_causal_action_p50,
    )
    if observation is not None:
        surfaces["daemon-observation-stream"] = observation

    paired_observation_cases = _paired_observation_case_comparisons(
        external_result,
        colocated_result,
    )

    result: dict[str, Any] = {"surfaces": surfaces}
    if paired_observation_cases:
        result["paired_observation_cases"] = paired_observation_cases
    if transport is not None:
        result.update(
            {
                "external_fastest_floor_p50_ms": transport["external_p50_ms"],
                "colocated_fastest_floor_p50_ms": transport["colocated_p50_ms"],
                "delta_ms": transport["delta_ms"],
                "ratio_vs_external": transport["ratio_vs_external"],
            }
        )
    diagnosis = modal_colocated_latency_diagnosis(external_result, colocated_result, result)
    if diagnosis is not None:
        result["diagnosis"] = diagnosis
    return result


def modal_colocated_latency_diagnosis(
    external_result: dict[str, Any],
    colocated_result: dict[str, Any],
    comparison: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    comparison = comparison or modal_colocated_comparison(external_result, colocated_result)
    surfaces = _dict_value(comparison.get("surfaces"))
    transport = _dict_value(surfaces.get("daemon-transport-floor"))
    observation = _dict_value(surfaces.get("daemon-observation-stream"))
    framing = _causal_framing_comparison(external_result, colocated_result)
    likely_bound = _likely_latency_bound(transport, observation, framing)
    if likely_bound is None and framing is None:
        return None
    stage_diagnosis = _causal_stage_diagnosis(external_result, colocated_result, observation)
    return {
        "likely_bound": likely_bound or "insufficient_evidence",
        "transport_floor": transport or None,
        "causal_action_observe": observation or None,
        "causal_stage_diagnosis": stage_diagnosis,
        "causal_framing": framing,
        "interpretation": _latency_diagnosis_interpretation(likely_bound),
    }


def _paired_observation_case_comparisons(
    external_result: dict[str, Any],
    colocated_result: dict[str, Any],
) -> dict[str, Any]:
    external_cases = _observation_cases(external_result)
    colocated_cases = _observation_cases(colocated_result)
    case_names = sorted(
        name
        for name in set(external_cases) | set(colocated_cases)
        if _dict_value(external_cases.get(name)).get("paired_comparison")
        or _dict_value(colocated_cases.get(name)).get("paired_comparison")
    )
    return {
        name: {
            "external": _paired_observation_case_summary(_dict_value(external_cases.get(name))),
            "colocated": _paired_observation_case_summary(_dict_value(colocated_cases.get(name))),
        }
        for name in case_names
    }


def _paired_observation_case_summary(case: dict[str, Any]) -> dict[str, Any] | None:
    comparison = _dict_value(case.get("paired_comparison"))
    if not comparison:
        return None
    baseline = _dict_value(case.get("baseline"))
    variant = _dict_value(case.get("variant"))
    summary: dict[str, Any] = {
        "metric": case.get("metric"),
        "delta_direction": case.get("delta_direction"),
        "negative_delta_interpretation": case.get("negative_delta_interpretation"),
        "baseline": _paired_observation_arm_summary(baseline),
        "variant": _paired_observation_arm_summary(variant),
        "paired_comparison": comparison,
    }
    if stability := _dict_value(case.get("sample_stability")):
        summary["sample_stability"] = stability
    return summary


def _paired_observation_arm_summary(arm: dict[str, Any]) -> dict[str, Any]:
    summary = {
        key: arm.get(key)
        for key in (
            "label",
            "frame_encoding",
            "dirty_frame_producer",
            "full_frame_fallback",
            "change_region_radius",
        )
        if key in arm
    }
    if timing := _dict_value(arm.get("summary_ms")):
        summary["summary_ms"] = timing
    if region := _dirty_frame_capture_region_summary(arm):
        summary["dirty_frame_capture_region"] = region
    if stages := _stage_p50_summary(arm):
        summary["stage_p50_ms"] = stages
    return summary


def _metric_comparison(
    external_result: dict[str, Any],
    colocated_result: dict[str, Any],
    *,
    surface: str,
    metric_name: str,
    extractor: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> dict[str, Any] | None:
    external = extractor(external_result)
    colocated = extractor(colocated_result)
    if external is None and colocated is None:
        return None
    external_p50 = None if external is None else external.get("p50_ms")
    colocated_p50 = None if colocated is None else colocated.get("p50_ms")
    delta_ms = (
        None
        if not isinstance(external_p50, int | float) or not isinstance(colocated_p50, int | float)
        else colocated_p50 - external_p50
    )
    return {
        "surface": surface,
        "metric": metric_name,
        "case": (colocated or external or {}).get("case"),
        "external_p50_ms": external_p50,
        "colocated_p50_ms": colocated_p50,
        "delta_ms": delta_ms,
        "ratio_vs_external": (
            None
            if not isinstance(external_p50, int | float)
            or not isinstance(colocated_p50, int | float)
            or external_p50 == 0
            else colocated_p50 / external_p50
        ),
    }


def _transport_floor_fastest_p50(result: dict[str, Any]) -> dict[str, Any] | None:
    surface = _dict_value(_dict_value(result.get("surfaces")).get("daemon-transport-floor"))
    summary = _dict_value(surface.get("transport_floor_summary"))
    fastest = _dict_value(summary.get("fastest_floor_case"))
    value = fastest.get("p50_ms")
    if not isinstance(value, int | float):
        return None
    return {"case": fastest.get("case"), "p50_ms": float(value)}


def _observation_causal_action_p50(result: dict[str, Any]) -> dict[str, Any] | None:
    cases = _dict_value(
        _dict_value(_dict_value(result.get("surfaces")).get("daemon-observation-stream")).get(
            "cases"
        )
    )
    preferred_cases = (
        "observation_action_click_act_and_observe_sdk_default_production",
        "observation_action_click_act_and_observe_auto_signal_production",
        "observation_action_click_act_and_observe_auto_region_production",
        "observation_action_click_observe_change_auto_signal_binary_envelope_production",
        "observation_action_click_observe_change_auto_signal_production",
        "observation_action_click_observe_change_auto_signal",
    )
    for case_name in preferred_cases:
        case = _dict_value(cases.get(case_name))
        value = _summary_p50(case.get("action_to_frame_summary_ms")) or _summary_p50(
            case.get("summary_ms")
        )
        if value is not None:
            return {"case": case_name, "p50_ms": value}
    return None


def _causal_stage_diagnosis(
    external_result: dict[str, Any],
    colocated_result: dict[str, Any],
    observation: dict[str, Any],
) -> dict[str, Any] | None:
    case_name = observation.get("case")
    if not isinstance(case_name, str):
        return None
    external = _case_stage_diagnosis(external_result, case_name)
    colocated = _case_stage_diagnosis(colocated_result, case_name)
    if external is None and colocated is None:
        return None
    return {
        "case": case_name,
        "external": external,
        "colocated": colocated,
    }


def _case_stage_diagnosis(result: dict[str, Any], case_name: str) -> dict[str, Any] | None:
    case = _dict_value(_observation_cases(result).get(case_name))
    latency_diagnosis = _dict_value(case.get("latency_diagnosis"))
    stage_p50 = _stage_p50_summary(case)
    if not latency_diagnosis and not stage_p50:
        return None
    dominant_stage = _dominant_stage(stage_p50)
    diagnosis = {
        "latency_diagnosis": latency_diagnosis or None,
        "stage_p50_ms": stage_p50,
        "dominant_stage": dominant_stage,
    }
    sample_stability = _dict_value(case.get("sample_stability"))
    if sample_stability:
        diagnosis["sample_stability"] = sample_stability
    region_summary = _dirty_frame_capture_region_summary(case)
    if region_summary:
        diagnosis["dirty_frame_capture_region"] = region_summary
    return diagnosis


def _dirty_frame_capture_region_summary(case: dict[str, Any]) -> dict[str, Any] | None:
    summaries = {
        "width_px": _dict_value(case.get("dirty_frame_capture_region_width_summary_px")),
        "height_px": _dict_value(case.get("dirty_frame_capture_region_height_summary_px")),
        "area_px": _dict_value(case.get("dirty_frame_capture_region_area_summary_px")),
    }
    compact = {name: summary for name, summary in summaries.items() if summary}
    sources = case.get("dirty_frame_capture_region_sources")
    if isinstance(sources, list) and sources:
        compact["sources"] = sources
    return compact or None


def _stage_p50_summary(case: dict[str, Any]) -> dict[str, float]:
    stage_summary = _dict_value(case.get("change_stage_timing_summary_ms"))
    return {
        name: p50
        for name in (
            "server_pre_emit_ms",
            "dirty_producer_wait_ms",
            "dirty_region_confirmation_ms",
            "dirty_region_confirmation_capture_ms",
            "dirty_region_confirmation_capture_operation_ms",
            "dirty_region_confirmation_native_ms",
            "frame_poll_ms",
        )
        if (p50 := _summary_p50(_dict_value(stage_summary.get(name)))) is not None
    }


def _dominant_stage(stage_p50: dict[str, float]) -> dict[str, Any] | None:
    candidates = {
        name: value
        for name, value in stage_p50.items()
        if name != "server_pre_emit_ms" and value > 0
    }
    if not candidates:
        return None
    name, value = max(candidates.items(), key=lambda item: item[1])
    return {"name": name, "p50_ms": value}


def _causal_framing_comparison(
    external_result: dict[str, Any],
    colocated_result: dict[str, Any],
) -> dict[str, Any] | None:
    pairs = {
        "auto_signal": (
            "observation_action_click_act_and_observe_auto_signal_production",
            "observation_action_click_act_and_observe_auto_signal_binary_envelope_production",
        ),
        "auto_region": (
            "observation_action_click_act_and_observe_auto_region_production",
            "observation_action_click_act_and_observe_auto_region_binary_envelope_production",
        ),
    }
    rows: dict[str, Any] = {}
    for name, (json_case, envelope_case) in pairs.items():
        external = _framing_row(external_result, json_case=json_case, envelope_case=envelope_case)
        colocated = _framing_row(colocated_result, json_case=json_case, envelope_case=envelope_case)
        if external is not None or colocated is not None:
            rows[name] = {
                "external": external,
                "colocated": colocated,
            }
    if not rows:
        return None
    material_wins = [
        {"case_group": name, "caller_path": role}
        for name, row in rows.items()
        for role in ("external", "colocated")
        if isinstance(row.get(role), dict)
        and bool(row.get(role, {}).get("material_envelope_win"))
    ]
    return {
        "cases": rows,
        "material_envelope_win": bool(material_wins),
        "material_envelope_wins": material_wins,
    }


def _framing_row(
    result: dict[str, Any],
    *,
    json_case: str,
    envelope_case: str,
) -> dict[str, Any] | None:
    cases = _observation_cases(result)
    json_p50 = _summary_p50(_dict_value(cases.get(json_case)).get("action_to_frame_summary_ms"))
    envelope_p50 = _summary_p50(
        _dict_value(cases.get(envelope_case)).get("action_to_frame_summary_ms")
    )
    if json_p50 is None and envelope_p50 is None:
        return None
    delta_ms = (
        None if json_p50 is None or envelope_p50 is None else envelope_p50 - json_p50
    )
    ratio = (
        None
        if json_p50 is None or envelope_p50 is None or json_p50 == 0
        else envelope_p50 / json_p50
    )
    return {
        "json_binary_case": json_case,
        "binary_envelope_case": envelope_case,
        "json_binary_p50_ms": json_p50,
        "binary_envelope_p50_ms": envelope_p50,
        "delta_ms": delta_ms,
        "ratio_vs_json_binary": ratio,
        "material_envelope_win": bool(
            delta_ms is not None and ratio is not None and delta_ms <= -5.0 and ratio <= 0.9
        ),
    }


def _observation_cases(result: dict[str, Any]) -> dict[str, Any]:
    return _dict_value(
        _dict_value(_dict_value(result.get("surfaces")).get("daemon-observation-stream")).get(
            "cases"
        )
    )


def _likely_latency_bound(
    transport: dict[str, Any],
    observation: dict[str, Any],
    framing: dict[str, Any] | None,
) -> str | None:
    if framing is not None and framing.get("material_envelope_win"):
        win_paths = {
            item.get("caller_path")
            for item in framing.get("material_envelope_wins", [])
            if isinstance(item, dict)
        }
        if {"external", "colocated"}.issubset(win_paths):
            return "websocket_message_framing"
        return "partial_websocket_message_framing_evidence"
    transport_ratio = transport.get("ratio_vs_external")
    observation_ratio = observation.get("ratio_vs_external")
    if isinstance(transport_ratio, int | float) and isinstance(observation_ratio, int | float):
        if transport_ratio <= 0.25 and observation_ratio >= 0.5:
            return "daemon_action_capture_or_change_detection"
        if transport_ratio <= 0.75 and observation_ratio <= 0.75:
            return "caller_placement_or_modal_receive_floor"
    if transport or observation or framing:
        return "mixed_or_inconclusive"
    return None


def _latency_diagnosis_interpretation(likely_bound: str | None) -> str:
    if likely_bound == "websocket_message_framing":
        return (
            "binary-envelope materially improves the causal production path; consider a policy PR "
            "only after repeating the live A/B."
        )
    if likely_bound == "partial_websocket_message_framing_evidence":
        return (
            "binary-envelope materially improves at least one caller path, but the selected matrix "
            "did not prove the win across both external and co-located callers."
        )
    if likely_bound == "daemon_action_capture_or_change_detection":
        return (
            "co-location reduced raw transport more than causal action-observe; daemon action, "
            "damage, capture, or diff work remains material."
        )
    if likely_bound == "caller_placement_or_modal_receive_floor":
        return (
            "co-location improves transport and causal action-observe together; caller placement "
            "is the primary lever."
        )
    if likely_bound == "mixed_or_inconclusive":
        return (
            "selected cases do not isolate one dominant lever; inspect per-case attribution and "
            "rerun the focused matrix."
        )
    return "insufficient successful cases to diagnose the latency bound."


def _summary_p50(value: object) -> float | None:
    if not isinstance(value, dict):
        return None
    p50 = value.get("p50")
    return float(p50) if isinstance(p50, int | float) else None


def _modal_colocated_environment_metadata(
    config: ModalColocatedClientBenchmarkConfig,
) -> dict[str, Any]:
    image_identity = f"inline:{config.browser or config.resource_profile or 'standard'}"
    return {
        "caller_region_label": config.caller_region_label,
        "modal_region": config.modal_region,
        "modal_ingress": config.modal_ingress,
        "daemon_http_version": config.daemon_http_version,
        "resource_profile": config.resource_profile,
        "browser": config.browser,
        "gpu": config.gpu,
        "input_rate_limit_per_sec": config.input_rate_limit_per_sec,
        "image_profile": config.image_profile,
        "provenance": benchmark_provenance(
            caller_path="modal-colocated-client",
            modal_region=config.modal_region,
            image_identity=image_identity,
            cpu=config.modal_cpu,
            memory_mib=config.modal_memory_mib,
            gpu=config.gpu,
        ),
    }


def _dict_value(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
