from __future__ import annotations

import copy
import hashlib
import json
import math
import statistics
from pathlib import Path

import pytest

from modal_computer_use import cli
from modal_computer_use.benchmarks.action_frame_report import (
    ACTION_FRAME_SCHEMA_VERSION,
    ActionFrameReportError,
    assemble_action_frame_report,
    render_action_frame_report_markdown,
    validate_action_frame_report,
)

SOURCE_SHA = "a" * 40
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TIMER_BOUNDARY = "caller_before_ordered_action_dispatch_to_validated_immediate_full_frame_bytes"
SCREENSHOT = {"format": "png", "width": 1024, "height": 768, "show_cursor": False}


def _summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    rank = 0.95 * (len(ordered) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    p95 = ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)
    return {"p50": statistics.median(ordered), "p95": p95}


def _arm(provider: str, *, offset: float = 0.0) -> dict[str, object]:
    samples = [10.0 + offset + (index % 4) for index in range(30)]
    return {
        "case_id": "ordered-actions-to-immediate-frame-v1",
        "provider": provider,
        "path": "computer.step" if provider == "modal" else "provider.action_then_screenshot",
        "source_sha": SOURCE_SHA,
        "sdk": {
            "package": "modal-computer-use" if provider == "modal" else provider,
            "version": "2.0.0" if provider == "modal" else "1.0.0",
            "retry_policy": "provider-default",
        },
        "topology": {
            "caller": "application-owned-modal-function"
            if provider == "modal"
            else "external-provider-sdk-caller",
            "requested_region": "us-west-2" if provider == "modal" else "provider-default",
            "observed_region": "us-west-2" if provider == "modal" else "provider-default",
            "placement": "match" if provider == "modal" else "provider-default",
        },
        "resources": {
            "cpu": None if provider == "tzafon" else 1.0,
            "memory_mib": None if provider == "tzafon" else 2048,
            "source": "provider-not-disclosed" if provider == "tzafon" else "benchmark-config",
            "availability": "unavailable" if provider == "tzafon" else "recorded",
        },
        "screenshot": (
            {"format": "jpeg", "width": 1280, "height": 720, "show_cursor": False}
            if provider == "tzafon"
            else copy.deepcopy(SCREENSHOT)
        ),
        "request_shape": {
            "sdk_calls": 1,
            "transport_requests": 1,
            "batching": "single-request",
        },
        "timer_boundary": TIMER_BOUNDARY,
        "warmup_iterations": 2,
        "measured_iterations": len(samples),
        "samples_ms": samples,
        "summary_ms": _summary(samples),
        "failures": [],
        "cleanup": {"status": "clean", "survivors": 0},
        "harness_retries": 0,
        "replacement_samples": 0,
        "input_artifact_digests": [
            {"role": "sanitized-run-input", "sha256": "b" * 64},
        ],
        "status": "measured",
    }


def _artifact() -> dict[str, object]:
    return {
        "schema_version": ACTION_FRAME_SCHEMA_VERSION,
        "benchmark": "external-provider-action-frame",
        "status": "eligible",
        "evidence_date": "2026-08-11",
        "source_sha": SOURCE_SHA,
        "comparison": {
            "case_id": "ordered-actions-to-immediate-frame-v1",
            "scope": "complete-measured-paths",
            "topology": "disclosed-per-arm",
            "claim": "path-level-comparison-only",
        },
        "workload": {
            "case_id": "ordered-actions-to-immediate-frame-v1",
            "action_semantics": "one-left-click-at-512-384-then-immediate-full-frame",
            "action_payload_sha256": hashlib.sha256(b"ordered actions v1").hexdigest(),
            "timer_boundary": TIMER_BOUNDARY,
            "warmup_iterations": 2,
            "measured_iterations": 30,
            "screenshot_policy": "provider-native-full-frame",
        },
        "arms": [_arm("modal"), _arm("daytona", offset=4.0), _arm("tzafon", offset=8.0)],
    }


