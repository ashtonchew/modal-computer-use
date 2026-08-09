"""A credential-gated, same-runtime input-capacity promotion gate.

The gate is deliberately smaller than the product rate limiter.  It exercises
the public action-batch seam through one borrowed computer and records only
sanitized observations.  A live Modal runner is responsible for supplying the
minimum resource configuration and the native XTest backend; this module does
not create a Sandbox or make network calls by itself.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from ..daemon.input_rate_limit import INPUT_RATE_LIMIT_POLICY, batch_input_token_cost
from ..models import parse_action

CAPACITY_BENCHMARK = "normalized-input-capacity-v1"
MINIMUM_TARGET_TOKENS_PER_SEC = 1_000.0
MINIMUM_CPU = 1.0
MINIMUM_MEMORY_MIB = 2_048
DEFAULT_RATE_LIMIT_PER_SEC = 2_000
DEFAULT_RATE_LIMIT_BURST = 4_000
DEFAULT_BATCHES = 80
DEFAULT_WARMUP_BATCHES = 4
DEFAULT_CYCLES_PER_BATCH = 6
DEFAULT_MAX_TAIL_REGRESSION = 1.5
DEFAULT_MAX_CPU_UTILIZATION_PERCENT = 95.0
DEFAULT_MAX_RSS_GROWTH_BYTES = 64 * 1024 * 1024
_EXACT_REGION = re.compile(r"^[a-z][a-z0-9]*-[a-z][a-z0-9]*-[0-9][a-z0-9]*$")
_RESOURCE_SAMPLE_SCRIPT = """
import glob, json, os
ticks = os.sysconf("SC_CLK_TCK")
page = os.sysconf("SC_PAGE_SIZE")
cpu_ticks = 0
rss_pages = 0
for path in glob.glob("/proc/[0-9]*/stat"):
    try:
        fields = open(path, encoding="utf-8").read().split()
        memory = open(path.removesuffix("stat") + "statm", encoding="utf-8").read().split()
        cpu_ticks += int(fields[13]) + int(fields[14])
        rss_pages += int(memory[1])
    except (FileNotFoundError, PermissionError, IndexError, ValueError):
        pass
