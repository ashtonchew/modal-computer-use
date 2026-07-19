from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol

from ..constants import PROVIDER_BENCHMARK_TEXT, TYPE_1000_CHARS_TEXT
from ..lifecycle import (
    CleanupError,
    cleanup_lifecycle_resource,
    measure_create_to_first_observation,
)
from ..measurement import _case_result, _measure_observed_case
from ..safety import _failure, _redact_text
from .provider_sdk import sanitize_provider_observation
from .results import build_provider_cleanup_case, build_provider_result
from .verification import TYPE_READBACK_TEXT


class ProductProviderDriver(Protocol):
    def create_lifecycle_session(self) -> Any: ...

    def observe_first_screenshot(self, sandbox: Any) -> dict[str, Any]: ...

    def cleanup_session(self, sandbox: Any) -> list[CleanupError]: ...


def run_product_provider_cases(
    *,
    provider: str,
    driver: ProductProviderDriver,
    cold_cases: tuple[str, ...],
    warm_cases: tuple[str, ...],
    iterations: int,
    warmup_iterations: int,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    cleanup_errors: list[CleanupError] = []
    results: dict[str, Any] = {}
    measured_runtime_seconds = 0.0
    verification: dict[str, Any] | None = None
    for case in cold_cases:
        result_name = _provider_cold_case_name(case)
        lifecycle = measure_create_to_first_observation(
            name=result_name,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            create=driver.create_lifecycle_session,
            observe=driver.observe_first_screenshot,
            cleanup=driver.cleanup_session,
            redacted_text=_provider_case_redacted_text(result_name),
        )
        cleanup_errors.extend(lifecycle.cleanup_errors)
        measured_runtime_seconds += lifecycle.completed_runtime_seconds
        result = _case_result(result_name, iterations, lifecycle.samples_ms, lifecycle.failures)
        result["last_result"] = (
            sanitize_provider_observation(lifecycle.observations[-1])
            if lifecycle.observations
            else None
        )
        _annotate_product_readiness_case(result, metadata)
        results[result_name] = result
        if result_name != case:
            results[case] = {
                **result,
                "name": case,
                "canonical_case": result_name,
                "deprecated": True,
                "removal_version": "1.2.0",
            }
    if warm_cases:
        sandbox: Any | None = None
        warm_start = time.perf_counter()
        try:
            sandbox = driver.create_lifecycle_session()
            driver.observe_first_screenshot(sandbox)
        except Exception as exc:
            if sandbox is not None:
                cleanup_errors.extend(cleanup_lifecycle_resource(driver.cleanup_session, sandbox))
            measured_runtime_seconds += time.perf_counter() - warm_start
            for case in warm_cases:
                failure = _failure(
                    case,
                    phase="setup",
                    iteration=0,
                    exc=exc,
                    redacted_text=_provider_case_redacted_text(case),
                )
                results[case] = _case_result(case, iterations, [], [failure])
        else:
            try:
                resource_metadata = getattr(driver, "resource_metadata", None)
                if callable(resource_metadata):
                    metadata = _merge_provider_resource_metadata(
                        metadata, resource_metadata(sandbox)
                    )
                for case in warm_cases:
                    operation = getattr(driver, case)
                    case_failures: list[dict[str, Any]] = []
                    samples, observations = _measure_observed_case(
                        name=case,
                        iterations=iterations,
                        warmup_iterations=warmup_iterations,
                        operation=_bind_provider_operation(operation, sandbox),
                        failures=case_failures,
                        redacted_text=_provider_case_redacted_text(case),
                    )
                    result = _case_result(case, iterations, samples, case_failures)
                    result["last_result"] = (
                        sanitize_provider_observation(observations[-1]) if observations else None
                    )
                    results[case] = result
            finally:
                verifier = getattr(driver, "verify_readbacks", None)
                if callable(verifier):
                    try:
                        verification = sanitize_provider_observation(verifier(sandbox))
                    except Exception as exc:
                        verification = {
                            "status": "failed",
                            "message": _redact_text(str(exc), TYPE_READBACK_TEXT),
                        }
                warm_errors = cleanup_lifecycle_resource(driver.cleanup_session, sandbox)
                cleanup_errors.extend(warm_errors)
                measured_runtime_seconds += time.perf_counter() - warm_start
    cleanup_case = build_provider_cleanup_case(cleanup_errors)
    if cleanup_case is not None:
        results["cleanup"] = cleanup_case
    if cleanup_errors:
        metadata = dict(metadata)
        metadata["cost_notes"] = ["cleanup failed; leaked resources may incur unmeasured cost"]
    return build_provider_result(
        provider,
        cases=results,
        metadata=metadata,
        runtime_seconds=measured_runtime_seconds,
        verification=verification,
    )


def wait_for_provider_screenshot_ready(
    screenshot_operation: Callable[[Any], dict[str, Any]],
    sandbox: Any,
    *,
    attempts: int = 5,
    delay_seconds: float = 1.0,
) -> None:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            screenshot_operation(sandbox)
            return
        except Exception as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(delay_seconds)
    if last_error is not None:
        raise last_error


def cleanup_provider_sandbox(sandbox: Any) -> list[tuple[str, Exception]]:
    errors: list[tuple[str, Exception]] = []
    for method_name in ("delete", "kill", "stop"):
        method = getattr(sandbox, method_name, None)
        if not callable(method):
            continue
        try:
            method()
            return []
        except Exception as exc:
            errors.append((method_name, exc))
        try:
            method(force=True)
            return []
        except Exception as exc:
            errors.append((f"{method_name}(force=True)", exc))
    return errors


def _bind_provider_operation(operation: Callable[[Any], Any], sandbox: Any) -> Callable[[], Any]:
    def run() -> Any:
        return operation(sandbox)

    return run


def _provider_case_redacted_text(case: str) -> str | None:
    if case == "type_1000_chars":
        return TYPE_1000_CHARS_TEXT
    return PROVIDER_BENCHMARK_TEXT


def _provider_cold_case_name(case: str) -> str:
    if case == "cold_create_to_ready":
        return "product_create_to_first_screenshot"
    return case


def _annotate_product_readiness_case(
    case: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    case["definition"] = "provider product create call to first successful full-screen screenshot"
    for key in (
        "startup_model",
        "uses_snapshot_or_template",
        "readiness_contract",
        "setup_included",
        "ingress_included",
        "first_observation_api",
    ):
        if key in metadata:
            case[key] = metadata[key]


def _merge_provider_resource_metadata(
    metadata: dict[str, Any], resource_metadata: dict[str, Any]
) -> dict[str, Any]:
    if not resource_metadata:
        return metadata
    merged = {**metadata, **resource_metadata}
    if any(
        resource_metadata.get(key) == "provider_sandbox_metadata"
        for key in ("cpu_count_source", "memory_gib_source", "storage_gib_source")
    ):
        merged.pop("cost_notes", None)
    return merged
