from __future__ import annotations

from typing import Any

from . import core
from .costs import estimate_surface_cost


def _surface_result(
    surface: str,
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
        if not isinstance(case, dict):
            continue
        failures.extend(core._benchmark_failures(case_name, case.get("failures", [])))
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
        "surface": surface,
        "metadata": safe_metadata,
        "cases": cases,
        "failures": failures,
    }
    if verification is not None:
        result["verification"] = verification
    if billing_reconciliation is not None:
        result["billing_reconciliation"] = billing_reconciliation
    result["cost_estimate"] = estimate_surface_cost(
        surface,
        surface_status=status,
        runtime_seconds=runtime_seconds,
        metadata=safe_metadata,
    )
    return result

def _surface_not_measured(surface: str, reason: str) -> dict[str, Any]:
    return _surface_result(
        surface,
        cases={"setup": {"status": "not_measured", "reason": reason, "failures": []}},
    )
