from __future__ import annotations

from copy import deepcopy

import pytest

from modal_computer_use.benchmarks.x11_shm_screenshot import (
    evaluate_x11_shm_screenshot_promotion,
    validate_x11_shm_screenshot_artifact,
)


def _artifact() -> dict:
    schedule = []
    for index in range(100):
        for position, arm in enumerate(("mss", "x11-shm")):
            schedule.append(
                {
                    "sequence": len(schedule),
                    "sample_index": index,
                    "position": position,
                    "arm": arm,
                }
            )

    def observations(
        arm: str,
        *,
        complete_ms: float,
        daemon_ms: float,
        payload_bytes: int,
    ) -> list[dict]:
        return [
            {
                "sample_index": index,
                "status": "ok",
                "complete_sdk_ms": complete_ms,
                "daemon_total_ms": daemon_ms,
                "capture_ms": 1.0 if arm == "mss" else None,
                "encode_ms": 4.0 if arm == "mss" else None,
                "x11_shm_capture_encode_ms": 2.0 if arm == "x11-shm" else None,
                "hash_ms": 0.2,
                "payload_bytes": payload_bytes,
                "capture_backend": arm,
                "decoded_pixel_parity": True,
                "metadata_parity": True,
            }
            for index in range(100)
        ]

    artifact = {
        "schema_version": 1,
        "benchmark": "x11-shm-screenshot-promotion",
        "status": "complete",
        "public_call": "await computer.screenshots.full()",
        "preregistration": {
            "samples_per_arm": 100,
            "warmup_iterations": 10,
            "schedule_seed": 20260808,
            "bootstrap_seed": 20260808,
            "bootstrap_resamples": 500,
            "gates": {
                "minimum_p50_improvement_percent": 20.0,
                "maximum_p95_regression_percent": 5.0,
                "maximum_payload_growth_percent": 10.0,
                "minimum_daemon_saving_ms": 5.0,
            },
            "operational_ceilings": {
                "maximum_readiness_p95_regression_percent": 5.0,
                "maximum_concurrency_p95_regression_percent": 5.0,
                "maximum_rss_growth_bytes": 16 * 1024 * 1024,
                "maximum_fd_delta": 0,
                "maximum_mapping_delta": 0,
            },
        },
        "configuration": {
            "source_revision": "a" * 40,
            "worktree_clean": True,
            "x11_shm_source_sha256": "b" * 64,
            "cargo_lock_sha256": "c" * 64,
            "rust_toolchain": "rustc 1.91.0",
            "python_version": "3.12.11",
            "target": "x86_64-unknown-linux-gnu",
            "image_identity": "modal-computer-use-a" + "1" * 39,
            "image_object_id": "im-test-image",
            "native_builds": {
                arm: {
                    "backend": "x11-shm",
                    "codec": "png-deflate-level1-fixed-up",
                    "module_sha256": "d" * 64,
                    "image_object_id": "im-test-image",
                    "machine": "x86_64",
                }
                for arm in ("mss", "x11-shm")
            },
            "requested_placement": {"cloud": "aws", "region": "us-west-2"},
            "observed_placement": {
                "runner": {"cloud": "aws", "region": "us-west-2"},
                "targets": {
                    arm: {"cloud": "aws", "region": "us-west-2"}
                    for arm in ("mss", "x11-shm")
                },
            },
            "resources": {"cpu": 1.0, "memory_mib": 2048},
            "observed_resources": {
                arm: {"cpu": 1.0, "memory_bytes": 2048 * 1024 * 1024}
                for arm in ("mss", "x11-shm")
            },
            "browser": "chromium",
            "browser_launch_args": [
                "--kiosk",
                "--window-position=0,0",
                "--window-size=1024,768",
                "--force-device-scale-factor=1",
                "--no-first-run",
                "--disable-session-crashed-bubble",
                "--disable-infobars",
            ],
            "browser_gpu_mode": "off",
            "display": {"width": 1024, "height": 768, "depth": 24},
            "screenshot": {
                "format": "png",
                "lossless": True,
                "show_cursor": False,
                "scale": 1.0,
                "storage": "inline",
            },
            "ingress": "attested-tunnel",
            "http_version": "1.1",
            "connection_reuse": "one-pooled-async-client-per-arm",
        },
        "schedule": schedule,
        "arms": {
            "mss": {
                "requested_source": "mss",
                "expected_backend": "mss",
                "observations": observations(
                    "mss", complete_ms=30.0, daemon_ms=16.0, payload_bytes=100_000
                ),
            },
            "x11-shm": {
                "requested_source": "auto",
                "expected_backend": "x11-shm",
                "observations": observations(
                    "x11-shm", complete_ms=20.0, daemon_ms=8.0, payload_bytes=105_000
                ),
            },
        },
        "fallback_counts": {"mss": 0, "x11-shm": 0},
        "replacement_samples": 0,
        "retries": 0,
        "failures": [],
        "cleanup": {"succeeded": True, "remaining_sandboxes": 0},
        "operational_gates": {
            "chromium_fixture": True,
            "failure_matrix": True,
            "concurrency_matrix": True,
            "readiness_parity": True,
            "x_server_restart": True,
            "bounded_x_server_failure": True,
            "region_parity": True,
            "captures": 10_000,
            "full_captures": 5_000,
            "region_captures": 5_000,
            "fd_delta": 0,
            "mapping_delta": 0,
            "rss_growth_bytes": 0,
            "peak_rss_growth_bytes": 0,
            "cleanup_succeeded": True,
        },
        "operational_details": {
            "concurrency": {
                "passed": True,
                "maximum_p95_regression_percent": 5.0,
                "arms": {
                    arm: {
                        "passed": True,
                        "source": arm,
                        "levels": [
                            {
                                "concurrency": level,
                                "trials": 5,
                                "captures_per_trial": level,
                                "elapsed_p50_ms": 2.0 if arm == "mss" else 1.0,
                                "elapsed_p95_ms": 2.0 if arm == "mss" else 1.0,
                                "capture_backend": arm,
                            }
                            for level in (1, 2, 4, 8)
                        ],
                    }
                    for arm in ("mss", "x11-shm")
                },
            },
            "readiness": {
                "passed": True,
                "maximum_p95_regression_percent": 5.0,
                "arms": {
                    arm: {
                        "passed": True,
                        "source": arm,
                        "samples": 20,
                        "startup_p50_ms": 100.0,
                        "startup_p95_ms": 100.0,
                        "capture_backend": arm,
                    }
                    for arm in ("mss", "x11-shm")
                },
            },
            "failure_matrix": {
                "passed": True,
                "checks": {
                    key: True
                    for key in (
                        "close_idempotent",
                        "closed_capture_rejected",
                        "constructor_geometry_failure",
                        "invalid_region_rejected",
                        "attach_failure_falls_back_once",
                        "encode_failure_falls_back_once",
                        "invalid_result_falls_back_once",
                        "extension_load_failure_selects_mss",
                        "close_failure_reported",
                    )
                },
            },
            "soak": {
                "passed": True,
                "captures": 10_000,
                "full_captures": 5_000,
                "region_captures": 5_000,
                "fd_delta": 0,
                "mapping_delta": 0,
                "rss_growth_bytes": 0,
                "peak_rss_growth_bytes": 0,
            },
            "x_server_restart": {
                "passed": True,
                "ready_after_restart": True,
                "backend_before": "x11-shm",
                "backend_after": "x11-shm",
            },
            "x_server_timeout": {
                "passed": True,
                "failed_bounded": True,
                "public_error_type": "DaemonHTTPError",
                "public_error_code": "internal_error",
                "public_error_detail_type": "ScreenshotCaptureTimedOut",
                "no_fallback_observed": True,
                "constructor_bounded": True,
                "constructor_error_type": "ScreenshotCaptureTimedOut",
                "elapsed_ms": 600.0,
                "constructor_elapsed_ms": 600.0,
                "backend_after_restart": "x11-shm",
            },
            "region_parity": {
                "passed": True,
                "case_count": 4,
                "decoded_pixel_and_metadata_parity": True,
                "arms": {
                    arm: [
                        {
                            "region": {"x": index, "y": index, "width": 1, "height": 1},
                            "pixels_sha256": "e" * 64,
                            "width": 1,
                            "height": 1,
                            "cursor_visible": False,
                            "cursor_position_is_null": True,
                            "coordinate_space": {
                                "desktop_width": 1024,
                                "desktop_height": 768,
                                "image_width": 1,
                                "image_height": 1,
                                "scale_x": 1.0,
                                "scale_y": 1.0,
                                "source_region": {
                                    "x": index,
                                    "y": index,
                                    "width": 1,
                                    "height": 1,
                                },
                            },
                            "capture_backend": arm,
                        }
                        for index in range(4)
                    ]
                    for arm in ("mss", "x11-shm")
                },
            },
            "terminal_cleanup": {
                "succeeded": True,
                "survivors_before_sweep": 0,
                "remaining_sandboxes": 0,
                "cleanup_error_types": [],
            },
        },
    }
    artifact["promotion"] = evaluate_x11_shm_screenshot_promotion(artifact)
    return artifact