print(json.dumps({"cpu_seconds": cpu_ticks / ticks, "rss_bytes": rss_pages * page}))
""".strip()

_INPUT_ACTION_TYPES = {
    "move",
    "click",
    "double_click",
    "triple_click",
    "drag",
    "scroll",
    "mouse_down",
    "mouse_up",
    "type",
    "keypress",
    "hotkey",
    "hold_key",
    "release_all",
}
_FORBIDDEN_KEYS = {
    "authorization",
    "base_url",
    "bearer",
    "clipboard",
    "credential",
    "credentials",
    "endpoint",
    "password",
    "secret",
    "token",
    "typed_text",
}


class InputCapacityGateError(RuntimeError):
    """Raised when capacity evidence is not eligible for promotion."""

    def __init__(
        self,
        category: str,
        message: str,
        *,
        evidence: Mapping[str, str | int] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.evidence = dict(evidence or {})


@dataclass(frozen=True, slots=True)
class InputCapacitySettings:
    """Explicit resource, limiter, and measurement choices for one run."""

    requested_cloud: str
    requested_region: str
    source_sha: str
    input_backend: str = "xtest"
    input_rate_limit_per_sec: int = DEFAULT_RATE_LIMIT_PER_SEC
    input_rate_limit_burst: int = DEFAULT_RATE_LIMIT_BURST
    target_tokens_per_sec: float = MINIMUM_TARGET_TOKENS_PER_SEC
    cpu: float = MINIMUM_CPU
    memory_mib: int = MINIMUM_MEMORY_MIB
    batches: int = DEFAULT_BATCHES
    warmup_batches: int = DEFAULT_WARMUP_BATCHES
    cycles_per_batch: int = DEFAULT_CYCLES_PER_BATCH
    max_tail_regression: float = DEFAULT_MAX_TAIL_REGRESSION
    max_cpu_utilization_percent: float = DEFAULT_MAX_CPU_UTILIZATION_PERCENT
    max_rss_growth_bytes: int = DEFAULT_MAX_RSS_GROWTH_BYTES

    def validate(self, *, batch_cost: int | None = None) -> None:
        if self.requested_cloud not in {"aws", "azure", "gcp", "oci"}:
            raise ValueError("requested cloud must be explicit and supported")
        if _EXACT_REGION.fullmatch(self.requested_region) is None:
            raise ValueError("requested region must be one exact provider region")
        if len(self.source_sha) != 40 or any(
            char not in "0123456789abcdef" for char in self.source_sha
        ):
            raise ValueError("source_sha must be a full lowercase Git SHA")
        if self.input_backend != "xtest":
            raise ValueError("capacity gate requires the native XTest input backend")
        if self.input_rate_limit_per_sec < MINIMUM_TARGET_TOKENS_PER_SEC:
            raise ValueError(
                "input_rate_limit_per_sec must be at least the 1000-token promotion target"
            )
        if self.input_rate_limit_burst < 1:
            raise ValueError("input_rate_limit_burst must be positive")
        if isinstance(self.target_tokens_per_sec, bool) or not math.isfinite(
            self.target_tokens_per_sec
        ) or self.target_tokens_per_sec < MINIMUM_TARGET_TOKENS_PER_SEC:
            raise ValueError("target_tokens_per_sec must be at least 1000")
        if self.cpu != MINIMUM_CPU or self.memory_mib != MINIMUM_MEMORY_MIB:
            raise ValueError("capacity gate must run on the minimum 1 CPU/2048 MiB configuration")
        if self.batches < 2 or self.warmup_batches < 0 or self.cycles_per_batch < 1:
            raise ValueError("batch and warmup counts are invalid")
        if self.max_tail_regression < 1 or not math.isfinite(self.max_tail_regression):
            raise ValueError("max_tail_regression must be finite and at least one")
        if not 0 < self.max_cpu_utilization_percent <= 100:
            raise ValueError("max_cpu_utilization_percent must be in (0, 100]")
        if self.max_rss_growth_bytes < 0:
            raise ValueError("max_rss_growth_bytes must be non-negative")
        if batch_cost is not None:
            if batch_cost <= 0:
                raise ValueError("workload must contain input work")
            if batch_cost > self.input_rate_limit_burst:
                raise ValueError("one workload batch exceeds the configured burst")


class InputCapacityComputer(Protocol):
    """The small public seam used by the gate and its offline fakes."""

    actions: Any
    lifecycle: Any
    mouse: Any
    commands: Any


@dataclass(frozen=True, slots=True)
class _BatchExpectation:
    actions: tuple[dict[str, Any], ...]
    weighted_tokens: int
    final_point: tuple[int, int]


def build_mixed_input_workload(
    *, batches: int = DEFAULT_BATCHES, cycles_per_batch: int = DEFAULT_CYCLES_PER_BATCH
) -> list[_BatchExpectation]:
    """Build bounded, ordered mixed input without retaining typed text in artifacts.

    Every cycle ends with a unique pointer sentinel.  The response's ordered
    result list and final cursor therefore expose both dropped and reordered
    batches without relying on daemon internals.
    """

    if batches < 1 or cycles_per_batch < 1:
        raise ValueError("batches and cycles_per_batch must be positive")
    output: list[_BatchExpectation] = []
    for batch_index in range(batches):
        actions: list[dict[str, Any]] = []
        for cycle in range(cycles_per_batch):
            sequence = batch_index * cycles_per_batch + cycle
            x = 32 + ((sequence * 37) % 900)
            y = 32 + ((sequence * 53) % 650)
            sentinel_x = 48 + ((sequence * 61) % 880)
            sentinel_y = 48 + ((sequence * 47) % 620)
            actions.extend(
                (
                    {"type": "move", "x": x, "y": y},
                    {"type": "click", "x": x, "y": y, "button": "left"},
                    {"type": "type", "text": "x", "method": "keystrokes", "delay_ms": 0},
                    {"type": "scroll", "direction": "down", "amount": 1},
                    {
                        "type": "drag",
                        "start_x": x,
                        "start_y": y,
                        "end_x": sentinel_x,
                        "end_y": sentinel_y,
                        "duration_ms": 0,
                    },
                    {"type": "hotkey", "keys": ["CTRL", "A"], "duration_ms": 0},
                    {"type": "keypress", "key": "ESC"},
                    {"type": "move", "x": sentinel_x, "y": sentinel_y},
                )
            )
        parsed = [parse_action(action) for action in actions]
        output.append(
            _BatchExpectation(
                actions=tuple(actions),
                weighted_tokens=batch_input_token_cost(parsed),
                final_point=(
                    int(actions[-1]["x"]),
                    int(actions[-1]["y"]),
                ),
            )
        )
    return output


async def run_input_capacity_measurement(
    computer: InputCapacityComputer,
    *,
    settings: InputCapacitySettings,
    configuration: Mapping[str, Any],
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """Run the same-client workload and return a promotion-ready artifact."""

    workload = build_mixed_input_workload(
        batches=settings.batches,
        cycles_per_batch=settings.cycles_per_batch,
    )
    settings.validate(batch_cost=workload[0].weighted_tokens)
    safe_configuration = _safe_value(dict(configuration))
    _assert_mapping(safe_configuration, "configuration")
    _validate_configuration(settings, safe_configuration)
    action_digest = hashlib.sha256(
        _canonical_actions(workload[0].actions).encode("utf-8")
    ).hexdigest()
    observations: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    total_tokens = 0
    measured_elapsed_ms = 0.0
    expected_final = workload[-1].final_point
    try:
        await _require_healthy(computer, stage="initial")
        for warmup_index in range(settings.warmup_batches):
            await _run_batch(computer, workload[warmup_index % len(workload)], clock=clock)
        resources_before = await _resource_sample(computer)
        for index, expected in enumerate(workload):
            started = clock()
            result = await _run_batch(computer, expected, clock=clock)
            elapsed_ms = max(0.001, (clock() - started) * 1000.0)
            _validate_batch_result(result, expected, index=index)
            await _require_healthy(computer, stage="post_batch", batch_index=index)
            throughput = expected.weighted_tokens / (elapsed_ms / 1000.0)
            observations.append(
                {
                    "batch_index": index,
                    "action_count": len(expected.actions),
                    "weighted_tokens": expected.weighted_tokens,
                    "elapsed_ms": round(elapsed_ms, 3),
                    "weighted_tokens_per_sec": round(throughput, 3),
                    "result_count": len(_result_items(result)),
                    "input_backends": _result_backends(result),
                    "final_point": {
                        "x": expected.final_point[0],
                        "y": expected.final_point[1],
                    },
                    "health": {"ready": True},
                }
            )
            total_tokens += expected.weighted_tokens
            measured_elapsed_ms += elapsed_ms
        try:
            observed_point = _point_tuple(await computer.mouse.position())
        except InputCapacityGateError:
            raise
        except Exception as exc:
            raise InputCapacityGateError(
                "cursor_state", "final cursor position was unavailable"
            ) from exc
        if observed_point != expected_final:
            raise InputCapacityGateError("lost_input", "final cursor sentinel was not observed")
        resources_after = await _resource_sample(computer)
    except InputCapacityGateError as exc:
        failure: dict[str, Any] = {
            "phase": "measure",
            "category": exc.category,
            "status": "failed",
        }
        if exc.evidence:
            failure["evidence"] = _safe_value(exc.evidence)
        failures.append(failure)
    except Exception as exc:
        failures.append(
            {
                "phase": "measure",
                "category": _error_category(exc),
                "status": "failed",
            }
        )

    total_throughput = (
        total_tokens / (measured_elapsed_ms / 1000.0) if measured_elapsed_ms > 0 else 0.0
    )
    resource_summary = _resource_summary(
        before=locals().get("resources_before"),
        after=locals().get("resources_after"),
        elapsed_ms=measured_elapsed_ms,
        cpu=settings.cpu,
    )
    status = "complete"
    if failures:
        status = "failed"
    elif total_throughput < settings.target_tokens_per_sec:
        status = "failed"
        failures.append(
            {
                "phase": "decision",
                "category": "throughput",
                "status": "failed",
            }
        )
    else:
        tail_failure = _tail_regression_failure(
            observations,
            max_ratio=settings.max_tail_regression,
        )
        if tail_failure is not None:
            status = "failed"
            failures.append(tail_failure)
        resource_failure = _resource_failure(resource_summary, settings=settings)
        if resource_failure is not None:
            status = "failed"
            failures.append(resource_failure)
    artifact = {
        "schema_version": 1,
        "benchmark": CAPACITY_BENCHMARK,
        "status": status,
        "configuration": {
            **safe_configuration,
            "workload": {
                "action_batch_count": settings.batches,
                "cycles_per_batch": settings.cycles_per_batch,
                "action_batch_size": len(workload[0].actions),
                "weighted_tokens_per_batch": workload[0].weighted_tokens,
                "action_payload_sha256": action_digest,
            },
            "connection_reuse": "one-pooled-async-client",
            "input_backend": "xtest",
        },
        "target": {
            "minimum_weighted_tokens_per_sec": settings.target_tokens_per_sec,
            "max_tail_regression": settings.max_tail_regression,
            "max_cpu_utilization_percent": settings.max_cpu_utilization_percent,
            "max_rss_growth_bytes": settings.max_rss_growth_bytes,
        },
        "observations": observations,
        "summary": {
            "weighted_tokens": total_tokens,
            "elapsed_ms": round(measured_elapsed_ms, 3),
            "weighted_tokens_per_sec": round(total_throughput, 3),
            "p50_batch_ms": round(_percentile(observations, 0.50), 3),
            "p95_batch_ms": round(_percentile(observations, 0.95), 3),
            "final_point": {"x": expected_final[0], "y": expected_final[1]},
            "resources": resource_summary,
        },
        "failures": failures,
        "retries": 0,
        "cleanup": {"succeeded": True, "survivors": 0},
    }
    validate_input_capacity_artifact(artifact)
    return artifact


async def execute_input_capacity_gate(
    borrow: Callable[[], AbstractAsyncContextManager[InputCapacityComputer]],
    *,
    settings: InputCapacitySettings,
    configuration: Mapping[str, Any],
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """Measure one borrow and preserve cleanup failures in the artifact."""

    try:
        async with borrow() as computer:
            artifact = await run_input_capacity_measurement(
                computer,
                settings=settings,
                configuration=configuration,
                clock=clock,
            )
    except Exception as exc:
        artifact = _failed_artifact(
            settings=settings,
            configuration=configuration,
            category="cleanup" if "cleanup" in str(exc).lower() else _error_category(exc),
        )
        artifact["cleanup"] = {"succeeded": False, "survivors": "unknown"}
        validate_input_capacity_artifact(artifact)
    return artifact


async def _run_batch(
    computer: InputCapacityComputer,
    expected: _BatchExpectation,
    *,
    clock: Callable[[], float],
) -> Any:
    del clock
    try:
        return await computer.actions.run(
            list(expected.actions),
            continue_on_error=False,
            screenshot_after=False,
            source="input-capacity-gate",
        )
    except Exception as exc:
        raise InputCapacityGateError(_error_category(exc), "input batch request failed") from exc


async def _require_healthy(
    computer: InputCapacityComputer,
    *,
    stage: str,
    batch_index: int | None = None,
) -> None:
    evidence: dict[str, str | int] = {"stage": stage}
    if batch_index is not None:
        evidence["batch_index"] = batch_index
    try:
        status = await computer.lifecycle.status()
    except Exception as exc:
        evidence["outcome"] = "request_failed"
        raise InputCapacityGateError(
            "unhealthy_daemon",
            "daemon health check failed",
            evidence=evidence,
        ) from exc
    ready = status.get("ready") if isinstance(status, Mapping) else getattr(status, "ready", None)
    if ready is not True:
        raw_status = (
            status.get("status")
            if isinstance(status, Mapping)
            else getattr(status, "status", None)
        )
        evidence.update(
            {
                "outcome": "not_ready",
                "status": raw_status
                if raw_status in {"starting", "running", "stopped", "degraded", "failed"}
                else "unknown",
            }
        )
        raise InputCapacityGateError(
            "unhealthy_daemon",
            "daemon is not ready",
            evidence=evidence,
        )


async def _resource_sample(computer: InputCapacityComputer) -> dict[str, float | int]:
    try:
        result = await computer.commands.run("python", "-c", _RESOURCE_SAMPLE_SCRIPT, timeout=10)
        payload = _as_mapping(result)
        output = payload.get("output")
        stdout = output.get("stdout") if isinstance(output, Mapping) else None
        decoded = json.loads(stdout) if isinstance(stdout, str) else None
    except Exception as exc:
        raise InputCapacityGateError(
            "resource_observation",
            "Sandbox resource usage was unavailable",
        ) from exc
    if not isinstance(decoded, Mapping):
        raise InputCapacityGateError(
            "resource_observation",
            "Sandbox resource usage was malformed",
        )
    cpu_seconds = decoded.get("cpu_seconds")
    rss_bytes = decoded.get("rss_bytes")
    if (
        isinstance(cpu_seconds, bool)
        or not isinstance(cpu_seconds, int | float)
        or not math.isfinite(float(cpu_seconds))
        or cpu_seconds < 0
        or isinstance(rss_bytes, bool)
        or not isinstance(rss_bytes, int)
        or rss_bytes < 0
    ):
        raise InputCapacityGateError(
            "resource_observation",
            "Sandbox resource usage was malformed",
        )
    return {"cpu_seconds": float(cpu_seconds), "rss_bytes": rss_bytes}


def _resource_summary(
    *,
    before: Any,
    after: Any,
    elapsed_ms: float,
    cpu: float,
) -> dict[str, float | int] | None:
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return None
    cpu_delta = max(0.0, float(after["cpu_seconds"]) - float(before["cpu_seconds"]))
    rss_growth = max(0, int(after["rss_bytes"]) - int(before["rss_bytes"]))
    utilization = (
        (cpu_delta / (elapsed_ms / 1_000.0) / cpu) * 100.0 if elapsed_ms > 0 else 0.0
    )
    return {
        "cpu_seconds_delta": round(cpu_delta, 6),
        "cpu_utilization_percent": round(utilization, 3),
        "rss_bytes_before": int(before["rss_bytes"]),
        "rss_bytes_after": int(after["rss_bytes"]),
        "rss_growth_bytes": rss_growth,
    }


def _resource_failure(
    summary: dict[str, float | int] | None,
    *,
    settings: InputCapacitySettings,
) -> dict[str, Any] | None:
    if summary is None:
        return {"phase": "decision", "category": "resource_observation", "status": "failed"}
    if (
        float(summary["cpu_utilization_percent"]) > settings.max_cpu_utilization_percent
        or int(summary["rss_growth_bytes"]) > settings.max_rss_growth_bytes
    ):
        return {"phase": "decision", "category": "resource_saturation", "status": "failed"}
    return None


def _validate_batch_result(result: Any, expected: _BatchExpectation, *, index: int) -> None:
    items = _result_items(result)
    if len(items) != len(expected.actions):
        raise InputCapacityGateError(
            "lost_input", f"batch {index} returned an unexpected result count"
        )
    for position, (item, action) in enumerate(zip(items, expected.actions, strict=True)):
        payload = _as_mapping(item)
        if payload.get("index") != position or payload.get("type") != action["type"]:
            raise InputCapacityGateError("misordered_input", f"batch {index} result order changed")
        if payload.get("ok") is not True:
            raise InputCapacityGateError("input_error", f"batch {index} reported an input failure")
        if action["type"] in _INPUT_ACTION_TYPES:
            output = payload.get("output")
            if not isinstance(output, Mapping) or output.get("input_backend") != "xtest":
                raise InputCapacityGateError("x11_backend", "batch did not use native XTest")
            if any("x11" in str(value).lower() for value in output.values()):
                raise InputCapacityGateError("x11_error", "daemon reported an X11 error")
        if action["type"] == "move":
            output = payload.get("output")
            if not isinstance(output, Mapping) or (
                output.get("x") != action.get("x") or output.get("y") != action.get("y")
            ):
                raise InputCapacityGateError(
                    "misordered_input", "pointer sentinel was not observed"
                )


def _result_items(result: Any) -> list[Any]:
    if isinstance(result, Mapping):
        items = result.get("results")
    else:
        items = getattr(result, "results", None)
    return items if isinstance(items, list) else []


def _result_backends(result: Any) -> list[str]:
    backends: list[str] = []
    for item in _result_items(result):
        payload = _as_mapping(item)
        output = payload.get("output")
        backend = output.get("input_backend") if isinstance(output, Mapping) else None
        if isinstance(backend, str) and backend not in backends:
            backends.append(backend)
    return backends


def _point_tuple(value: Any) -> tuple[int, int]:
    if isinstance(value, Mapping):
        x, y = value.get("x"), value.get("y")
    else:
        x, y = getattr(value, "x", None), getattr(value, "y", None)
    if (
        isinstance(x, bool)
        or not isinstance(x, int)
        or isinstance(y, bool)
        or not isinstance(y, int)
    ):
        raise InputCapacityGateError("cursor_state", "cursor position was malformed")
    return x, y


def _tail_regression_failure(
    observations: Sequence[Mapping[str, Any]], *, max_ratio: float
) -> dict[str, Any] | None:
    if len(observations) < 4:
        return None
    midpoint = len(observations) // 2
    first = [float(item["elapsed_ms"]) for item in observations[:midpoint]]
    second = [float(item["elapsed_ms"]) for item in observations[midpoint:]]
    baseline = _percentile_values(first, 0.95)
    tail = _percentile_values(second, 0.95)
    if baseline > 0 and tail > baseline * max_ratio:
        return {
            "phase": "decision",
            "category": "tail_regression",
            "status": "failed",
        }
    return None


def _percentile(observations: Sequence[Mapping[str, Any]], fraction: float) -> float:
    return _percentile_values([float(item["elapsed_ms"]) for item in observations], fraction)


def _percentile_values(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * fraction
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * (rank - lower))


def _failed_artifact(
    *, settings: InputCapacitySettings, configuration: Mapping[str, Any], category: str
) -> dict[str, Any]:
    safe_configuration = _safe_value(dict(configuration))
    _assert_mapping(safe_configuration, "configuration")
    return {
        "schema_version": 1,
        "benchmark": CAPACITY_BENCHMARK,
        "status": "failed",
        "configuration": safe_configuration,
        "target": {
            "minimum_weighted_tokens_per_sec": settings.target_tokens_per_sec,
            "max_tail_regression": settings.max_tail_regression,
        },
        "observations": [],
        "summary": {
            "weighted_tokens": 0,
            "elapsed_ms": 0.0,
            "weighted_tokens_per_sec": 0.0,
            "p50_batch_ms": 0.0,
            "p95_batch_ms": 0.0,
            "final_point": None,
            "resources": None,
        },
        "failures": [{"phase": "setup", "category": category, "status": "failed"}],
        "retries": 0,
        "cleanup": {"succeeded": True, "survivors": 0},
    }


def validate_input_capacity_artifact(artifact: Mapping[str, Any]) -> None:
    """Validate sanitized evidence without trusting the producing runner."""

    if artifact.get("benchmark") != CAPACITY_BENCHMARK:
        raise ValueError("capacity artifact benchmark is invalid")
    _safe_value(dict(artifact))
    status = artifact.get("status")
    if status not in {"complete", "failed"}:
        raise ValueError("capacity artifact status is invalid")
    if artifact.get("retries") != 0:
        raise ValueError("capacity gate does not permit retries")
    failures = artifact.get("failures")
    if not isinstance(failures, list):
        raise ValueError("capacity artifact failures are invalid")
    cleanup = artifact.get("cleanup")
    if not isinstance(cleanup, Mapping):
        raise ValueError("capacity artifact cleanup is invalid")
    if status == "failed":
        if not failures:
            raise ValueError("failed capacity artifact must include a failure")
        return

    if failures:
        raise ValueError("complete capacity artifact cannot include failures")
    if cleanup != {"succeeded": True, "survivors": 0}:
        raise ValueError("complete capacity artifact cleanup is invalid")
    configuration = artifact.get("configuration")
    target = artifact.get("target")
    observations = artifact.get("observations")
    summary = artifact.get("summary")
    if not all(isinstance(value, Mapping) for value in (configuration, target, summary)):
        raise ValueError("complete capacity artifact structure is invalid")
    if not isinstance(observations, list):
        raise ValueError("complete capacity artifact observations are invalid")
    workload = configuration.get("workload")
    if not isinstance(workload, Mapping):
        raise ValueError("complete capacity artifact workload is invalid")
    expected_count = workload.get("action_batch_count")
    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 2
        or len(observations) != expected_count
    ):
        raise ValueError("capacity artifact observation count is invalid")
    target_throughput = _finite_number(
        target.get("minimum_weighted_tokens_per_sec"),
        "minimum throughput",
    )
    observed_throughput = _finite_number(
        summary.get("weighted_tokens_per_sec"),
        "observed throughput",
    )
    if observed_throughput < target_throughput:
        raise ValueError("capacity artifact throughput is below target")
    resources = summary.get("resources")
    if not isinstance(resources, Mapping):
        raise ValueError("capacity artifact resource evidence is missing")
    cpu_utilization = _finite_number(
        resources.get("cpu_utilization_percent"),
        "CPU utilization",
    )
    rss_growth = resources.get("rss_growth_bytes")
    max_cpu = _finite_number(
        target.get("max_cpu_utilization_percent"),
        "maximum CPU utilization",
    )
    max_rss = target.get("max_rss_growth_bytes")
    if (
        isinstance(rss_growth, bool)
        or not isinstance(rss_growth, int)
        or rss_growth < 0
        or isinstance(max_rss, bool)
        or not isinstance(max_rss, int)
        or max_rss < 0
    ):
        raise ValueError("capacity artifact resource evidence is invalid")
    if cpu_utilization > max_cpu or rss_growth > max_rss:
        raise ValueError("capacity artifact exceeds resource limits")
    expected_tokens = workload.get("weighted_tokens_per_batch")
    if isinstance(expected_tokens, bool) or not isinstance(expected_tokens, int):
        raise ValueError("capacity artifact workload tokens are invalid")
    for index, observation in enumerate(observations):
        if not isinstance(observation, Mapping):
            raise ValueError("capacity artifact observation is invalid")
        if observation.get("batch_index") != index:
            raise ValueError("capacity artifact observation order is invalid")
        if observation.get("weighted_tokens") != expected_tokens:
            raise ValueError("capacity artifact observation tokens are invalid")
        if observation.get("input_backends") != ["xtest"]:
            raise ValueError("capacity artifact input attribution is invalid")
        health = observation.get("health")
        if not isinstance(health, Mapping) or health.get("ready") is not True:
            raise ValueError("capacity artifact health evidence is invalid")


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"capacity artifact {name} is invalid")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"capacity artifact {name} is invalid")
    return number


def _validate_configuration(
    settings: InputCapacitySettings,
    configuration: Mapping[str, Any],
) -> None:
    expected_scalars = {
        "caller_topology": "one-application-owned-modal-function",
        "ingress": "attested-tunnel",
        "http_version": "1.1",
        "input_backend": "xtest",
        "input_rate_limit_policy": INPUT_RATE_LIMIT_POLICY,
        "input_rate_limit_per_sec": settings.input_rate_limit_per_sec,
        "input_rate_limit_burst": settings.input_rate_limit_burst,
    }
    for key, expected in expected_scalars.items():
        if configuration.get(key) != expected:
            raise InputCapacityGateError(
                "configuration_mismatch",
                f"capacity configuration does not match {key}",
            )
    expected_placement = {
        "cloud": settings.requested_cloud,
        "region": settings.requested_region,
    }
    if configuration.get("requested_placement") != expected_placement:
        raise InputCapacityGateError(
            "configuration_mismatch",
            "requested placement does not match capacity settings",
        )
    observed = configuration.get("observed_placement")
    if not isinstance(observed, Mapping) or any(
        observed.get(owner) != expected_placement for owner in ("target", "function")
    ):
        raise InputCapacityGateError(
            "configuration_mismatch",
            "observed Function and target placement must match",
        )
    resources = configuration.get("resources")
    expected_resources = {"cpu": settings.cpu, "memory_mib": settings.memory_mib}
    if not isinstance(resources, Mapping) or any(
        resources.get(owner) != expected_resources for owner in ("sandbox", "function")
    ):
        raise InputCapacityGateError(
            "configuration_mismatch",
            "Function and Sandbox resources must match the minimum profile",
        )
    if configuration.get("image_identity") != f"inline-source-{settings.source_sha}":
        raise InputCapacityGateError(
            "configuration_mismatch",
            "image identity does not match the source revision",
        )
    if configuration.get("warm_capacity") != {
        "function_min_containers": 0,
        "sandbox_pool_capacity": 0,
    }:
        raise InputCapacityGateError(
            "configuration_mismatch",
            "warm capacity must remain zero",
        )


def _safe_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and key.lower().replace("-", "_") in _FORBIDDEN_KEYS:
        raise ValueError(f"capacity artifact contains secret-bearing key: {key}")
    if isinstance(value, Mapping):
        return {
            str(item_key): _safe_value(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and (
            "authorization:" in value.lower() or "bearer " in value.lower()
        ):
            raise ValueError("capacity artifact contains a credential")
        return value
    if hasattr(value, "model_dump"):
        return _safe_value(value.model_dump(mode="json"), key=key)
    raise ValueError(f"capacity artifact contains unsupported value: {type(value).__name__}")


def _canonical_actions(actions: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps(actions, sort_keys=True, separators=(",", ":"))


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        if isinstance(dumped, Mapping):
            return dumped
    raise InputCapacityGateError("operation", "daemon returned a malformed action result")


def _assert_mapping(value: Any, name: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")


def _error_category(exc: Exception) -> str:
    name = f"{type(exc).__name__} {exc}".lower()
    if "timeout" in name:
        return "timeout"
    if "x11" in name or "input" in name:
        return "x11_error"
    if "cleanup" in name or "close" in name:
        return "cleanup"
    return "operation"
