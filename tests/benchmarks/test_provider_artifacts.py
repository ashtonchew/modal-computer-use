from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from modal_computer_use.benchmarks.artifacts import (
    generate_sanitized_provider_benchmark,
    sanitize_provider_benchmark,
    serialize_provider_benchmark,
    validate_sanitized_provider_benchmark,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_provider_artifact_generation_is_deterministic_and_removes_ephemeral_metadata(
    tmp_path,
) -> None:
    raw_payload = {
        "ok": True,
        "base_url": "https://user:password@example.com/connect?token=secret",
        "metadata": {
            "environment": {
                "modal_run_id": "run-secret",
                "modal_sandbox_id": "sb-secret",
                "browser": "chromium",
            }
        },
        "providers": {},
    }
    raw_bytes = json.dumps(raw_payload, sort_keys=True).encode()
    kwargs = {
        "raw_bytes": raw_bytes,
        "raw_artifact_path": "benchmark-results/candidates/provider.json",
        "harness_commit": "a" * 40,
        "harness_state": "clean",
        "status": "current_reference",
        "scope": "provider-default SDK paths, three measured iterations",
    }

    first = sanitize_provider_benchmark(raw_payload, **kwargs)
    second = sanitize_provider_benchmark(raw_payload, **kwargs)

    assert serialize_provider_benchmark(first) == serialize_provider_benchmark(second)
    serialized = serialize_provider_benchmark(first)
    assert "run-secret" not in serialized
    assert "sb-secret" not in serialized
    assert "password" not in serialized
    assert first["base_url"] is None
    assert first["provenance"]["harness_state"] == "clean"

    raw_path = tmp_path / "raw.json"
    output_path = tmp_path / "sanitized.json"
    raw_path.write_bytes(raw_bytes)
    assert generate_sanitized_provider_benchmark(
        raw_path=raw_path,
        output_path=output_path,
        check=False,
        **{key: value for key, value in kwargs.items() if key != "raw_bytes"},
    )
    assert generate_sanitized_provider_benchmark(
        raw_path=raw_path,
        output_path=output_path,
        check=True,
        **{key: value for key, value in kwargs.items() if key != "raw_bytes"},
    )
    output_path.write_text("{}\n", encoding="utf-8")
    assert not generate_sanitized_provider_benchmark(
        raw_path=raw_path,
        output_path=output_path,
        check=True,
        **{key: value for key, value in kwargs.items() if key != "raw_bytes"},
    )


def test_provider_artifact_rejects_secret_bearing_keys() -> None:
    with pytest.raises(ValueError, match="secret-bearing key"):
        sanitize_provider_benchmark(
            {"base_url": None, "api_key": "secret"},
            raw_bytes=b"{}",
            raw_artifact_path="benchmark-results/raw.json",
            harness_commit="b" * 40,
            harness_state="clean",
            status="historical",
            scope="test",
        )


def test_rejected_provider_artifact_requires_status_reason() -> None:
    with pytest.raises(ValueError, match="requires status_reason"):
        sanitize_provider_benchmark(
            {"base_url": None},
            raw_bytes=b"{}",
            raw_artifact_path="benchmark-results/raw.json",
            harness_commit="b" * 40,
            harness_state="clean",
            status="rejected",
            scope="test",
        )


def test_candidate_provider_artifact_tracks_dirty_harness_diff() -> None:
    payload = sanitize_provider_benchmark(
        {"base_url": None},
        raw_bytes=b"{}",
        raw_artifact_path="benchmark-results/raw.json",
        harness_commit="c" * 40,
        harness_state="dirty",
        harness_diff_sha256="d" * 64,
        status="candidate",
        scope="test",
    )

    assert payload["provenance"]["harness_state"] == "dirty"
    assert payload["provenance"]["harness_diff_sha256"] == "d" * 64


def test_current_reference_rejects_dirty_harness_diff() -> None:
    with pytest.raises(ValueError, match="requires a clean harness"):
        sanitize_provider_benchmark(
            {"base_url": None},
            raw_bytes=b"{}",
            raw_artifact_path="benchmark-results/raw.json",
            harness_commit="c" * 40,
            harness_state="dirty",
            harness_diff_sha256="d" * 64,
            status="current_reference",
            scope="test",
        )

    payload = sanitize_provider_benchmark(
        {"base_url": None},
        raw_bytes=b"{}",
        raw_artifact_path="benchmark-results/raw.json",
        harness_commit="c" * 40,
        harness_state="dirty",
        harness_diff_sha256="d" * 64,
        status="candidate",
        scope="test",
    )
    payload["provenance"]["status"] = "current_reference"
    with pytest.raises(ValueError, match="requires a clean harness"):
        validate_sanitized_provider_benchmark(payload)


def test_tracked_provider_artifact_is_rejected_and_secret_safe() -> None:
    payload = json.loads(
        (REPO_ROOT / "benchmark-data/provider-compare-2026-07-18.json").read_text()
    )

    validate_sanitized_provider_benchmark(payload)
    assert payload["provenance"]["status"] == "rejected"
    assert payload["provenance"]["raw_artifact_tracked"] is False


def test_current_provider_reference_has_complete_samples_and_provenance() -> None:
    payload = json.loads(
        (REPO_ROOT / "benchmark-data/provider-compare-2026-07-24-current.json").read_text()
    )

    validate_sanitized_provider_benchmark(payload)
    assert payload["ok"] is True
    assert payload["provenance"]["status"] == "current_reference"
    assert payload["provenance"]["harness_state"] == "clean"
    assert "harness_diff_sha256" not in payload["provenance"]
    for provider in ("modal-daemon", "daytona", "e2b", "tzafon"):
        result = payload["providers"][provider]
        lifecycle = result["cases"]["product_create_to_first_screenshot"]
        assert result["status"] == "ok"
        assert lifecycle["status"] == "ok"
        assert len(lifecycle["samples_ms"]) == 3
        assert "cleanup" not in result["cases"]
        assert result["verification"]["cursor_position"]["status"] == "ok"
        assert result["verification"]["type_text"]["status"] == "ok"
        assert result["cases"]["cold_create_to_ready"]["deprecated"] is True

    tzafon = payload["providers"]["tzafon"]
    assert tzafon["metadata"]["computer_kind"] == "desktop"
    assert tzafon["metadata"]["resolution_requested"] == "1024x768"
    assert tzafon["metadata"]["resolution"] == "1280x720"
    assert tzafon["metadata"]["requested_resolution_honored"] is False
    assert tzafon["cases"]["move_click"]["last_result"]["provider_action_count"] == 1
    assert (
        tzafon["cases"]["move_click_sequence"]["last_result"]["provider_action_count"]
        == 4
    )


def test_tzafon_competitive_context_matches_provider_reference() -> None:
    reference_path = REPO_ROOT / "benchmark-data/provider-compare-2026-07-24-current.json"
    context_path = (
        REPO_ROOT
        / "benchmark-data/tzafon-competitive-context-us-west-2-2026-07-24.json"
    )
    reference = json.loads(reference_path.read_text())
    context = json.loads(context_path.read_text())

    assert context["status"] == "candidate"
    assert context["provenance"]["source_sha"] == reference["provenance"]["harness_commit"]
    assert context["provenance"]["git_worktree_clean"] is True
    assert context["provenance"]["provider_reference_sha256"] == hashlib.sha256(
        reference_path.read_bytes()
    ).hexdigest()
    assert context["verification"]["provider_default_failure_count"] == 0
    assert context["verification"]["modal_optimized_failure_count"] == 0

    for case, result in context["results"].items():
        for provider, field in (
            ("modal-daemon", "modal_default_p50_ms"),
            ("daytona", "daytona_p50_ms"),
            ("e2b", "e2b_p50_ms"),
            ("tzafon", "tzafon_p50_ms"),
        ):
            expected = reference["providers"][provider]["cases"][case]["summary_ms"]["p50"]
            assert result[field] == expected

    serialized = context_path.read_text()
    assert "modal.host" not in serialized
    assert "sb-" not in serialized
    assert "run_" not in serialized
    assert "api_key" not in serialized.lower()


def test_coordinate_command_provider_reference_is_complete_and_secret_safe() -> None:
    reference_path = (
        REPO_ROOT
        / "benchmark-data/provider-compare-coordinate-command-2026-07-24.json"
    )
    payload = json.loads(reference_path.read_text())

    validate_sanitized_provider_benchmark(payload)
    assert payload["ok"] is True
    assert payload["provenance"]["status"] == "current_reference"
    assert payload["provenance"]["harness_state"] == "clean"
    for provider in ("modal-daemon", "daytona", "e2b", "tzafon"):
        result = payload["providers"][provider]
        assert result["status"] == "ok"
        for case, semantics in (
            ("coordinate_click", "coordinate-click-v1"),
            ("coordinate_click_sequence", "coordinate-click-v1"),
            ("command_nonlogin_shell_echo", "shell-command-echo-v2"),
        ):
            measured = result["cases"][case]
            assert measured["status"] == "ok"
            assert measured["successful_iterations"] == 3
            assert measured["benchmark_semantics"] == semantics
        command = result["cases"]["command_nonlogin_shell_echo"]
        assert command["shell_mode"] == "non_login"
        assert command["command"]["argv"] == ["sh", "-c", "printf 42"]

    serialized = reference_path.read_text()
    for forbidden in (
        "modal.host",
        "sb-",
        "modal_run_id",
        "sandbox_id",
        "api_key",
        "access_token",
    ):
        assert forbidden not in serialized.lower()


def test_tzafon_coordinate_command_context_matches_allowlisted_sources() -> None:
    reference_path = (
        REPO_ROOT
        / "benchmark-data/provider-compare-coordinate-command-2026-07-24.json"
    )
    context_path = (
        REPO_ROOT
        / "benchmark-data/tzafon-coordinate-command-context-2026-07-24.json"
    )
    reference = json.loads(reference_path.read_text())
    context = json.loads(context_path.read_text())

    assert context["status"] == "candidate"
    assert context["provenance"]["source_sha"] == reference["provenance"][
        "harness_commit"
    ]
    assert context["provenance"]["provider_reference_sha256"] == hashlib.sha256(
        reference_path.read_bytes()
    ).hexdigest()
    assert context["provenance"]["provider_raw_artifact_sha256"] == reference[
        "provenance"
    ]["raw_artifact_sha256"]

    for case, providers in context["provider_default_results_ms"].items():
        for provider, result in providers.items():
            summary = reference["providers"][provider]["cases"][case]["summary_ms"]
            assert result == {"p50": summary["p50"], "p95": summary["p95"]}

    for name in ("provider_default", "modal_optimized"):
        config = dict(context["configuration"][name])
        expected = config.pop("safe_configuration_sha256")
        serialized_config = json.dumps(
            config, sort_keys=True, separators=(",", ":")
        ).encode()
        assert hashlib.sha256(serialized_config).hexdigest() == expected

    raw_sources = {
        "modal_optimized": (
            REPO_ROOT
            / context["provenance"]["modal_optimized_raw_artifact"],
            context["provenance"]["modal_optimized_raw_artifact_sha256"],
        ),
        **{
            backend: (
                REPO_ROOT
                / "benchmark-results/candidates"
                / f"subprocess-ab-{backend}-clean-2026-07-24.json",
                arm["raw_artifact_sha256"],
            )
            for backend, arm in context["subprocess_runner_ab"].items()
            if isinstance(arm, dict)
        },
    }
    for raw_path, expected_sha256 in raw_sources.values():
        if raw_path.exists():
            assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == expected_sha256

    optimized_path = raw_sources["modal_optimized"][0]
    if optimized_path.exists():
        optimized = json.loads(optimized_path.read_text())["runs"][
            "modal_colocated_runner"
        ]["surfaces"]["daemon-http"]["cases"]
        for case in (
            "screenshot_full",
            "coordinate_click",
            "coordinate_click_sequence",
            "type_100_chars",
            "type_1000_chars",
        ):
            assert context["modal_optimized_results_ms"][case] == {
                key: optimized[case]["summary_ms"][key] for key in ("p50", "p95")
            }
        command = optimized["command_nonlogin_shell_echo"]
        for label, raw_key in (
            ("total", "summary_ms"),
            ("daemon", "daemon_summary_ms"),
            ("caller_transport_overhead", "overhead_summary_ms"),
        ):
            assert context["modal_optimized_results_ms"][
                "command_nonlogin_shell_echo"
            ][label] == {key: command[raw_key][key] for key in ("p50", "p95")}

    for backend, arm in context["subprocess_runner_ab"].items():
        if not isinstance(arm, dict):
            continue
        raw_path = raw_sources[backend][0]
        if not raw_path.exists():
            continue
        command = json.loads(raw_path.read_text())["runs"]["modal_colocated_runner"][
            "surfaces"
        ]["daemon-http"]["cases"]["command_nonlogin_shell_echo"]
        for label, raw_key in (
            ("total", "summary_ms"),
            ("daemon", "daemon_summary_ms"),
            ("caller_transport_overhead", "overhead_summary_ms"),
        ):
            assert arm[label] == {
                key: command[raw_key][key] for key in ("p50", "p95")
            }

    serialized = context_path.read_text().lower()
    for forbidden in (
        "modal.host",
        "sb-",
        "run_",
        "api_key",
        "access_token",
        "base_url",
        "bearer",
    ):
        assert forbidden not in serialized
