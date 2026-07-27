from __future__ import annotations

import math
import os
import re
import statistics
import time
from dataclasses import dataclass
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
from .provenance import benchmark_provenance
from .surfaces import run_sdk_surface_benchmark

PRODUCT_CREATE_CASE = "product_create_to_first_screenshot"
WARM_CASES = (
    "screenshot_full",
    "coordinate_click",
    "coordinate_click_sequence",
    "type_100_chars",
    "type_1000_chars",
    "command_nonlogin_shell_echo",
)
_WARM_CASE_FIELDS = (
    "status",
    "iterations",
    "successful_iterations",
    "samples_ms",
    "summary_ms",
    "benchmark_semantics",
    "input_backends",
    "shell_mode",
    "request",
)
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
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


class PlacementMismatchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModalOptimizedProviderConfig:
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
        if self.cpu <= 0:
            raise ValueError("cpu must be positive")
        if self.memory_mib < 128:
            raise ValueError("memory_mib must be at least 128")
        if self.iterations < 1 or self.warmup_iterations < 0:
            raise ValueError("iteration counts are invalid")
        publishable_counts = self.iterations == 30 and self.warmup_iterations == 1
        if not self.pilot and not publishable_counts:
            raise ValueError("nonpublishable counts require pilot=True")


def run_modal_optimized_provider_benchmark(
    config: ModalOptimizedProviderConfig,
    *,
    function_launcher: Any = run_modal_benchmark_function_once,
    cleanup_sweep: Any = cleanup_modal_benchmark_run,
    clock: Any = time.perf_counter,
    run_id_factory: Any = new_run_id,
) -> dict[str, Any]:
    run_tag = run_id_factory()
    started = clock()
    remote: dict[str, Any] | None = None
    failures: list[dict[str, Any]] = []
    try:
        launched = function_launcher(
            run_modal_optimized_provider_in_runner,
            config=config,
            run_tag=run_tag,
            app_name=config.app_name,
            region=config.region,
            image_revision=config.image_revision,
            cpu=config.cpu,
            memory_mib=config.memory_mib,
            timeout_seconds=max(900, (config.iterations + config.warmup_iterations) * 150),
            retries=0,
        )
        if isinstance(launched, dict):
            remote = launched
        else:
            failures.append(
                {
                    "phase": "runner_dispatch",
                    "iteration": -1,
                    "exception_type": "InvalidRunnerResult",
                }
            )
    except Exception as exc:
        failures.append(_safe_failure("runner_dispatch", -1, exc))
    dispatch_ms = max(0.0, (clock() - started) * 1000.0)
    try:
        cleanup_result = cleanup_sweep(
            app_name=config.app_name,
            run_id=run_tag,
            include_inventory=True,
        )
        final_cleanup = cleanup_result if isinstance(cleanup_result, dict) else {}
    except Exception as exc:
        final_cleanup = {}
        failures.append(_safe_failure("final_cleanup", -1, exc))
    cleanup_ok = (
        final_cleanup.get("cleanup_succeeded") is True
        and final_cleanup.get("remaining_sandboxes") == 0
    )
    remote_ok = remote is not None and remote.get("ok") is True
    if remote is not None:
        failures.extend(_safe_failures(remote.get("failures", []), "runner"))
    if not cleanup_ok and not any(
        failure["phase"] == "final_cleanup" for failure in failures
    ):
        failures.append(
            {"phase": "final_cleanup", "iteration": -1, "exception_type": "CleanupFailed"}
        )
    provenance = benchmark_provenance(
        caller_path="single-modal-function-same-requested-modal-region",
        modal_region=config.region,
        image_identity=f"named:{config.image_revision}",
        cpu=config.cpu,
        memory_mib=config.memory_mib,
        gpu=None,
    )
    metadata = {
        "caller_topology": "single-modal-function-same-requested-modal-region",
        "runner_kind": "modal-function",
        "runner_invocations": 1,
        "runner_startup_in_product_create_boundary": False,
        "modal_region": config.region,
        "modal_ingress": "connect",
        "daemon_http_version": "1.1",
        "browser": config.browser,
        "browser_prewarm": False,
        "image_revision": config.image_revision,
        "runner_cpu": config.cpu,
        "runner_memory_mib": config.memory_mib,
        "target_cpu": config.cpu,
        "target_memory_mib": config.memory_mib,
        "input_rate_limit_per_sec": 0,
        "subprocess_backend": "isolated-asyncio",
        "external_caller_included": False,
        "provenance": provenance,
    }
    result = {
        "schema_version": 1,
        "benchmark": "modal-optimized-provider",
        "ok": remote_ok and cleanup_ok,
        "eligibility": (
            "publishable" if not config.pilot and remote_ok and cleanup_ok else "pilot_ineligible"
        ),
        "iterations": config.iterations,
        "warmup_iterations": config.warmup_iterations,
        "replacement_samples": 0,
        "metadata": metadata,
        "runs": {"modal_optimized_runner": remote or {}},
        "runner_dispatch": {
            "elapsed_ms": dispatch_ms,
            "included_in_product_create_samples": False,
        },
        "final_cleanup": {
            "cleanup_succeeded": cleanup_ok,
            "remaining_sandboxes": final_cleanup.get("remaining_sandboxes"),
        },
        "failures": failures,
    }
    validate_modal_optimized_provider_artifact(result, require_publishable=False)
    return result


