from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..errors import SandboxUnavailableError
from ..sandbox import ComputerSandbox, modal_sandbox_exec_once
from ..state import new_run_id
from .constants import BenchmarkSurface
from .surfaces import run_sdk_surface_benchmark

DEFAULT_MODAL_COLOCATED_SURFACES: tuple[BenchmarkSurface, ...] = ("daemon-transport-floor",)
MODAL_COLOCATED_ALLOWED_SURFACES: tuple[BenchmarkSurface, ...] = (
    "daemon-transport-floor",
    "daemon-observation-stream",
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
    iterations: int


def run_modal_colocated_client_benchmark(
    config: ModalColocatedClientBenchmarkConfig,
    *,
    run_id_factory: Callable[[], str] = new_run_id,
    create_computer: Callable[..., ComputerSandbox] = ComputerSandbox.create,
    exec_once: Callable[..., Any] = modal_sandbox_exec_once,
    surface_benchmark: Callable[..., dict[str, Any]] = run_sdk_surface_benchmark,
) -> dict[str, Any]:
    if config.runner_cpu is not None and config.runner_cpu <= 0:
        raise ValueError("runner_cpu must be greater than 0")
    if config.runner_memory_mib is not None and config.runner_memory_mib < 128:
        raise ValueError("runner_memory_mib must be at least 128")
    if not config.surfaces:
        raise ValueError("at least one surface is required")
    invalid = [
        surface for surface in config.surfaces if surface not in MODAL_COLOCATED_ALLOWED_SURFACES
    ]
    if invalid:
        raise ValueError(f"unsupported co-located benchmark surface: {', '.join(invalid)}")

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
        colocated_result = run_modal_colocated_runner_benchmark(
            config,
            run_id=run_id,
            target_base_url=computer.client.base_url,
            target_token=getattr(computer.client.transport, "token", None),
            target_sandbox_id=target_metadata["modal_sandbox_id"],
            exec_once=exec_once,
        )
        return {
            "ok": bool(external_result.get("ok")) and bool(colocated_result.get("ok")),
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
                "comparison": "external caller versus same-region Modal runner",
            },
            "runs": {
                "external_caller": external_result,
                "modal_colocated_runner": colocated_result,
            },
            "comparison": modal_colocated_comparison(external_result, colocated_result),
            "failures": [
                *external_result.get("failures", []),
                *colocated_result.get("failures", []),
            ],
        }
    finally:
        computer.terminate()
        computer.detach()


def run_modal_colocated_runner_benchmark(
    config: ModalColocatedClientBenchmarkConfig,
    *,
    run_id: str,
    target_base_url: str,
    target_token: str | None,
    target_sandbox_id: str | None,
    exec_once: Callable[..., Any] = modal_sandbox_exec_once,
) -> dict[str, Any]:
    runner_region_label = f"modal-runner:{config.modal_region}"
    metadata = {
        **_modal_colocated_environment_metadata(config),
        "caller_region_label": runner_region_label,
        "modal_colocation_role": "modal-colocated-runner",
        "modal_runner_region": config.modal_region,
        "modal_target_sandbox_id": target_sandbox_id,
    }
    env = build_modal_colocated_runner_env(
        base_url=target_base_url,
        token=target_token,
        iterations=config.iterations,
        http2=config.daemon_http_version == "2",
        surfaces=config.surfaces,
        observation_cases=config.observation_cases,
        metadata=metadata,
    )
    exec_result = exec_once(
        ("python", "-c", modal_colocated_runner_code()),
        app_name=config.app_name,
        name=modal_colocated_runner_name(config.name),
        region=config.modal_region,
        env=env,
        app_tags={"benchmark": "modal-colocated-client", "benchmark_run_id": run_id},
        tags={
            "benchmark": "modal-colocated-client",
            "benchmark_run_id": run_id,
            "role": "runner",
        },
        cpu=config.runner_cpu,
        memory_mib=config.runner_memory_mib,
        exec_timeout_seconds=_runner_exec_timeout_seconds(config),
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
    base_url: str,
    token: str | None,
    iterations: int,
    http2: bool,
    surfaces: list[BenchmarkSurface],
    observation_cases: list[str] | None,
    metadata: dict[str, Any],
) -> dict[str, str]:
    env = {
        "COMPUTER_USE_BENCHMARK_BASE_URL": base_url,
        "COMPUTER_USE_BENCHMARK_ITERATIONS": str(iterations),
        "COMPUTER_USE_BENCHMARK_HTTP2": str(http2).lower(),
        "COMPUTER_USE_BENCHMARK_SURFACES_JSON": json.dumps(surfaces),
        "COMPUTER_USE_BENCHMARK_OBSERVATION_CASES_JSON": json.dumps(observation_cases),
        "COMPUTER_USE_BENCHMARK_METADATA_JSON": json.dumps(metadata, sort_keys=True),
    }
    if token:
        env["COMPUTER_USE_BENCHMARK_TOKEN"] = token
    return env


def modal_colocated_runner_code() -> str:
    return f"""
import json
import os

from modal_computer_use import DaemonClient
from modal_computer_use.benchmarks.surfaces import run_sdk_surface_benchmark

base_url = os.environ["COMPUTER_USE_BENCHMARK_BASE_URL"]
token = os.environ.get("COMPUTER_USE_BENCHMARK_TOKEN") or None
iterations = int(os.environ["COMPUTER_USE_BENCHMARK_ITERATIONS"])
http2 = os.environ.get("COMPUTER_USE_BENCHMARK_HTTP2") == "true"
surfaces = json.loads(os.environ["COMPUTER_USE_BENCHMARK_SURFACES_JSON"])
observation_cases = json.loads(os.environ["COMPUTER_USE_BENCHMARK_OBSERVATION_CASES_JSON"])
metadata = json.loads(os.environ["COMPUTER_USE_BENCHMARK_METADATA_JSON"])
client = DaemonClient(base_url, token=token, http2=http2)
try:
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
print("{MODAL_COLOCATED_RESULT_START}")
print(json.dumps(result, sort_keys=True))
print("{MODAL_COLOCATED_RESULT_END}")
"""


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

    result: dict[str, Any] = {"surfaces": surfaces}
    if transport is not None:
        result.update(
            {
                "external_fastest_floor_p50_ms": transport["external_p50_ms"],
                "colocated_fastest_floor_p50_ms": transport["colocated_p50_ms"],
                "delta_ms": transport["delta_ms"],
                "ratio_vs_external": transport["ratio_vs_external"],
            }
        )
    return result


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
        "observation_action_click_act_and_observe_auto_signal_production",
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


def _summary_p50(value: object) -> float | None:
    if not isinstance(value, dict):
        return None
    p50 = value.get("p50")
    return float(p50) if isinstance(p50, int | float) else None


def _modal_colocated_environment_metadata(
    config: ModalColocatedClientBenchmarkConfig,
) -> dict[str, Any]:
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
    }


def _dict_value(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
