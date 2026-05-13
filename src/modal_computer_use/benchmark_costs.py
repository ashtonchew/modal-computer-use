from __future__ import annotations

from typing import Any

PricingRate = dict[str, Any]

PRICING_RETRIEVED_DATE = "2026-05-12"
PRICING_SOURCES = {
    "modal": "https://modal.com/products/sandboxes",
    "e2b": "https://e2b.dev/pricing",
    "daytona": "https://www.daytona.io/pricing",
}

PUBLIC_RATE_CATALOG: dict[str, dict[str, PricingRate]] = {
    "modal-daemon": {
        "cpu": {
            "rate": 0.00003942,
            "rate_unit": "USD_per_core_second",
            "quantity_unit": "core_seconds",
        },
        "memory": {
            "rate": 0.00000672,
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

NON_BILLING_PROVIDERS = {"openai", "anthropic", "generic", "modal-exec"}


def estimate_provider_cost(
    provider: str,
    *,
    provider_status: str,
    runtime_seconds: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Estimate benchmark run cost from public rates and safe resource metadata."""

    if provider in NON_BILLING_PROVIDERS:
        return _not_applicable("provider comparison does not create billable provider resources")
    if provider_status in {"not_measured", "unavailable"}:
        return _not_measured(f"provider status was {provider_status}")
    if runtime_seconds is None or runtime_seconds <= 0:
        return _unknown("measured provider runtime was unavailable")

    rates = PUBLIC_RATE_CATALOG.get(provider)
    if rates is None:
        return _unknown("no public pricing catalog entry is configured for this provider")

    safe_metadata = metadata or {}
    resources = _resource_inputs(provider, safe_metadata)
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
        "duration_policy": "measured_wall_time_including_warmup",
        "confidence": "estimated" if complete else "partial",
        "notes": notes,
        "pricing": {
            "retrieved_date": PRICING_RETRIEVED_DATE,
            "source_url": PRICING_SOURCES.get(_pricing_source_provider(provider)),
        },
    }


def _resource_inputs(provider: str, metadata: dict[str, Any]) -> dict[str, float | None]:
    environment = (
        metadata.get("environment") if isinstance(metadata.get("environment"), dict) else {}
    )
    if provider == "e2b":
        return {
            "cpu_count": _float_or_none(metadata.get("cpu_count")) or 2.0,
            "memory_gib": _float_or_none(metadata.get("memory_gib")),
            "storage_gib": _float_or_none(metadata.get("storage_gib")),
        }
    if provider == "modal-daemon":
        return {
            "cpu_count": _float_or_none(environment.get("modal_cpu_count"))
            or _float_or_none(metadata.get("cpu_count")),
            "memory_gib": _float_or_none(environment.get("modal_memory_gib"))
            or _float_or_none(metadata.get("memory_gib")),
            "storage_gib": None,
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


def _pricing_source_provider(provider: str) -> str:
    return "modal" if provider == "modal-daemon" else provider


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
