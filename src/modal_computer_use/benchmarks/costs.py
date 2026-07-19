from __future__ import annotations

from collections.abc import Callable
from typing import Any

PricingRate = dict[str, Any]

PRICING_RETRIEVED_DATE = "2026-07-18"
PRICING_SOURCES = {
    "modal": "https://modal.com/products/sandboxes",
    "e2b": "https://e2b.dev/pricing",
    "daytona": "https://www.daytona.io/pricing",
}

PUBLIC_RATE_CATALOG: dict[str, dict[str, PricingRate]] = {
    "modal": {
        "cpu": {
            "rate": 0.00003942,
            "rate_unit": "USD_per_core_second",
            "quantity_unit": "core_seconds",
        },
        "memory": {
            "rate": 0.00000667,
            "rate_unit": "USD_per_GiB_second",
            "quantity_unit": "GiB_seconds",
        },
    },
    "e2b": {
        "cpu": {
            "rate": 0.000014,
            "rate_unit": "USD_per_vCPU_second",
            "quantity_unit": "vCPU_seconds",
        },
        "memory": {
            "rate": 0.0000045,
            "rate_unit": "USD_per_GiB_second",
            "quantity_unit": "GiB_seconds",
        },
    },
    "daytona": {
        "cpu": {
            "rate": 0.000014,
            "rate_unit": "USD_per_vCPU_second",
            "quantity_unit": "vCPU_seconds",
        },
        "memory": {
            "rate": 0.0000045,
            "rate_unit": "USD_per_GiB_second",
            "quantity_unit": "GiB_seconds",
        },
        "storage": {
            "rate": 0.00000003,
            "rate_unit": "USD_per_GiB_second",
            "quantity_unit": "GiB_seconds",
        },
    },
}

NON_BILLING_SURFACES = {
    "openai-adapter",
    "anthropic-adapter",
    "action-executor",
    "sandbox-exec",
}
NON_BILLING_PROVIDERS = {"openai", "anthropic", "generic", "modal-exec"}


