from __future__ import annotations

import math
import os
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..config import (
    ActionConfig,
    BrowserConfig,
    ComputerConfig,
    ImageConfig,
    ResourceConfig,
    RuntimeConfig,
)
from ..latency import validate_first_frame
from ..sandbox import (
    ComputerSandbox,
    cleanup_modal_benchmark_run,
    run_modal_benchmark_function_once,
)
from ..state import new_run_id
from .action_batch import run_action_batch_benchmark

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_CASE_NAMES = ("batch_4_clicks", "separate_4_clicks")
_CASE_FIELDS = (
    "status",
    "iterations",
    "successful_iterations",
    "samples_ms",
    "summary_ms",
    "logical_action_count",
    "sdk_call_count",
    "transport_request_count",
    "batching_semantics",
    "timer_boundary",
    "actions",
    "input_backends",
    "transport_http_versions",
)
_MEASUREMENT_POLICY = {
    "timer_boundary": "complete arm at caller",
    "retries": 0,
    "replacement_samples": 0,
    "fixed_action_order": True,
}
_ACTIONS = [
    {"type": "click", "x": 16, "y": 16, "button": "left"},
    {"type": "click", "x": 128, "y": 16, "button": "left"},
    {"type": "click", "x": 128, "y": 128, "button": "left"},
    {"type": "click", "x": 16, "y": 128, "button": "left"},
]
_CASE_TIMER_BOUNDARY = "before first SDK call through validation of final response"
_BATCH_SEMANTICS = "one ordered action batch, validated before execution, stop on first error"
_SEPARATE_SEMANTICS = "four sequential requests in fixed order, stop on first error"
_UNSAFE_KEYS = {
    "access_key",
    "api_key",
    "authorization",
    "base_url",
    "bearer",
    "artifact_bytes",
    "clipboard_text",
    "credential",
    "credentials",
    "endpoint",
    "headers",
    "password",
    "private_key",
    "resource_id",
    "run_id",
    "sandbox_id",
    "screenshot",
    "screenshot_bytes",
    "secret",
    "secret_key",
    "stderr",
    "stdout",
    "token",
    "typed_text",
}


@dataclass(frozen=True, slots=True)
class ModalActionBatchABConfig:
    region: str
    image_revision: str
    cpu: float = 4.0
    memory_mib: int = 8192
    browser: Literal["chromium"] = "chromium"
    iterations: int = 30
    warmup_iterations: int = 1
    pilot: bool = False
    app_name: str = "modal-computer-use"
    readiness_timeout_seconds: int = 120
    sandbox_timeout_seconds: int = 900

    def __post_init__(self) -> None:
        if not self.region.strip():
            raise ValueError("region must be explicit")
        if _COMMIT_RE.fullmatch(self.image_revision) is None:
            raise ValueError("image_revision must be a full Git commit")
        if self.cpu <= 0 or self.memory_mib < 128:
            raise ValueError("resource configuration is invalid")
        if self.iterations < 1 or self.warmup_iterations < 0:
            raise ValueError("iteration counts are invalid")
        if not self.pilot and (self.iterations != 30 or self.warmup_iterations != 1):
            raise ValueError("nonpublishable counts require pilot=True")