def _step_source_artifact() -> dict[str, object]:
    samples = [40.0 + (index % 4) for index in range(30)]
    return {
        "benchmark": "computer-step-promotion",
        "status": "complete",
        "failures": [],
        "retries": 0,
        "replacement_samples": 0,
        "cleanup": {"attempted": True, "succeeded": True, "survivors": 0},
        "preregistration": {"warmup_iterations": 2, "samples_per_arm": len(samples)},
        "configuration": {
            "action_scenario": "reset-pointer-then-click-unique-coordinate-v1",
            "action_payload_sha256": (
                "83599900ae670680c7d84271000b03114940c492d935c26b5f0999a281958296"
            ),
            "operation_transport": "computer-step-envelope-v1",
            "image_identity": f"inline-source-{SOURCE_SHA}-config-0123456789abcdef",
            "caller_topology": "one-application-owned-modal-function",
            "requested_placement": {"cloud": "aws", "region": "us-west-2"},
            "observed_placement": {
                "function": {"cloud": "aws", "region": "us-west-2"},
                "target": {"cloud": "aws", "region": "us-west-2"},
            },
            "resources": {
                "function": {"cpu": 1.0, "memory_mib": 2048},
                "sandbox": {"cpu": 1.0, "memory_mib": 2048},
            },
            "screenshot": {
                "format": "png",
                "show_cursor": False,
            },
        },
        "observations": [
            {
                "status": "ok",
                "borrow_count": 1,
                "connection_reused": True,
                "frame_valid": True,
                "timings_ms": {"action_to_frame_ms": value},
            }
            for value in samples
        ],
    }


def _provider_source_artifact() -> dict[str, object]:
    samples = [50.0 + (index % 4) for index in range(30)]
    case = {
        "case_id": "ordered-actions-to-immediate-frame-v1",
        "path": "provider.action_then_screenshot",
        "source_sha": SOURCE_SHA,
        "action_semantics": "one-left-click-at-512-384-then-immediate-full-frame",
        "action_payload_sha256": (
            "83599900ae670680c7d84271000b03114940c492d935c26b5f0999a281958296"
        ),
        "timer_boundary": (
            "caller_before_ordered_action_dispatch_to_validated_immediate_full_frame_bytes"
        ),
        "screenshot": {"format": "jpeg", "width": 1280, "height": 720, "show_cursor": False},
        "request_shape": {
            "sdk_calls": 2,
            "transport_requests": 2,
            "batching": "sequential-requests",
        },
        "harness_retries": 0,
        "replacement_samples": 0,
        "iterations": len(samples),
        "successful_iterations": len(samples),
        "samples_ms": samples,
        "failures": [],
        "status": "ok",
    }
    return {
        "source_sha": SOURCE_SHA,
        "benchmark": "provider-compare",
        "ok": True,
        "status": "ok",
        "iterations": len(samples),
        "warmup_iterations": 2,
        "providers": {
            "tzafon": {
                "source_sha": SOURCE_SHA,
                "status": "ok",
                "failures": [],
                "metadata": {
                    "sdk_package": "tzafon",
                    "sdk_version": "2.44.1",
                    "sdk_retry_policy": "provider_default",
                    "topology": {
                        "caller": "external-provider-sdk-caller",
                        "requested_region": "provider-default",
                        "observed_region": "provider-default",
                        "placement": "provider-default",
                    },
                },
                "cases": {"action_to_immediate_frame": case},
            }
        },
    }


def _tracked_provider_source_artifact() -> dict[str, object]:
    raw = _provider_source_artifact()
    provider = raw["providers"]["tzafon"]  # type: ignore[index]
    case = provider["cases"]["action_to_immediate_frame"]  # type: ignore[index]
    case["screenshot"]["show_cursor"] = None  # type: ignore[index]
    del case["failures"]  # type: ignore[index]
    return {
        "schema_version": 1,
        "benchmark": "external-provider-action-frame-run",
        "status": "eligible",
        "evidence_date": "2026-08-11",
        "source_sha": SOURCE_SHA,
        "case_id": "ordered-actions-to-immediate-frame-v1",
        "action_semantics": "one-left-click-at-512-384-then-immediate-full-frame",
        "timer_boundary": TIMER_BOUNDARY,
        "warmup_iterations": 2,
        "measured_iterations": 30,
        "providers": {
            "tzafon": {
                "source_sha": SOURCE_SHA,
                "status": "ok",
                "failures": [],
                "metadata": provider["metadata"],  # type: ignore[index]
                "case": case,
                "cleanup": {"status": "clean", "survivors": 0},
                "inventory": {"before_count": 0, "after_count": 0},
            }
        },
        "cleanup": {
            "source_sha": SOURCE_SHA,
            "providers": {"tzafon": {"status": "clean", "survivors": 0}},
        },
        "failures": [],
    }