def test_publishable_artifact_passes_fixed_promotion_gates() -> None:
    artifact = _artifact()
    validate_x11_shm_screenshot_artifact(artifact)
    decision = evaluate_x11_shm_screenshot_promotion(artifact)
    assert decision["decision"] == "promote"
    assert decision["eligible"] is True
    assert decision["metrics"]["complete_sdk_ms"]["p50_improvement_percent"] > 20
    assert decision["metrics"]["daemon_total_ms"]["absolute_saving_ms"] >= 5


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.__setitem__("retries", 1), "retries"),
        (
            lambda payload: payload["configuration"].__setitem__(
                "browser", "synthetic-x11"
            ),
            "Chromium",
        ),
        (
            lambda payload: payload["arms"]["x11-shm"]["observations"][0].__setitem__(
                "capture_backend", "mss-fallback"
            ),
            "backend",
        ),
        (
            lambda payload: payload["operational_gates"].__setitem__("fd_delta", 1),
            "resource",
        ),
        (
            lambda payload: payload["operational_details"]["readiness"]["arms"][
                "x11-shm"
            ].__setitem__("startup_p95_ms", 110.0),
            "readiness",
        ),
        (
            lambda payload: payload.__setitem__("app_url", "https://modal.invalid/private"),
            "unsafe",
        ),
    ],
)
def test_validator_rejects_non_publishable_or_unsafe_evidence(mutation, message: str) -> None:
    artifact = _artifact()
    mutation(artifact)
    with pytest.raises(ValueError, match=message):
        validate_x11_shm_screenshot_artifact(artifact)


