from __future__ import annotations

from typing import Any

PricingRate = dict[str, Any]

PRICING_RETRIEVED_DATE = "2026-07-18"
PRICING_SOURCES = {
    "modal": "https://modal.com/products/sandboxes",
}

PUBLIC_RATE_CATALOG: dict[str, dict[str, PricingRate]] = {
    "daemon-http": {
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
}

NON_BILLING_SURFACES = {
    "openai-adapter",
    "anthropic-adapter",
    "action-executor",
    "sandbox-exec",
}


def estimate_surface_cost(
    surface: str,
    *,
    surface_status: str,
    runtime_seconds: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Estimate SDK benchmark surface cost from Modal public rates and safe metadata."""

    if surface in NON_BILLING_SURFACES:
        return _not_applicable("benchmark surface does not create billable resources")
    if surface_status in {"not_measured", "unavailable"}:
        return _not_measured(f"surface status was {surface_status}")
    if runtime_seconds is None or runtime_seconds <= 0:
        return _unknown("measured surface runtime was unavailable")

    rates = PUBLIC_RATE_CATALOG.get(surface)
    if rates is None:
        return _unknown("no public pricing catalog entry is configured for this surface")

    safe_metadata = metadata or {}
    resources = _resource_inputs(safe_metadata)
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
    missing_allocation_notes = [
        note for note in notes if note.endswith("allocation was unavailable")
    ]
    complete = bool(components) and not missing_allocation_notes
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
        or "caller_supplied_measured_runtime",
        "confidence": "estimated" if complete else "partial",
        "notes": notes,
        "pricing": {
            "retrieved_date": PRICING_RETRIEVED_DATE,
            "source_url": PRICING_SOURCES.get("modal"),
        },
    }


def _resource_inputs(metadata: dict[str, Any]) -> dict[str, float | None]:
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
    return {
        "status": "not_applicable",
        "currency": "USD",
        "total": None,
        "components": [],
        "inputs": {},
        "duration_policy": "not_applicable",
        "confidence": "not_applicable",
        "notes": [reason],
    }


def _not_measured(reason: str) -> dict[str, Any]:
    return {
        "status": "not_measured",
        "currency": "USD",
        "total": None,
        "components": [],
        "inputs": {},
        "duration_policy": "not_measured",
        "confidence": "not_measured",
        "notes": [reason],
    }


def _unknown(reason: str) -> dict[str, Any]:
    return {
        "status": "unknown",
        "currency": "USD",
        "total": None,
        "components": [],
        "inputs": {},
        "duration_policy": "unknown",
        "confidence": "unknown",
        "notes": [reason],
    }