def run_modal_optimized_provider_in_runner(
    config: ModalOptimizedProviderConfig,
    *,
    run_tag: str = "remote-run",
    runner_placement: dict[str, str | None] | None = None,
    create_computer: Any = ComputerSandbox.create,
    surface_benchmark: Any = run_sdk_surface_benchmark,
    clock: Any = time.perf_counter,
) -> dict[str, Any]:
    placement = runner_placement or {
        "cloud": os.environ.get("MODAL_CLOUD_PROVIDER"),
        "region": os.environ.get("MODAL_REGION"),
    }
    product_create = _run_product_create_case(
        config,
        run_tag=run_tag,
        runner_placement=placement,
        create_computer=create_computer,
        clock=clock,
    )
    warm_target: Any | None = None
    warm_cleanup = {"attempted": False, "succeeded": None, "error_type": None}
    warm_placement_verified = False
    warm_failures: list[dict[str, Any]] = []
    surfaces: dict[str, Any] = {}
    try:
        warm_config = _computer_config(config, run_id=f"{run_tag}-warm")
        warm_target = create_computer(
            config=warm_config,
            app_name=config.app_name,
            app_tags={"benchmark": "modal-optimized-provider"},
            tags={"benchmark": "modal-optimized-provider", "role": "warm-target"},
            wait=True,
        )
        _validated_raw_frame(warm_target, warm_config)
        _require_matching_placement(placement, warm_target.runtime_placement())
        warm_placement_verified = True
        warm_result = surface_benchmark(
            surfaces=["daemon-http"],
            client=warm_target.client,
            mode="http",
            iterations=config.iterations,
            base_url=warm_target.client.base_url,
            environment_metadata={
                "modal_region": config.region,
                "modal_ingress": "connect",
                "daemon_http_version": "1.1",
                "browser": config.browser,
                "input_rate_limit_per_sec": 0,
                "subprocess_backend": "isolated-asyncio",
                "modal_runner_path": "connect",
            },
            typing_method="keystrokes",
            typing_delay_ms=0,
        )
        surfaces = _safe_warm_surfaces(warm_result)
        warm_failures.extend(_safe_failures(warm_result.get("failures", []), "warm_surface"))
    except Exception as exc:
        warm_failures.append(_safe_failure("warm_setup", -1, exc))
    finally:
        warm_cleanup = _cleanup_target(warm_target)
        if warm_cleanup.get("succeeded") is False:
            warm_failures.append(
                {
                    "phase": "warm_cleanup",
                    "iteration": -1,
                    "exception_type": str(warm_cleanup.get("error_type") or "CleanupFailed"),
                }
            )
    failures = [*product_create["failures"], *warm_failures]
    return {
        "ok": product_create["status"] == "ok" and not failures,
        "failures": failures,
        "product_create": product_create,
        "surfaces": surfaces,
        "warm_target_cleanup": warm_cleanup,
        "warm_target_placement_verified": warm_placement_verified,
        "runner_placement": {
            "cloud": placement.get("cloud"),
            "region": placement.get("region"),
        },
    }