def estimate_surface_cost(
    surface: str,
    *,
    surface_status: str,
    runtime_seconds: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if surface in NON_BILLING_SURFACES:
        return _not_applicable("benchmark surface does not create billable resources")
    return _estimate_cost(
        subject_kind="surface",
        subject_status=surface_status,
        runtime_seconds=runtime_seconds,
        metadata=metadata,
        rates=PUBLIC_RATE_CATALOG.get("modal") if surface == "daemon-http" else None,
        pricing_source="modal",
        resource_inputs=_modal_resource_inputs,
        duration_policy="measured_wall_time_including_warmup",
    )


def estimate_provider_cost(
    provider: str,
    *,
    provider_status: str,
    runtime_seconds: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if provider in NON_BILLING_PROVIDERS:
        return _not_applicable("provider comparison does not create billable provider resources")
    pricing_source = "modal" if provider == "modal-daemon" else provider
    return _estimate_cost(
        subject_kind="provider",
        subject_status=provider_status,
        runtime_seconds=runtime_seconds,
        metadata=metadata,
        rates=PUBLIC_RATE_CATALOG.get(pricing_source),
        pricing_source=pricing_source,
        resource_inputs=lambda safe_metadata: _provider_resource_inputs(provider, safe_metadata),
        duration_policy="measured_billable_resource_lifetime_including_cleanup",
    )


def _estimate_cost(
    *,
    subject_kind: str,
    subject_status: str,
    runtime_seconds: float | None,
    metadata: dict[str, Any] | None,
    rates: dict[str, PricingRate] | None,
    pricing_source: str,
    resource_inputs: Callable[[dict[str, Any]], dict[str, float | None]],
    duration_policy: str,
) -> dict[str, Any]:
    if subject_status in {"not_measured", "unavailable"}:
        return _not_measured(f"{subject_kind} status was {subject_status}")
    if runtime_seconds is None or runtime_seconds <= 0:
        return _unknown(f"measured {subject_kind} runtime was unavailable")
    if rates is None:
        return _unknown(f"no public pricing catalog entry is configured for this {subject_kind}")

    safe_metadata = metadata or {}
    resources = resource_inputs(safe_metadata)
    components: list[dict[str, Any]] = []
    notes: list[str] = []
    extra_notes = safe_metadata.get("cost_notes")
    if isinstance(extra_notes, list):
        notes.extend(str(note) for note in extra_notes)
    for resource, rate in rates.items():
        quantity_value = resources.get(_resource_quantity_key(resource))
        if quantity_value is None:
            notes.append(f"{resource} allocation was unavailable")
            continue
        quantity = float(quantity_value) * runtime_seconds
        components.append(
            {
                "resource": resource,
                "amount": quantity * float(rate["rate"]),
                "quantity": quantity,
                "quantity_unit": rate["quantity_unit"],
                "rate": rate["rate"],
                "rate_unit": rate["rate_unit"],
                "source": "public_pricing_docs",
            }
        )

    total_amount = sum(float(component["amount"]) for component in components)
    missing_allocations = [note for note in notes if note.endswith("allocation was unavailable")]
    complete = bool(components) and not missing_allocations
    status = "estimated" if complete else "partial"
    return {
        "status": status,
        "currency": "USD",
        "total": {"amount": total_amount, "unit": "run"} if components else None,
        "components": components,
        "inputs": {
            "duration_seconds": runtime_seconds,
            "cpu_count": resources.get("cpu_count"),
            "memory_gib": resources.get("memory_gib"),
            "storage_gib": resources.get("storage_gib"),
        },
        "duration_policy": safe_metadata.get(
            "cost_duration_policy",
            (
                safe_metadata.get("environment", {}).get("cost_duration_policy")
                if isinstance(safe_metadata.get("environment"), dict)
                else None
            ),
        )
        or duration_policy,
        "confidence": "estimated" if complete else "partial",
        "notes": notes,
        "pricing": {
            "retrieved_date": PRICING_RETRIEVED_DATE,
            "source_url": PRICING_SOURCES.get(pricing_source),
        },
    }


def _modal_resource_inputs(metadata: dict[str, Any]) -> dict[str, float | None]:
    environment = (
        metadata.get("environment") if isinstance(metadata.get("environment"), dict) else {}
    )
    return {
        "cpu_count": _float_or_none(environment.get("modal_cpu_count"))
        or _float_or_none(metadata.get("cpu_count")),
        "memory_gib": _float_or_none(environment.get("modal_memory_gib"))
        or _float_or_none(metadata.get("memory_gib")),
        "storage_gib": None,
    }


def _provider_resource_inputs(
    provider: str, metadata: dict[str, Any]
) -> dict[str, float | None]:
    if provider == "modal-daemon":
        return _modal_resource_inputs(metadata)
    if provider == "e2b":
        return {
            "cpu_count": _float_or_none(metadata.get("cpu_count")) or 2.0,
            "memory_gib": _float_or_none(metadata.get("memory_gib")),
            "storage_gib": _float_or_none(metadata.get("storage_gib")),
        }
    return {
        "cpu_count": _float_or_none(metadata.get("cpu_count")),
        "memory_gib": _float_or_none(metadata.get("memory_gib")),
        "storage_gib": _float_or_none(metadata.get("storage_gib")),
    }


def _resource_quantity_key(resource: str) -> str:
    if resource == "cpu":
        return "cpu_count"
    if resource == "memory":
        return "memory_gib"
    return "storage_gib"


def _float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _not_applicable(reason: str) -> dict[str, Any]:
    return _empty_cost("not_applicable", reason)


def _not_measured(reason: str) -> dict[str, Any]:
    return _empty_cost("not_measured", reason)


def _unknown(reason: str) -> dict[str, Any]:
    return _empty_cost("unknown", reason)


def _empty_cost(status: str, reason: str) -> dict[str, Any]:
    return {
        "status": status,
        "currency": "USD",
        "total": None,
        "components": [],
        "inputs": {},
        "duration_policy": status,
        "confidence": status,
        "notes": [reason],
    }