def _cleanup_verification() -> dict[str, object]:
    return {
        "source_sha": SOURCE_SHA,
        "providers": {
            "modal-daemon": {"status": "clean", "survivors": 0},
            "tzafon": {"status": "clean", "survivors": 0},
        },
    }


def test_valid_action_frame_artifact_validates() -> None:
    validate_action_frame_report(_artifact())


def test_tracked_action_frame_report_recomputes_and_renders() -> None:
    artifact_path = PROJECT_ROOT / "benchmark-data/external-provider-action-frame-2026-08-11.json"
    report_path = PROJECT_ROOT / "docs/benchmark-results-2026-08-11-provider-action-frame.md"
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))

    validate_action_frame_report(payload)

    assert payload["status"] == "eligible"
    assert [arm["provider"] for arm in payload["arms"]] == [
        "modal-daemon",
        "daytona",
        "e2b",
        "tzafon",
    ]
    assert all(arm["measured_iterations"] == 100 for arm in payload["arms"])
    assert report_path.read_text(encoding="utf-8") == render_action_frame_report_markdown(payload)


def test_assembler_binds_fresh_step_provider_and_cleanup_inputs() -> None:
    result = assemble_action_frame_report(
        step_artifact=_step_source_artifact(),
        provider_artifact=_provider_source_artifact(),
        cleanup_verification=_cleanup_verification(),
        source_sha=SOURCE_SHA,
        evidence_date="2026-08-11",
        input_artifact_digests={
            "step_candidate": "c" * 64,
            "provider_compare": "d" * 64,
            "cleanup_verification": "e" * 64,
        },
    )

    assert result["status"] == "eligible"
    assert [arm["provider"] for arm in result["arms"]] == ["modal-daemon", "tzafon"]
    assert result["arms"][0]["summary_ms"]["p50"] == 41.0
    assert result["arms"][0]["screenshot"] == {
        "format": "png",
        "width": None,
        "height": None,
        "show_cursor": False,
    }
    assert result["arms"][0]["sdk"]["package"] == "modal-computer-use"
    assert result["arms"][1]["resources"] == {
        "cpu": None,
        "memory_mib": None,
        "source": "provider-not-disclosed",
        "availability": "unavailable",
    }
    assert len(result["arms"][0]["input_artifact_digests"]) == 3


def test_assembler_accepts_sanitized_runner_artifact_with_unknown_cursor_policy() -> None:
    result = assemble_action_frame_report(
        step_artifact=_step_source_artifact(),
        provider_artifact=_tracked_provider_source_artifact(),
        cleanup_verification=_cleanup_verification(),
        source_sha=SOURCE_SHA,
        evidence_date="2026-08-11",
        input_artifact_digests={
            "step_candidate": "c" * 64,
            "provider_compare": "d" * 64,
            "cleanup_verification": "e" * 64,
        },
    )

    assert result["arms"][1]["screenshot"]["show_cursor"] is None
    markdown = render_action_frame_report_markdown(result)
    assert "cursor=unknown" in markdown


@pytest.mark.parametrize("field", ["source_sha", "cleanup_verification", "provider_artifact"])
def test_assembler_fails_closed_on_source_or_cleanup_mismatch(field: str) -> None:
    step = _step_source_artifact()
    provider = _provider_source_artifact()
    cleanup = _cleanup_verification()
    if field == "source_sha":
        step["source_sha"] = "f" * 40
    elif field == "provider_artifact":
        provider["providers"]["tzafon"]["source_sha"] = "f" * 40  # type: ignore[index]
    else:
        cleanup["providers"]["tzafon"]["survivors"] = 1  # type: ignore[index]

    with pytest.raises(ActionFrameReportError):
        assemble_action_frame_report(
            step_artifact=step,
            provider_artifact=provider,
            cleanup_verification=cleanup,
            source_sha=SOURCE_SHA,
            evidence_date="2026-08-11",
            input_artifact_digests={
                "step_candidate": "c" * 64,
                "provider_compare": "d" * 64,
                "cleanup_verification": "e" * 64,
            },
        )