def _run_product_create_case(
    config: ModalOptimizedProviderConfig,
    *,
    run_tag: str,
    runner_placement: dict[str, str | None],
    create_computer: Any,
    clock: Any,
) -> dict[str, Any]:
    samples: list[float] = []
    failures: list[dict[str, Any]] = []
    successful_warmups = 0
    cleanup_attempted = 0
    cleanup_succeeded = 0
    placements_verified = 0
    targets_created = 0
    total = config.warmup_iterations + config.iterations
    for absolute_index in range(total):
        phase = "warmup" if absolute_index < config.warmup_iterations else "measure"
        iteration = (
            absolute_index
            if phase == "warmup"
            else absolute_index - config.warmup_iterations
        )
        computer: Any | None = None
        observed = False
        elapsed_ms: float | None = None
        computer_config = _computer_config(
            config,
            run_id=f"{run_tag}-create-{phase}-{iteration:03d}",
        )
        try:
            started = clock()
            computer = create_computer(
                config=computer_config,
                app_name=config.app_name,
                app_tags={"benchmark": "modal-optimized-provider"},
                tags={
                    "benchmark": "modal-optimized-provider",
                    "role": "create-target",
                    "sample_phase": phase,
                },
                wait=True,
            )
            targets_created += 1
            try:
                _validated_raw_frame(computer, computer_config)
            except Exception as exc:
                failures.append(_safe_failure("first_valid_frame", iteration, exc))
            else:
                elapsed_ms = max(0.0, (clock() - started) * 1000.0)
                try:
                    _require_matching_placement(runner_placement, computer.runtime_placement())
                except Exception as exc:
                    failures.append(_safe_failure("placement_validation", iteration, exc))
                else:
                    placements_verified += 1
                    observed = True
        except Exception as exc:
            failures.append(_safe_failure("create", iteration, exc))
        finally:
            cleanup = _cleanup_target(computer)
            if cleanup["attempted"]:
                cleanup_attempted += 1
            if cleanup["succeeded"]:
                cleanup_succeeded += 1
            elif cleanup["attempted"]:
                failures.append(
                    {
                        "phase": "cleanup",
                        "iteration": iteration,
                        "exception_type": str(cleanup["error_type"] or "CleanupFailed"),
                    }
                )
                observed = False
        if observed and elapsed_ms is not None:
            if phase == "warmup":
                successful_warmups += 1
            else:
                samples.append(elapsed_ms)
    expected_successes = config.iterations
    ok = (
        not failures
        and successful_warmups == config.warmup_iterations
        and len(samples) == expected_successes
        and cleanup_succeeded == total
    )
    return {
        "name": PRODUCT_CREATE_CASE,
        "status": "ok" if ok else "failed",
        "definition": (
            "public ComputerSandbox.create call to decoded, parsed, format- and "
            "geometry-validated full screenshot"
        ),
        "iterations": config.iterations,
        "successful_iterations": len(samples),
        "warmup_iterations": config.warmup_iterations,
        "successful_warmup_iterations": successful_warmups,
        "replacement_samples": 0,
        "fresh_target_per_attempt": True,
        "targets_created": targets_created,
        "target_attempts": total,
        "targets_reused": 0,
        "target_placements_verified": placements_verified,
        "samples_ms": samples,
        "summary_ms": _summary(samples),
        "cleanup": {
            "attempted": cleanup_attempted,
            "succeeded": cleanup_succeeded,
            "failures": [failure for failure in failures if failure["phase"] == "cleanup"],
        },
        "failures": failures,
    }


