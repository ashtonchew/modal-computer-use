from __future__ import annotations

from typing import Any

from ..constants import PROVIDER_BENCHMARK_TEXT
from ..costs import estimate_provider_cost
from ..lifecycle import CleanupError
from ..measurement import _case_result
from ..metadata import _benchmark_failures
from ..safety import _failure


def build_provider_result(
    provider: str,
    *,
    cases: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    runtime_seconds: float | None = None,
    verification: dict[str, Any] | None = None,
    billing_reconciliation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    measured = False
    failed = False
    for case_name, case in cases.items():
        if not isinstance(case, dict) or case.get("deprecated") is True:
            continue
        failures.extend(_benchmark_failures(case_name, case.get("failures", [])))
        status = case.get("status")
        failed = failed or status == "failed"
        measured = measured or status == "ok"
    if failed:
        status = "failed"
    elif measured:
        status = "ok"
    else:
        statuses = {case.get("status") for case in cases.values() if isinstance(case, dict)}
        status = "unavailable" if "unavailable" in statuses else "not_measured"

    safe_metadata = metadata or {}
    result = {
        "status": status,
        "provider": provider,
        "metadata": safe_metadata,
        "cases": cases,
        "failures": failures,
    }
    if verification is not None:
        result["verification"] = verification
    if billing_reconciliation is not None:
        result["billing_reconciliation"] = billing_reconciliation
    result["cost_estimate"] = estimate_provider_cost(
        provider,
        provider_status=status,
        runtime_seconds=runtime_seconds,
        metadata=safe_metadata,
    )
    return apply_verification_status(result)


def provider_not_measured(provider: str, reason: str) -> dict[str, Any]:
    return build_provider_result(
        provider,
        cases={"setup": {"status": "not_measured", "reason": reason, "failures": []}},
    )


def provider_unavailable(provider: str, reason: str) -> dict[str, Any]:
    return build_provider_result(
        provider,
        cases={"setup": {"status": "unavailable", "reason": reason, "failures": []}},
    )


def record_provider_runtime(
    payload: dict[str, Any],
    *,
    provider: str,
    runtime_seconds: float,
) -> None:
    provider_result = dict_value(dict_value(payload.get("providers")).get(provider))
    metadata = dict_value(provider_result.get("metadata"))
    environment = dict_value(metadata.get("environment"))
    environment["measured_resource_runtime_seconds"] = runtime_seconds
    metadata["environment"] = environment
    provider_result["metadata"] = metadata
    provider_result["cost_estimate"] = estimate_provider_cost(
        provider,
        provider_status=str(provider_result.get("status", "not_measured")),
        runtime_seconds=runtime_seconds,
        metadata=metadata,
    )


def record_provider_cleanup_errors(
    payload: dict[str, Any],
    *,
    provider: str,
    errors: list[CleanupError],
) -> None:
    if not errors:
        return
    provider_result = dict_value(dict_value(payload.get("providers")).get(provider))
    cleanup_case = build_provider_cleanup_case(errors)
    if cleanup_case is None:
        return
    cases = dict_value(provider_result.get("cases"))
    cases["cleanup"] = cleanup_case
    provider_result["cases"] = cases
    provider_result["status"] = "failed"
    provider_result["failures"] = [
        *list(provider_result.get("failures", [])),
        *_benchmark_failures("cleanup", cleanup_case.get("failures", [])),
    ]
    refresh_comparison_failures(payload)


def build_provider_cleanup_case(errors: list[CleanupError]) -> dict[str, Any] | None:
    if not errors:
        return None
    failures = []
    for index, (method, exc) in enumerate(errors):
        failure = _failure(
            "cleanup",
            phase="cleanup",
            iteration=index,
            exc=exc,
            redacted_text=PROVIDER_BENCHMARK_TEXT,
        )
        failure["method"] = method
        failures.append(failure)
    return _case_result("cleanup", len(failures), [], failures)


def refresh_comparison_failures(payload: dict[str, Any]) -> None:
    failures: list[dict[str, Any]] = []
    for provider, result in dict_value(payload.get("providers")).items():
        if isinstance(result, dict):
            failures.extend(_benchmark_failures(provider, result.get("failures", [])))
    payload["failures"] = failures
    payload["ok"] = not failures


def apply_verification_status(result: dict[str, Any]) -> dict[str, Any]:
    verification_failures = collect_verification_failures(result.get("verification"))
    if not verification_failures:
        return result
    result["status"] = "failed"
    result["failures"] = [*list(result.get("failures", [])), *verification_failures]
    return result


def collect_verification_failures(
    value: Any, *, path: tuple[str, ...] = ()
) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    if value.get("status") == "failed":
        case = ".".join(("verification", *path))
        return [
            {
                "case": case,
                "phase": "verification",
                "iteration": 0,
                "type": "VerificationFailed",
                "message": str(value.get("message") or "readback verification failed"),
            }
        ]
    failures: list[dict[str, Any]] = []
    for key, item in value.items():
        if isinstance(item, dict):
            failures.extend(collect_verification_failures(item, path=(*path, str(key))))
    return failures


def dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
