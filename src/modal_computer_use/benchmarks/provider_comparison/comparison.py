from __future__ import annotations

import time
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Literal

from ..._version import __version__
from ...client import DaemonClient
from ..constants import (
    DEFAULT_COMPARE_PROVIDERS,
    PROVIDER_BENCHMARK_TEXT,
    BenchmarkSurface,
    ComparisonProvider,
)
from ..measurement import _case_result
from ..metadata import _benchmark_failures
from ..safety import _failure, _safe_base_url
from ..surfaces import run_sdk_surface_benchmark
from .daytona import run_daytona_provider
from .e2b import run_e2b_provider
from .results import (
    apply_verification_status,
    build_provider_result,
    dict_value,
    provider_not_measured,
)
from .tzafon import run_tzafon_provider

ProviderMode = Literal["mock-local", "http", "provider-live"]

_ADAPTER_SURFACES: dict[ComparisonProvider, BenchmarkSurface] = {
    "openai": "openai-adapter",
    "anthropic": "anthropic-adapter",
    "generic": "action-executor",
}

_ADAPTER_METADATA: dict[str, dict[str, Any]] = {
    "openai": {
        "adapter": "OpenAIAdapter",
        "provider_api_calls": False,
        "target_kind": "adapter",
    },
    "anthropic": {
        "adapter": "AnthropicAdapter",
        "tool_version": "computer_20250124",
        "provider_api_calls": False,
        "target_kind": "adapter",
    },
    "generic": {
        "adapter": "ActionExecutor",
        "provider_api_calls": False,
        "target_kind": "adapter",
    },
}