def _computer_config(config: ModalOptimizedProviderConfig, *, run_id: str) -> ComputerConfig:
    return ComputerConfig(
        runtime=RuntimeConfig(
            modal_region=config.region,
            timeout_seconds=config.sandbox_timeout_seconds,
            readiness_timeout_seconds=config.readiness_timeout_seconds,
        ),
        resources=ResourceConfig(
            profile="browser",
            cpu=config.cpu,
            memory_mib=config.memory_mib,
        ),
        image=ImageConfig(source="named", revision=config.image_revision),
        browser=BrowserConfig(kind=config.browser, prewarm=False),
        actions=ActionConfig(input_rate_limit_per_sec=0),
        run_id=run_id,
        ingress="connect",
    )


def _validated_raw_frame(computer: Any, config: ComputerConfig) -> bytes:
    payload = computer.screenshots.full_bytes(format="png", processing="daemon")
    return validate_first_frame(
        payload,
        expected_width=config.desktop.resolution[0],
        expected_height=config.desktop.resolution[1],
        image_format="png",
    )


def _require_matching_placement(
    runner: dict[str, str | None], target: dict[str, str | None]
) -> None:
    if not runner.get("cloud") or not runner.get("region") or target != runner:
        raise PlacementMismatchError("runner and target placement differ")


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


def _safe_failure(phase: str, iteration: int, exc: Exception) -> dict[str, Any]:
    return {"phase": phase, "iteration": iteration, "exception_type": type(exc).__name__}