def test_validator_recomputes_summary_from_samples() -> None:
    artifact = _artifact()
    artifact["arms"][0]["summary_ms"]["p50"] = 999  # type: ignore[index]

    with pytest.raises(ActionFrameReportError, match="p50"):
        validate_action_frame_report(artifact)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_sha", "short", "source_sha"),
        ("timer_boundary", "different", "timer boundary"),
        (
            "screenshot",
            {"format": "bmp", "width": 800, "height": 600, "show_cursor": False},
            "screenshot",
        ),
        ("harness_retries", 1, "retries"),
        ("replacement_samples", 1, "replacement"),
    ],
)
def test_validator_rejects_mismatched_evidence(field: str, value: object, message: str) -> None:
    artifact = _artifact()
    if field in {"source_sha", "timer_boundary"} or field == "screenshot":
        artifact["arms"][1][field] = value  # type: ignore[index]
    else:
        artifact["arms"][1][field] = value  # type: ignore[index]

    with pytest.raises(ActionFrameReportError, match=message):
        validate_action_frame_report(artifact)


@pytest.mark.parametrize(
    "bad_key", ["run_id", "sandbox_id", "base_url", "typed_text", "screenshot_bytes"]
)
def test_validator_rejects_secret_or_ephemeral_fields(bad_key: str) -> None:
    artifact = _artifact()
    artifact["arms"][0][bad_key] = "secret"  # type: ignore[index]

    with pytest.raises(ActionFrameReportError):
        validate_action_frame_report(artifact)


def test_validator_rejects_raw_failure_details() -> None:
    artifact = _artifact()
    artifact["arms"][0]["failures"] = [  # type: ignore[index]
        {"phase": "measure", "category": "timeout", "message": "raw"}
    ]

    with pytest.raises(ActionFrameReportError, match="forbidden"):
        validate_action_frame_report(artifact)


def test_validator_rejects_unknown_fields_and_incomplete_arm() -> None:
    artifact = _artifact()
    artifact["arms"][0]["unexpected"] = True  # type: ignore[index]
    del artifact["arms"][1]["samples_ms"]  # type: ignore[index]

    with pytest.raises(ActionFrameReportError, match="unknown field"):
        validate_action_frame_report(artifact)


def test_markdown_renderer_is_deterministic_and_mobile_friendly() -> None:
    artifact = _artifact()

    first = render_action_frame_report_markdown(artifact)
    second = render_action_frame_report_markdown(copy.deepcopy(artifact))

    assert first == second
    assert "| Case | Path | p50 (ms) | p95 (ms) | n | Status |" in first
    assert "| ordered-actions-to-immediate-frame-v1 | modal / computer.step |" in first
    assert "All arms use the same action case and timer boundary" in first
    assert "winner" not in first.lower()
    assert "Not disclosed" in first
    assert "run_id" not in first
    assert "b" * 64 in first


def test_cli_validates_and_renders_offline_artifact(tmp_path, capsys) -> None:
    artifact_path = tmp_path / "action-frame.json"
    artifact_path.write_text(json.dumps(_artifact()), encoding="utf-8")

    exit_code = cli.main(["benchmark", "action-frame-report", str(artifact_path)])

    assert exit_code == 0
    assert "| Case | Path | p50 (ms) | p95 (ms) | n | Status |" in capsys.readouterr().out


def test_cli_assembles_and_renders_fresh_inputs(tmp_path, capsys) -> None:
    step_path = tmp_path / "step.json"
    provider_path = tmp_path / "provider.json"
    cleanup_path = tmp_path / "cleanup.json"
    step_path.write_text(json.dumps(_step_source_artifact()), encoding="utf-8")
    provider_path.write_text(json.dumps(_provider_source_artifact()), encoding="utf-8")
    cleanup_path.write_text(json.dumps(_cleanup_verification()), encoding="utf-8")

    exit_code = cli.main(
        [
            "benchmark",
            "action-frame-report",
            "--step-artifact",
            str(step_path),
            "--provider-artifact",
            str(provider_path),
            "--cleanup-verification",
            str(cleanup_path),
            "--source-sha",
            SOURCE_SHA,
            "--evidence-date",
            "2026-08-11",
            "--format",
            "json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["status"] == "eligible"
    assert [arm["provider"] for arm in output["arms"]] == ["modal-daemon", "tzafon"]