def run_modal_action_batch_ab(
    config: ModalActionBatchABConfig,
    *,
    function_launcher: Any = run_modal_benchmark_function_once,
    cleanup_sweep: Any = cleanup_modal_benchmark_run,
    run_id_factory: Any = new_run_id,
) -> dict[str, Any]:
    run_id = run_id_factory()
    failures: list[dict[str, Any]] = []
    runner_result: dict[str, Any] | None = None
    try:
        launched = function_launcher(
            run_modal_action_batch_ab_in_runner,
            config=config,
            run_tag=run_id,
            app_name=config.app_name,
            region=config.region,
            image_revision=config.image_revision,
            cpu=config.cpu,
            memory_mib=config.memory_mib,
            timeout_seconds=max(900, (config.iterations + config.warmup_iterations) * 30),
            retries=0,
        )
        if isinstance(launched, dict):
            runner_result = launched
        else:
            failures.append(_failure("runner_dispatch", -1, "InvalidRunnerResult"))
    except Exception as exc:
        failures.append(_failure("runner_dispatch", -1, type(exc).__name__))

    try:
        cleanup = cleanup_sweep(app_name=config.app_name, run_id=run_id, include_inventory=True)
    except Exception as exc:
        cleanup = {}
        failures.append(_failure("final_cleanup", -1, type(exc).__name__))
    cleanup_ok = (
        isinstance(cleanup, dict)
        and cleanup.get("cleanup_succeeded") is True
        and cleanup.get("remaining_sandboxes") == 0
    )
    if not cleanup_ok and not any(item["phase"] == "final_cleanup" for item in failures):
        failures.append(_failure("final_cleanup", -1, "CleanupFailed"))
    if runner_result is not None:
        failures.extend(_safe_failures(runner_result.get("failures"), "runner"))
    runner_ok = runner_result is not None and runner_result.get("ok") is True
    publishable = not config.pilot and runner_ok and cleanup_ok and not failures
    result = {
        "schema_version": 1,
        "benchmark": "modal-action-batching-ab",
        "ok": runner_ok and cleanup_ok and not failures,
        "eligibility": "publishable" if publishable else "pilot_ineligible",
        "iterations": config.iterations,
        "warmup_iterations": config.warmup_iterations,
        "replacement_samples": 0,
        "metadata": {
            "caller_topology": "single Modal Function and target with matching observed placement",
            "runner_kind": "modal-function",
            "runner_invocations": 1,
            "modal_region": config.region,
            "modal_ingress": "connect",
            "daemon_http_version": "1.1",
            "image_revision": config.image_revision,
            "target_count": 1,
        },
        "run": runner_result or {},
        "final_cleanup": {
            "cleanup_succeeded": cleanup_ok,
            "remaining_sandboxes": cleanup.get("remaining_sandboxes")
            if isinstance(cleanup, dict)
            else None,
        },
        "failures": failures,
    }
    validate_modal_action_batch_ab_artifact(result, require_publishable=False)
    return result


def run_modal_action_batch_ab_in_runner(
    config: ModalActionBatchABConfig,
    *,
    run_tag: str = "remote-run",
    runner_placement: dict[str, str | None] | None = None,
    create_computer: Any = ComputerSandbox.create,
) -> dict[str, Any]:
    placement = runner_placement or {
        "cloud": os.environ.get("MODAL_CLOUD_PROVIDER"),
        "region": os.environ.get("MODAL_REGION"),
    }
    computer: Any | None = None
    failures: list[dict[str, Any]] = []
    benchmark: dict[str, Any] = {}
    placement_verified = False
    observed_placement: dict[str, Any] = {
        "requested_region": config.region,
        "runner": _safe_observed_placement(placement),
        "target": None,
    }
    cleanup = {"attempted": False, "succeeded": None, "error_type": None}
    try:
        computer_config = _computer_config(config, run_id=f"{run_tag}-target")
        computer = create_computer(
            config=computer_config,
            app_name=config.app_name,
            app_tags={"benchmark": "modal-action-batching-ab"},
            tags={"benchmark": "modal-action-batching-ab", "role": "target"},
            wait=True,
        )
        frame = computer.screenshots.full_bytes(format="png", processing="daemon")
        validate_first_frame(
            frame,
            expected_width=computer_config.desktop.resolution[0],
            expected_height=computer_config.desktop.resolution[1],
            image_format="png",
        )
        target_placement = computer.runtime_placement()
        observed_placement["target"] = _safe_observed_placement(target_placement)
        _require_matching_placement(
            placement,
            target_placement,
            requested_region=config.region,
        )
        placement_verified = True
        raw = run_action_batch_benchmark(
            client=computer.client,
            mode="http",
            iterations=config.iterations,
            warmup_iterations=config.warmup_iterations,
            include_legacy_cases=False,
            include_four_click_cases=True,
        )
        benchmark = _safe_batch_result(raw)
        failures.extend(_safe_failures(raw.get("failures"), "benchmark"))
    except Exception as exc:
        failures.append(_failure("runner", -1, type(exc).__name__))
    finally:
        cleanup = _cleanup_target(computer)
        if cleanup.get("succeeded") is False:
            failures.append(_failure("target_cleanup", -1, str(cleanup["error_type"])))
    return {
        "ok": placement_verified and cleanup.get("succeeded") is True and not failures,
        "placement_verified": placement_verified,
        "placement": observed_placement,
        "benchmark": benchmark,
        "target_cleanup": cleanup,
        "failures": failures,
    }