def _safe_failures(value: Any, phase: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    failures: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        item_phase = item.get("phase") if isinstance(item, dict) else None
        item_iteration = item.get("iteration") if isinstance(item, dict) else None
        item_type = (
            item.get("exception_type") or item.get("type")
            if isinstance(item, dict)
            else None
        )
        failures.append(
            {
                "phase": item_phase if isinstance(item_phase, str) else phase,
                "iteration": (
                    item_iteration
                    if isinstance(item_iteration, int) and not isinstance(item_iteration, bool)
                    else index
                ),
                "exception_type": item_type if isinstance(item_type, str) else "BenchmarkFailure",
            }
        )
    return failures


def _safe_warm_surfaces(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError("warm surface benchmark returned a non-object")
    raw_surfaces = result.get("surfaces")
    if not isinstance(raw_surfaces, dict):
        raise ValueError("warm surface benchmark omitted surfaces")
    raw_surface = raw_surfaces.get("daemon-http")
    if not isinstance(raw_surface, dict):
        raise ValueError("warm surface benchmark omitted daemon-http")
    raw_cases = raw_surface.get("cases")
    if not isinstance(raw_cases, dict):
        raise ValueError("warm surface benchmark omitted cases")
    cases: dict[str, Any] = {}
    for name in WARM_CASES:
        raw_case = raw_cases.get(name)
        if not isinstance(raw_case, dict):
            raise ValueError(f"warm surface benchmark omitted {name}")
        cases[name] = {
            **{field: raw_case[field] for field in _WARM_CASE_FIELDS if field in raw_case},
            "failures": _safe_failures(raw_case.get("failures", []), name),
        }
    raw_verification = raw_surface.get("verification")
    if not isinstance(raw_verification, dict):
        raise ValueError("warm surface benchmark omitted verification")
    verification: dict[str, Any] = {}
    for name in ("cursor_position", "type_text"):
        item = raw_verification.get(name)
        if not isinstance(item, dict):
            raise ValueError(f"warm surface verification omitted {name}")
        verification[name] = {"status": item.get("status")}
    return {
        "daemon-http": {
            "status": raw_surface.get("status"),
            "failures": _safe_failures(raw_surface.get("failures", []), "daemon-http"),
            "cases": cases,
            "verification": verification,
        }
    }


def _summary(samples: list[float]) -> dict[str, float | None]:
    if not samples:
        return {"p50": None, "p95": None}
    values = sorted(samples)
    rank = 0.95 * (len(values) - 1)
    lower = math.floor(rank)
    fraction = rank - lower
    p95 = values[lower] + fraction * (values[math.ceil(rank)] - values[lower])
    return {"p50": float(statistics.median(values)), "p95": float(p95)}


def validate_modal_optimized_provider_artifact(
    payload: dict[str, Any], *, require_publishable: bool = True
) -> None:
    _validate_safe_value(payload)
    _require_exact_keys(
        payload,
        {
            "schema_version",
            "benchmark",
            "ok",
            "eligibility",
            "iterations",
            "warmup_iterations",
            "replacement_samples",
            "metadata",
            "runs",
            "runner_dispatch",
            "final_cleanup",
            "failures",
        },
        "artifact",
    )
    if payload.get("schema_version") != 1 or payload.get("benchmark") != "modal-optimized-provider":
        raise ValueError("Modal optimized provider artifact schema is unsupported")
    if payload.get("replacement_samples") != 0:
        raise ValueError("replacement samples are forbidden")
    if require_publishable:
        if payload.get("eligibility") != "publishable" or payload.get("ok") is not True:
            raise ValueError("artifact is not publishable")
        if payload.get("iterations") != 30 or payload.get("warmup_iterations") != 1:
            raise ValueError("publishable artifact requires 30 measured and one warmup")
    dispatch = payload.get("runner_dispatch")
    if (
        not isinstance(dispatch, dict)
        or dispatch.get("included_in_product_create_samples") is not False
    ):
        raise ValueError("runner startup must remain outside product create samples")
    _finite_nonnegative(dispatch.get("elapsed_ms"), "runner dispatch")
    _require_exact_keys(
        dispatch,
        {"elapsed_ms", "included_in_product_create_samples"},
        "runner dispatch",
    )
    runs = payload.get("runs")
    if isinstance(runs, dict):
        run = runs.get("modal_optimized_runner")
        if isinstance(run, dict):
            product_create = run.get("product_create")
            if isinstance(product_create, dict):
                _validate_case_measurements(product_create, "product create case")
            surfaces = run.get("surfaces")
            if isinstance(surfaces, dict):
                surface = surfaces.get("daemon-http")
                if isinstance(surface, dict) and isinstance(surface.get("cases"), dict):
                    for name, case in surface["cases"].items():
                        if isinstance(case, dict):
                            _validate_case_measurements(case, f"{name} case")
    cleanup = payload.get("final_cleanup")
    if not isinstance(cleanup, dict):
        raise ValueError("final cleanup is required")
    if require_publishable and (
        cleanup.get("cleanup_succeeded") is not True or cleanup.get("remaining_sandboxes") != 0
    ):
        raise ValueError("publishable artifact requires terminal cleanup")
    _require_exact_keys(
        cleanup,
        {"cleanup_succeeded", "remaining_sandboxes"},
        "final cleanup",
    )
    _validate_failure_records(payload.get("failures"), "artifact failures")
    if require_publishable or payload.get("ok") is True:
        _validate_complete_run(payload)


def _validate_complete_run(payload: dict[str, Any]) -> None:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("complete artifact requires metadata")
    _require_exact_keys(
        metadata,
        {
            "caller_topology",
            "runner_kind",
            "runner_invocations",
            "runner_startup_in_product_create_boundary",
            "modal_region",
            "modal_ingress",
            "daemon_http_version",
            "browser",
            "browser_prewarm",
            "image_revision",
            "runner_cpu",
            "runner_memory_mib",
            "target_cpu",
            "target_memory_mib",
            "input_rate_limit_per_sec",
            "subprocess_backend",
            "external_caller_included",
            "provenance",
        },
        "metadata",
    )
    runs = payload.get("runs")
    if not isinstance(runs, dict) or set(runs) != {"modal_optimized_runner"}:
        raise ValueError("complete artifact requires one optimized runner result")
    run = runs.get("modal_optimized_runner")
    if not isinstance(run, dict):
        raise ValueError("optimized runner result must be an object")
    _require_exact_keys(
        run,
        {
            "ok",
            "failures",
            "product_create",
            "surfaces",
            "warm_target_cleanup",
            "warm_target_placement_verified",
            "runner_placement",
        },
        "optimized runner result",
    )
    if run.get("ok") is not True:
        raise ValueError("successful artifact requires a successful optimized runner")
    _validate_failure_records(run.get("failures"), "runner failures")
    if run.get("failures") != []:
        raise ValueError("successful optimized runner cannot contain failures")
    product_create = run.get("product_create")
    if not isinstance(product_create, dict):
        raise ValueError("complete artifact requires product create measurements")
    _require_exact_keys(
        product_create,
        {
            "name",
            "status",
            "definition",
            "iterations",
            "successful_iterations",
            "warmup_iterations",
            "successful_warmup_iterations",
            "replacement_samples",
            "fresh_target_per_attempt",
            "targets_created",
            "target_attempts",
            "targets_reused",
            "target_placements_verified",
            "samples_ms",
            "summary_ms",
            "cleanup",
            "failures",
        },
        "product create measurements",
    )
    if product_create.get("iterations") != payload.get("iterations"):
        raise ValueError("product create iterations do not match the artifact")
    if product_create.get("warmup_iterations") != payload.get("warmup_iterations"):
        raise ValueError("product create warmups do not match the artifact")
    if (
        product_create.get("name") != PRODUCT_CREATE_CASE
        or product_create.get("status") != "ok"
        or product_create.get("successful_iterations") != payload.get("iterations")
        or product_create.get("successful_warmup_iterations")
        != payload.get("warmup_iterations")
        or product_create.get("replacement_samples") != 0
        or product_create.get("fresh_target_per_attempt") is not True
        or product_create.get("targets_reused") != 0
    ):
        raise ValueError("product create success contract is invalid")
    _validate_failure_records(product_create.get("failures"), "product create failures")
    if product_create.get("failures") != []:
        raise ValueError("successful product create cannot contain failures")
    lifecycle_cleanup = product_create.get("cleanup")
    if not isinstance(lifecycle_cleanup, dict):
        raise ValueError("product create cleanup is missing")
    _require_exact_keys(
        lifecycle_cleanup,
        {"attempted", "succeeded", "failures"},
        "product create cleanup",
    )
    _validate_failure_records(lifecycle_cleanup.get("failures"), "cleanup failures")
    expected_targets = int(payload["iterations"]) + int(payload["warmup_iterations"])
    if any(
        product_create.get(name) != expected_targets
        for name in (
            "targets_created",
            "target_attempts",
            "target_placements_verified",
        )
    ) or any(
        lifecycle_cleanup.get(name) != expected_targets for name in ("attempted", "succeeded")
    ):
        raise ValueError("product create target and cleanup counts are inconsistent")
    if lifecycle_cleanup.get("failures") != []:
        raise ValueError("successful product create cleanup cannot contain failures")
    warm_cleanup = run.get("warm_target_cleanup")
    if not isinstance(warm_cleanup, dict):
        raise ValueError("warm target cleanup is missing")
    _require_exact_keys(
        warm_cleanup,
        {"attempted", "succeeded", "error_type"},
        "warm target cleanup",
    )
    if warm_cleanup != {"attempted": True, "succeeded": True, "error_type": None}:
        raise ValueError("successful artifact requires warm target cleanup")
    if run.get("warm_target_placement_verified") is not True:
        raise ValueError("successful artifact requires warm target placement verification")
    placement = run.get("runner_placement")
    if not isinstance(placement, dict):
        raise ValueError("runner placement is missing")
    _require_exact_keys(placement, {"cloud", "region"}, "runner placement")
    if not all(isinstance(placement.get(name), str) and placement[name] for name in placement):
        raise ValueError("runner placement must be observed")
    _validate_complete_surfaces(run.get("surfaces"), int(payload["iterations"]))


def _validate_complete_surfaces(value: Any, iterations: int) -> None:
    if not isinstance(value, dict) or set(value) != {"daemon-http"}:
        raise ValueError("complete artifact requires one daemon-http surface")
    surface = value.get("daemon-http")
    if not isinstance(surface, dict):
        raise ValueError("daemon-http surface must be an object")
    _require_exact_keys(
        surface,
        {"status", "failures", "cases", "verification"},
        "daemon-http surface",
    )
    if surface.get("status") != "ok":
        raise ValueError("daemon-http surface must succeed")
    _validate_failure_records(surface.get("failures"), "daemon-http failures")
    if surface.get("failures") != []:
        raise ValueError("successful daemon-http surface cannot contain failures")
    cases = surface.get("cases")
    if not isinstance(cases, dict) or set(cases) != set(WARM_CASES):
        raise ValueError("daemon-http cases are not exact")
    for name, case in cases.items():
        if not isinstance(case, dict):
            raise ValueError(f"{name} case must be an object")
        allowed = {*_WARM_CASE_FIELDS, "failures"}
        if not set(case).issubset(allowed):
            raise ValueError(f"{name} case contains unsupported fields")
        if (
            case.get("status") != "ok"
            or case.get("iterations") != iterations
            or case.get("successful_iterations") != iterations
        ):
            raise ValueError(f"{name} case success contract is invalid")
        _validate_failure_records(case.get("failures"), f"{name} failures")
        if case.get("failures") != []:
            raise ValueError(f"successful {name} case cannot contain failures")
    verification = surface.get("verification")
    if not isinstance(verification, dict) or set(verification) != {
        "cursor_position",
        "type_text",
    }:
        raise ValueError("daemon-http verification is not exact")
    for name, item in verification.items():
        if item != {"status": "ok"}:
            raise ValueError(f"{name} verification must succeed")


def _validate_failure_records(value: Any, label: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    for failure in value:
        if not isinstance(failure, dict):
            raise ValueError(f"{label} entries must be objects")
        _require_exact_keys(
            failure,
            {"phase", "iteration", "exception_type"},
            label,
        )
        if (
            not isinstance(failure.get("phase"), str)
            or isinstance(failure.get("iteration"), bool)
            or not isinstance(failure.get("iteration"), int)
            or not isinstance(failure.get("exception_type"), str)
        ):
            raise ValueError(f"{label} entries are invalid")


def _require_exact_keys(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} fields are not exact")


def _validate_case_measurements(case: dict[str, Any], label: str) -> None:
    iterations = case.get("iterations")
    successful = case.get("successful_iterations")
    if (
        isinstance(iterations, bool)
        or not isinstance(iterations, int)
        or iterations < 0
        or isinstance(successful, bool)
        or not isinstance(successful, int)
        or successful < 0
        or successful > iterations
    ):
        raise ValueError(f"{label} has inconsistent iteration counts")
    samples = case.get("samples_ms")
    if not isinstance(samples, list) or len(samples) != successful:
        raise ValueError(f"{label} sample count is inconsistent")
    values = [_finite_nonnegative(value, f"{label} sample") for value in samples]
    if case.get("status") == "ok" and successful != iterations:
        raise ValueError(f"{label} successful count is inconsistent")
    summary = case.get("summary_ms")
    if not isinstance(summary, dict):
        raise ValueError(f"{label} summary is missing")
    expected = _summary(values)
    for quantile in ("p50", "p95"):
        actual_value = summary.get(quantile)
        expected_value = expected[quantile]
        if expected_value is None:
            if actual_value is not None:
                raise ValueError(f"{label} summary is inconsistent")
        elif not math.isclose(
            _finite_nonnegative(actual_value, f"{label} summary {quantile}"),
            expected_value,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValueError(f"{label} summary is inconsistent")


def _finite_nonnegative(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} must be finite and nonnegative")
    return number


def _validate_safe_value(value: Any, *, key: str | None = None) -> None:
    if key is not None:
        normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower().replace("-", "_")
        if normalized in _UNSAFE_KEYS or normalized.endswith(
            ("_token", "_secret", "_password", "_resource_id", "_run_id")
        ):
            raise ValueError(f"artifact contains unsafe field: {key}")
    if isinstance(value, dict):
        for item_key, item in value.items():
            _validate_safe_value(item, key=str(item_key))
    elif isinstance(value, list):
        for item in value:
            _validate_safe_value(item)
    elif isinstance(value, str) and re.search(r"https?://", value, flags=re.IGNORECASE):
        raise ValueError("artifact contains unsafe URL")