def test_gate_rejects_payload_growth_without_weakening_threshold() -> None:
    artifact = deepcopy(_artifact())
    for observation in artifact["arms"]["x11-shm"]["observations"]:
        observation["payload_bytes"] = 111_000
    decision = evaluate_x11_shm_screenshot_promotion(artifact)
    assert decision["eligible"] is False
    assert decision["decision"] == "reject"
    assert any("payload" in reason for reason in decision["reasons"])


def test_gate_rejects_daemon_saving_below_five_milliseconds() -> None:
    artifact = deepcopy(_artifact())
    for observation in artifact["arms"]["x11-shm"]["observations"]:
        observation["daemon_total_ms"] = 11.1
    decision = evaluate_x11_shm_screenshot_promotion(artifact)
    assert decision["eligible"] is False
    assert any("daemon" in reason for reason in decision["reasons"])


def test_gate_retains_and_rejects_readiness_regression() -> None:
    artifact = _artifact()
    artifact["operational_gates"]["readiness_parity"] = False
    artifact["operational_details"]["readiness"]["passed"] = False
    artifact["operational_details"]["readiness"]["arms"]["x11-shm"][
        "startup_p95_ms"
    ] = 110.0
    artifact["promotion"] = evaluate_x11_shm_screenshot_promotion(artifact)

    validate_x11_shm_screenshot_artifact(artifact)

    assert artifact["promotion"]["eligible"] is False
    assert artifact["promotion"]["decision"] == "reject"
    assert any("readiness" in reason for reason in artifact["promotion"]["reasons"])


def test_preregistered_thresholds_are_immutable() -> None:
    artifact = _artifact()
    artifact["preregistration"]["gates"]["minimum_daemon_saving_ms"] = 4.99
    with pytest.raises(ValueError, match="fixed promotion gates"):
        validate_x11_shm_screenshot_artifact(artifact)


def test_validator_rejects_a_stale_retained_decision() -> None:
    artifact = _artifact()
    artifact["promotion"]["decision"] = "reject"

    with pytest.raises(ValueError, match="decision"):
        validate_x11_shm_screenshot_artifact(artifact)
