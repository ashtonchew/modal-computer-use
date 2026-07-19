from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

import modal_computer_use.benchmarks.billing as benchmark_billing
from modal_computer_use.benchmarks.costs import estimate_provider_cost, estimate_surface_cost
from modal_computer_use.errors import ModalNotInstalledError


def test_modal_cost_estimate_requires_explicit_resources() -> None:
    partial = estimate_surface_cost(
        "daemon-http",
        surface_status="ok",
        runtime_seconds=10.0,
        metadata={"environment": {"modal_cold_create_to_ready_ms": 10000}},
    )
    estimated = estimate_surface_cost(
        "daemon-http",
        surface_status="ok",
        runtime_seconds=10.0,
        metadata={
            "environment": {
                "modal_cpu_count": 0.125,
                "modal_memory_gib": 0.125,
            }
        },
    )

    assert partial["status"] == "partial"
    assert "cpu allocation was unavailable" in partial["notes"]
    assert "memory allocation was unavailable" in partial["notes"]
    assert estimated["status"] == "estimated"
    assert estimated["inputs"]["cpu_count"] == 0.125
    assert estimated["inputs"]["memory_gib"] == 0.125
    assert estimated["total"]["amount"] == pytest.approx(
        sum(component["amount"] for component in estimated["components"])
    )
    assert estimated["duration_policy"] == "measured_wall_time_including_warmup"


def test_provider_cost_estimate_uses_provider_resources_and_runtime() -> None:
    estimate = estimate_provider_cost(
        "daytona",
        provider_status="ok",
        runtime_seconds=12.0,
        metadata={"cpu_count": 1, "memory_gib": 1, "storage_gib": 3},
    )

    assert estimate["status"] == "estimated"
    assert estimate["inputs"] == {
        "duration_seconds": 12.0,
        "cpu_count": 1.0,
        "memory_gib": 1.0,
        "storage_gib": 3.0,
    }
    assert estimate["pricing"]["source_url"] == "https://www.daytona.io/pricing"
    assert (
        estimate["duration_policy"]
        == "measured_billable_resource_lifetime_including_cleanup"
    )


def test_modal_billing_reconciliation_filters_and_sums_tagged_rows() -> None:
    request = benchmark_billing.modal_billing_reconciliation_request(
        start=datetime(2026, 5, 13, 1, 0, tzinfo=UTC),
        end=datetime(2026, 5, 13, 2, 0, tzinfo=UTC),
        required_tags={
            "benchmark": "sdk-surfaces",
            "benchmark_run_id": "sdk_surface_test",
            "surface": "daemon-http",
        },
    )

    def report_loader(start, end, resolution, tag_names):
        assert start == datetime(2026, 5, 13, 1, 0, tzinfo=UTC)
        assert end == datetime(2026, 5, 13, 2, 0, tzinfo=UTC)
        assert resolution == "h"
        assert tag_names == ["benchmark", "benchmark_run_id", "surface"]
        return [
            {
                "cost": Decimal("0.12"),
                "cost_by_resource": {
                    "cpu": Decimal("0.08"),
                    "memory": Decimal("0.04"),
                },
                "interval_start": datetime(2026, 5, 13, 1, 0, tzinfo=UTC),
                "tags": {
                    "benchmark": "sdk-surfaces",
                    "benchmark_run_id": "sdk_surface_test",
                    "surface": "daemon-http",
                },
                "object_id": "secret-object-id",
            },
            {
                "cost": Decimal("9.99"),
                "interval_start": datetime(2026, 5, 13, 1, 0, tzinfo=UTC),
                "tags": {
                    "benchmark": "sdk-surfaces",
                    "benchmark_run_id": "other",
                    "surface": "daemon-http",
                },
            },
        ]

    result = benchmark_billing.reconcile_modal_billing(request, report_loader=report_loader)
    serialized = json.dumps(result)

    assert result["status"] == "matched"
    assert result["matched_row_count"] == 1
    assert result["row_count"] == 2
    assert result["total"]["amount"] == pytest.approx(0.12)
    assert result["cost_by_resource"] == {"cpu": 0.08, "memory": 0.04}
    assert "secret-object-id" not in serialized


def test_modal_billing_request_can_scope_to_environment() -> None:
    request = benchmark_billing.modal_billing_reconciliation_request(
        start=datetime(2026, 5, 13, 1, 0, tzinfo=UTC),
        end=datetime(2026, 5, 13, 2, 0, tzinfo=UTC),
        required_tags={"benchmark_run_id": "sdk_surface_test"},
        environment_name="prod",
    )

    result = benchmark_billing.reconcile_modal_billing(
        request,
        report_loader=lambda *args: [],
    )

    assert result["source"] == "modal.Environment.billing.report"
    assert result["environment_name"] == "prod"

def test_modal_billing_reconciliation_handles_unavailable_and_pending() -> None:
    request = benchmark_billing.modal_billing_reconciliation_request(
        start=datetime(2026, 5, 13, 1, 0, tzinfo=UTC),
        end=datetime(2026, 5, 13, 2, 0, tzinfo=UTC),
        required_tags={"benchmark_run_id": "sdk_surface_test"},
    )

    unavailable = benchmark_billing.reconcile_modal_billing(
        request,
        report_loader=lambda *args: (_ for _ in ()).throw(ImportError("no modal")),
    )
    pending = benchmark_billing.reconcile_modal_billing(
        request,
        report_loader=lambda *args: [],
    )

    assert unavailable["status"] == "unavailable"
    assert pending["status"] == "not_available_yet"

def test_modal_billing_reconciliation_distinguishes_tag_mismatch() -> None:
    request = benchmark_billing.modal_billing_reconciliation_request(
        start=datetime(2026, 5, 13, 1, 15, tzinfo=UTC),
        end=datetime(2026, 5, 13, 1, 45, tzinfo=UTC),
        required_tags={"benchmark_run_id": "sdk_surface_test"},
    )

    result = benchmark_billing.reconcile_modal_billing(
        request,
        report_loader=lambda *args: [
            {
                "cost": Decimal("0.12"),
                "interval_start": datetime(2026, 5, 13, 1, 0, tzinfo=UTC),
                "tags": {"surface": "daemon-http"},
            }
        ],
    )

    assert result["status"] == "no_matching_tags"
    assert result["row_count"] == 1
    assert "full intervals" in " ".join(result["notes"])

def test_modal_billing_reconciliation_treats_modal_missing_as_unavailable() -> None:
    request = benchmark_billing.modal_billing_reconciliation_request(
        start=datetime(2026, 5, 13, 1, 0, tzinfo=UTC),
        end=datetime(2026, 5, 13, 2, 0, tzinfo=UTC),
        required_tags={"benchmark_run_id": "sdk_surface_test"},
    )

    result = benchmark_billing.reconcile_modal_billing(
        request,
        report_loader=lambda *args: (_ for _ in ()).throw(
            ModalNotInstalledError("modal not installed")
        ),
    )

    assert result["status"] == "unavailable"