def validate_modal_action_batch_ab_artifact(
    payload: dict[str, Any], *, require_publishable: bool = True
) -> None:
    _validate_safe_value(payload)
    if payload.get("schema_version") != 1 or payload.get("benchmark") != "modal-action-batching-ab":
        raise ValueError("Modal action batching A/B artifact schema is unsupported")
    if payload.get("replacement_samples") != 0:
        raise ValueError("replacement samples are forbidden")
    if require_publishable and (
        payload.get("ok") is not True
        or payload.get("eligibility") != "publishable"
        or payload.get("iterations") != 30
        or payload.get("warmup_iterations") != 1
    ):
        raise ValueError("artifact is not publishable")
    run = payload.get("run")
    if payload.get("ok") is True:
        if not isinstance(run, dict) or run.get("ok") is not True:
            raise ValueError("successful artifact requires a successful runner")
        if run.get("placement_verified") is not True:
            raise ValueError("successful artifact requires observed placement verification")
        metadata = payload.get("metadata")
        requested_region = metadata.get("modal_region") if isinstance(metadata, dict) else None
        _validate_observed_placement(run.get("placement"), requested_region)
        if run.get("target_cleanup") != {
            "attempted": True,
            "succeeded": True,
            "error_type": None,
        }:
            raise ValueError("successful artifact requires target cleanup")
        if run.get("failures") != [] or payload.get("failures") != []:
            raise ValueError("successful artifact cannot contain failures")
        benchmark = run.get("benchmark")
        _validate_safe_batch_result(benchmark, int(payload["iterations"]))
    cleanup = payload.get("final_cleanup")
    if require_publishable and cleanup != {
        "cleanup_succeeded": True,
        "remaining_sandboxes": 0,
    }:
        raise ValueError("publishable artifact requires terminal cleanup")


def validate_modal_action_batch_output_path(path: Path) -> None:
    if path.is_absolute() or len(path.parts) < 2 or path.parts[0] != "benchmark-results":
        raise ValueError("output must be repository-relative under benchmark-results")
    if ".." in path.parts:
        raise ValueError("output must not traverse directories")
    benchmark_root = Path("benchmark-results")
    if benchmark_root.is_symlink():
        raise ValueError("output root cannot be a symlink")
    current = Path(path.parts[0])
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("output cannot traverse a symlink")
    resolved_root = benchmark_root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
        raise ValueError("output escapes benchmark-results")


def _safe_batch_result(payload: dict[str, Any]) -> dict[str, Any]:
    cases = payload.get("cases")
    if not isinstance(cases, dict):
        raise ValueError("action batching benchmark omitted cases")
    safe_cases: dict[str, Any] = {}
    for name in _CASE_NAMES:
        case = cases.get(name)
        if not isinstance(case, dict):
            raise ValueError(f"action batching benchmark omitted {name}")
        safe_cases[name] = {field: case[field] for field in _CASE_FIELDS if field in case}
        safe_cases[name]["failures"] = _safe_failures(case.get("failures"), name)
    return {
        "status": "ok" if payload.get("ok") is True else "failed",
        "measurement_policy": payload.get("measurement_policy"),
        "cases": safe_cases,
        "comparison": payload.get("four_click_comparison"),
        "failures": _safe_failures(payload.get("failures"), "benchmark"),
    }