def run_provider_comparison(
    *,
    providers: list[ComparisonProvider] | None = None,
    iterations: int,
    client: DaemonClient | None = None,
    mode: ProviderMode = "provider-live",
    base_url: str | None = None,
    warmup_iterations: int = 1,
    sandbox_exec_runner: Any = None,
    sandbox_exec_setup_failure: dict[str, Any] | None = None,
    environment_metadata: dict[str, Any] | None = None,
    billing_reconciliation_request: dict[str, Any] | None = None,
    precomputed_provider_results: dict[str, dict[str, Any]] | None = None,
    modal_action_pacing_seconds: float | None = None,
    benchmark_case: str = "all",
    modal_action_frame_runner: Any = None,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")

    selected = providers or list(DEFAULT_COMPARE_PROVIDERS)
    failures: list[dict[str, Any]] = []
    provider_results: dict[str, Any] = {}
    for provider in selected:
        if precomputed_provider_results and provider in precomputed_provider_results:
            result = precomputed_provider_results[provider]
        else:
            try:
                result = run_provider(
                    provider,
                    client=client,
                    mode=mode,
                    base_url=base_url,
                    iterations=iterations,
                    warmup_iterations=warmup_iterations,
                    sandbox_exec_runner=sandbox_exec_runner,
                    sandbox_exec_setup_failure=sandbox_exec_setup_failure,
                    environment_metadata=environment_metadata,
                    billing_reconciliation_request=billing_reconciliation_request,
                    modal_action_pacing_seconds=modal_action_pacing_seconds,
                    benchmark_case=benchmark_case,
                    modal_action_frame_runner=modal_action_frame_runner,
                )
            except Exception as exc:
                result = build_provider_result(
                    provider,
                    cases={
                        "setup": _case_result(
                            "setup",
                            iterations,
                            [],
                            [
                                _failure(
                                    "setup",
                                    phase="setup",
                                    iteration=0,
                                    exc=exc,
                                    redacted_text=PROVIDER_BENCHMARK_TEXT,
                                )
                            ],
                        )
                    },
                )
        provider_results[provider] = result
        failures.extend(_benchmark_failures(provider, result.get("failures", [])))
    return {
        "ok": not failures,
        "benchmark": "provider-compare",
        "generated_at": datetime.now(UTC).isoformat(),
        "package_version": __version__,
        "mode": mode,
        "base_url": _safe_base_url(base_url),
        "iterations": iterations,
        "warmup_iterations": warmup_iterations,
        "benchmark_case": benchmark_case,
        "metadata": {
            "environment": {
                key: value
                for key, value in (environment_metadata or {}).items()
                if value is not None
            },
            "providers": selected,
        },
        "providers": provider_results,
        "failures": failures,
    }


def run_provider(
    provider: ComparisonProvider,
    *,
    client: DaemonClient | None,
    mode: ProviderMode,
    base_url: str | None,
    iterations: int,
    warmup_iterations: int,
    sandbox_exec_runner: Any,
    sandbox_exec_setup_failure: dict[str, Any] | None,
    environment_metadata: dict[str, Any] | None,
    billing_reconciliation_request: dict[str, Any] | None = None,
    modal_action_pacing_seconds: float | None = None,
    benchmark_case: str = "all",
    modal_action_frame_runner: Any = None,
) -> dict[str, Any]:
    if provider == "modal-daemon":
        return _run_modal_daemon_provider(
            client=client,
            mode=mode,
            base_url=base_url,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            environment_metadata=environment_metadata,
            billing_reconciliation_request=billing_reconciliation_request,
            action_pacing_seconds=modal_action_pacing_seconds,
            benchmark_case=benchmark_case,
            action_frame_runner=modal_action_frame_runner,
        )
    if provider == "modal-exec":
        surface_payload = run_sdk_surface_benchmark(
            surfaces=["sandbox-exec"],
            mode="mock-local" if mode == "mock-local" else "http",
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            sandbox_exec_runner=sandbox_exec_runner,
            sandbox_exec_setup_failure=sandbox_exec_setup_failure,
            environment_metadata=environment_metadata,
        )
        return project_surface_result("modal-exec", surface_payload["surfaces"]["sandbox-exec"])
    if provider in _ADAPTER_SURFACES:
        return _run_adapter_provider(
            provider=provider,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        )
    if provider == "daytona":
        if mode == "mock-local":
            return provider_not_measured(
                provider,
                "live Daytona benchmarks are disabled in mock-local mode",
            )
        return run_daytona_provider(
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            benchmark_case=benchmark_case,
        )
    if provider == "e2b":
        if mode == "mock-local":
            return provider_not_measured(
                provider,
                "live E2B benchmarks are disabled in mock-local mode",
            )
        return run_e2b_provider(
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            benchmark_case=benchmark_case,
        )
    if provider == "tzafon":
        if mode == "mock-local":
            return provider_not_measured(
                provider,
                "live Tzafon benchmarks are disabled in mock-local mode",
            )
        return run_tzafon_provider(
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            benchmark_case=benchmark_case,
        )
    return provider_not_measured(str(provider), "unknown provider")


def project_surface_result(provider: str, surface: dict[str, Any]) -> dict[str, Any]:
    result = {
        "status": surface.get("status", "not_measured"),
        "provider": provider,
        "metadata": {
            **dict_value(surface.get("metadata")),
            "canonical_source": "benchmark sdk surface",
            "provider_surface": surface.get("surface"),
            "target_kind": "product" if provider == "modal-daemon" else "transport_baseline",
        },
        "cases": dict_value(surface.get("cases")),
        "failures": list(surface.get("failures", [])),
    }
    for key in ("verification", "billing_reconciliation", "cost_estimate"):
        if key in surface:
            result[key] = surface[key]
    return apply_verification_status(result)


def _run_modal_daemon_provider(
    *,
    client: DaemonClient | None,
    mode: str,
    base_url: str | None,
    iterations: int,
    warmup_iterations: int,
    environment_metadata: dict[str, Any] | None,
    billing_reconciliation_request: dict[str, Any] | None,
    action_pacing_seconds: float | None,
    benchmark_case: str,
    action_frame_runner: Any,
) -> dict[str, Any]:
    if client is None:
        return provider_not_measured(
            "modal-daemon",
            "modal-daemon comparison requires --mock-local or --base-url",
        )
    if benchmark_case in {"action_to_immediate_frame", "action-to-immediate-frame"}:
        if callable(action_frame_runner):
            return action_frame_runner(
                client=client,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
                environment_metadata=environment_metadata,
            )
        return provider_not_measured(
            "modal-daemon",
            "canonical action-to-frame comparison requires an application-owned placed "
            "Modal Function and one borrowed session; direct daemon comparison is excluded",
        )
    pace = (
        (lambda: time.sleep(action_pacing_seconds))
        if isinstance(action_pacing_seconds, int | float)
        and not isinstance(action_pacing_seconds, bool)
        and action_pacing_seconds > 0
        else None
    )
    surface_payload = run_sdk_surface_benchmark(
        surfaces=["daemon-http"],
        client=client,
        mode="mock-local" if mode == "mock-local" else "http",
        iterations=iterations,
        base_url=base_url,
        warmup_iterations=warmup_iterations,
        environment_metadata=environment_metadata,
        billing_reconciliation_request=billing_reconciliation_request,
        typing_method="auto",
        typing_delay_ms=10,
        before_daemon_action_iteration=pace,
    )
    return project_surface_result("modal-daemon", surface_payload["surfaces"]["daemon-http"])


def _run_adapter_provider(
    *,
    provider: ComparisonProvider,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    surface = _ADAPTER_SURFACES[provider]
    surface_payload = run_sdk_surface_benchmark(
        surfaces=[surface],
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )
    surface_result = surface_payload["surfaces"][surface]
    cases = deepcopy(dict_value(surface_result.get("cases")))
    _rename_adapter_failure_cases(cases, surface=surface, provider=provider)
    return build_provider_result(
        provider,
        cases=cases,
        metadata=dict(_ADAPTER_METADATA[provider]),
        runtime_seconds=None,
    )


def _rename_adapter_failure_cases(cases: dict[str, Any], *, surface: str, provider: str) -> None:
    old_name = f"{surface}_matrix"
    new_name = f"{provider}_adapter_matrix"
    for case in cases.values():
        if not isinstance(case, dict):
            continue
        # Case results have no embedded name; only nested failures retain the measurement name.
        for failure in case.get("failures", []):
            if isinstance(failure, dict) and failure.get("case") == old_name:
                failure["case"] = new_name