def _validate_safe_batch_result(value: Any, iterations: int) -> None:
    if not isinstance(value, dict) or value.get("status") != "ok":
        raise ValueError("successful artifact requires successful batching measurements")
    if value.get("measurement_policy") != _MEASUREMENT_POLICY:
        raise ValueError("batching measurement policy is not exact")
    if value.get("failures") != []:
        raise ValueError("successful batching measurements cannot contain failures")
    cases = value.get("cases")
    if not isinstance(cases, dict) or set(cases) != set(_CASE_NAMES):
        raise ValueError("batching cases are not exact")
    for name, expected_calls, expected_semantics in (
        ("batch_4_clicks", 1, _BATCH_SEMANTICS),
        ("separate_4_clicks", 4, _SEPARATE_SEMANTICS),
    ):
        case = cases[name]
        if (
            not isinstance(case, dict)
            or case.get("status") != "ok"
            or case.get("iterations") != iterations
            or case.get("successful_iterations") != iterations
            or case.get("logical_action_count") != 4
            or case.get("sdk_call_count") != expected_calls
            or case.get("transport_request_count") != expected_calls
            or case.get("batching_semantics") != expected_semantics
            or case.get("timer_boundary") != _CASE_TIMER_BOUNDARY
            or case.get("actions") != _ACTIONS
            or case.get("input_backends") != ["xtest"]
            or case.get("transport_http_versions") != ["HTTP/1.1"]
            or len(case.get("samples_ms", [])) != iterations
            or case.get("failures") != []
        ):
            raise ValueError(f"{name} success contract is invalid")
        summary = case.get("summary_ms")
        if not isinstance(summary, dict):
            raise ValueError(f"{name} summary is missing")
        samples = case["samples_ms"]
        for index, sample in enumerate(samples):
            _finite_nonnegative(sample, f"{name} sample {index}")
        for metric in ("p50", "p95"):
            _finite_nonnegative(summary.get(metric), f"{name} {metric}")
        expected_p50 = float(statistics.median(samples))
        expected_p95 = _percentile(samples, 95)
        if not math.isclose(float(summary["p50"]), expected_p50, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError(f"{name} p50 does not match samples")
        if not math.isclose(float(summary["p95"]), expected_p95, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError(f"{name} p95 does not match samples")
    comparison = value.get("comparison")
    if not isinstance(comparison, dict) or comparison != {
        "status": "measured",
        "metric": "p50",
        "batch_p50_ms": comparison.get("batch_p50_ms"),
        "separate_p50_ms": comparison.get("separate_p50_ms"),
        "speedup": comparison.get("speedup"),
        "delta_ms": comparison.get("delta_ms"),
        "batch_faster": comparison.get("batch_faster"),
    }:
        raise ValueError("four-click comparison is missing")
    for metric in ("batch_p50_ms", "separate_p50_ms", "speedup", "delta_ms"):
        _finite_number(comparison.get(metric), f"comparison {metric}")
    batch_p50 = float(cases["batch_4_clicks"]["summary_ms"]["p50"])
    separate_p50 = float(cases["separate_4_clicks"]["summary_ms"]["p50"])
    expected = {
        "batch_p50_ms": batch_p50,
        "separate_p50_ms": separate_p50,
        "speedup": separate_p50 / batch_p50,
        "delta_ms": separate_p50 - batch_p50,
        "batch_faster": batch_p50 < separate_p50,
    }
    for metric in ("batch_p50_ms", "separate_p50_ms", "speedup", "delta_ms"):
        if not math.isclose(
            float(comparison[metric]),
            float(expected[metric]),
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValueError(f"comparison {metric} does not match case p50 values")
    if comparison.get("batch_faster") is not expected["batch_faster"]:
        raise ValueError("comparison batch_faster does not match case p50 values")


def _computer_config(config: ModalActionBatchABConfig, *, run_id: str) -> ComputerConfig:
    return ComputerConfig(
        runtime=RuntimeConfig(
            modal_region=config.region,
            timeout_seconds=config.sandbox_timeout_seconds,
            readiness_timeout_seconds=config.readiness_timeout_seconds,
        ),
        resources=ResourceConfig(profile="browser", cpu=config.cpu, memory_mib=config.memory_mib),
        image=ImageConfig(source="named", revision=config.image_revision),
        browser=BrowserConfig(kind=config.browser, prewarm=False),
        actions=ActionConfig(input_rate_limit_per_sec=0),
        run_id=run_id,
        ingress="connect",
    )


def _require_matching_placement(
    runner: dict[str, str | None],
    target: dict[str, str | None],
    *,
    requested_region: str,
) -> None:
    if (
        not runner.get("cloud")
        or runner.get("region") != requested_region
        or target != runner
    ):
        raise RuntimeError("runner and target placement differ")


def _safe_observed_placement(value: dict[str, str | None]) -> dict[str, str | None]:
    return {"cloud": value.get("cloud"), "region": value.get("region")}


def _validate_observed_placement(value: Any, requested_region: Any) -> None:
    if not isinstance(requested_region, str) or not requested_region:
        raise ValueError("requested Modal region is missing")
    if not isinstance(value, dict) or set(value) != {"requested_region", "runner", "target"}:
        raise ValueError("observed placement is missing")
    if value.get("requested_region") != requested_region:
        raise ValueError("observed placement requested region differs from metadata")
    runner = value.get("runner")
    target = value.get("target")
    for label, placement in (("runner", runner), ("target", target)):
        if not isinstance(placement, dict) or set(placement) != {"cloud", "region"}:
            raise ValueError(f"observed {label} placement is invalid")
        if not isinstance(placement.get("cloud"), str) or not placement["cloud"]:
            raise ValueError(f"observed {label} cloud is missing")
        if placement.get("region") != requested_region:
            raise ValueError(f"observed {label} region differs from requested region")
    if runner != target:
        raise ValueError("observed runner and target placement differ")


def _percentile(samples: list[float], percentile: int) -> float:
    ordered = sorted(samples)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (percentile / 100) * (len(ordered) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return float(ordered[lower])
    weight = rank - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def _cleanup_target(computer: Any | None) -> dict[str, Any]:
    if computer is None:
        return {"attempted": False, "succeeded": None, "error_type": None}
    error_type: str | None = None
    try:
        computer.terminate(wait=True)
    except Exception as exc:
        error_type = type(exc).__name__
    try:
        computer.detach()
    except Exception as exc:
        error_type = error_type or type(exc).__name__
    return {"attempted": True, "succeeded": error_type is None, "error_type": error_type}


def _failure(phase: str, iteration: int, exception_type: str) -> dict[str, Any]:
    return {"phase": phase, "iteration": iteration, "exception_type": exception_type}


def _safe_failures(value: Any, default_phase: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    failures: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            failures.append(_failure(default_phase, index, "BenchmarkFailure"))
            continue
        phase = item.get("phase")
        iteration = item.get("iteration")
        exception_type = item.get("exception_type") or item.get("type")
        failures.append(
            _failure(
                phase if isinstance(phase, str) else default_phase,
                (
                    iteration
                    if isinstance(iteration, int) and not isinstance(iteration, bool)
                    else index
                ),
                exception_type if isinstance(exception_type, str) else "BenchmarkFailure",
            )
        )
    return failures


def _validate_safe_value(value: Any, *, key: str | None = None) -> None:
    if key is not None:
        normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower().replace("-", "_")
        if normalized in _UNSAFE_KEYS or normalized.endswith(
            ("_token", "_secret", "_password", "_resource_id", "_run_id")
        ):
            raise ValueError(f"artifact contains forbidden field: {key}")
    if isinstance(value, dict):
        for child_key, child in value.items():
            _validate_safe_value(child, key=str(child_key))
    elif isinstance(value, list):
        for child in value:
            _validate_safe_value(child)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("artifact contains a non-finite number")
    elif isinstance(value, str) and re.search(r"https?://", value, flags=re.IGNORECASE):
        raise ValueError("artifact contains an unsafe URL")


def _finite_nonnegative(value: Any, label: str) -> None:
    _finite_number(value, label)
    if value < 0:
        raise ValueError(f"{label} must be nonnegative")


def _finite_number(value: Any, label: str) -> None:
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
