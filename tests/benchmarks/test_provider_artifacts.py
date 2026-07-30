from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path

import pytest

from modal_computer_use.benchmarks.artifacts import (
    generate_sanitized_provider_benchmark,
    sanitize_provider_benchmark,
    serialize_provider_benchmark,
    validate_sanitized_provider_benchmark,
)
from modal_computer_use.benchmarks.measurement import _percentile

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


def test_modal_subprocess_runner_ab_2026_07_30_is_pinned_and_secret_safe() -> None:
    artifact_path = (
        REPO_ROOT / "benchmark-data/modal-subprocess-runner-ab-2026-07-30.json"
    )
    artifact = json.loads(artifact_path.read_text())

    assert artifact["status"] == "candidate"
    assert artifact["provenance"]["source_sha"] == (
        "7c8e6810ee7fc1da4046590525b0e8d48e1fd919"
    )
    assert artifact["provenance"]["harness_state"] == "clean"
    assert artifact["provenance"]["git_worktree_clean"] is True
    assert artifact["provenance"]["raw_artifacts_tracked"] is False
    assert artifact["semantics"]["command_nonlogin_shell_echo"] == {
        "benchmark_semantics": "shell-command-echo-v2",
        "shell_mode": "non_login",
        "argv": ["sh", "-c", "printf '42\\n'"],
        "timeout_seconds": 30,
        "transport_shape": "argv",
    }

    configuration = dict(artifact["configuration"])
    expected_configuration_sha256 = configuration.pop("safe_configuration_sha256")
    serialized_configuration = json.dumps(
        configuration, sort_keys=True, separators=(",", ":")
    ).encode()
    assert (
        hashlib.sha256(serialized_configuration).hexdigest()
        == expected_configuration_sha256
    )
    assert configuration["requested"]["modal_ingress"] == "attested-tunnel"
    assert configuration["requested"]["runner_path"] == "inherited"
    assert configuration["requested"]["runner_only"] is True
    assert configuration["requested"]["modal_cpu"] == 4.0
    assert configuration["requested"]["modal_memory_mib"] == 8192
    assert configuration["requested"]["runner_cpu"] == 4.0
    assert configuration["requested"]["runner_memory_mib"] == 8192
    assert configuration["observed"]["canonical_surface_name"] == (
        "modal-daemon-attested-tunnel"
    )
    assert configuration["observed"]["external_caller_included"] is False
    assert configuration["observed"]["input_backend"] == "xtest"

    block = artifact["subprocess_runner_ab"]
    assert block["metric"] == "modal-colocated shell-command-echo-v2 milliseconds"
    assert block["case"] == "command_nonlogin_shell_echo"
    assert block["iterations_per_arm"] == 30
    assert block["warmup_iterations"] == 1
    assert block["p50_method"] == "statistics.median"
    assert block["p95_method"] == (
        "linear interpolation on sorted values at rank 0.95*(n-1)"
    )
    assert artifact["verification"]["subprocess_runner_ab_failures"] == 0
    assert (
        artifact["verification"]["subprocess_runner_ab_successful_iterations_per_arm"]
        == 30
    )

    expected_arms = {
        "asyncio": {
            "raw_artifact_sha256": (
                "e7c13b02d8d80691e367899890506364ceab0278f294c65b05c9f5cc5db3d3a6"
            ),
            "total": {"p50": 54.90595200000037, "p95": 219.92788569999882},
            "daemon": {"p50": 49.534644500001335, "p95": 214.753257949997},
            "caller_transport_overhead": {
                "p50": 5.222455500000223,
                "p95": 5.809947500000322,
            },
        },
        "threaded": {
            "raw_artifact_sha256": (
                "ecf808416f5e2a148ad2fc5fa3344a2c8a0e418d8837613d88a2242bc716529c"
            ),
            "total": {"p50": 10.617587999999678, "p95": 13.161663449999136},
            "daemon": {"p50": 6.7867264999996735, "p95": 8.99796054999857},
            "caller_transport_overhead": {
                "p50": 3.809115500000182,
                "p95": 4.339098949999708,
            },
        },
        "isolated-asyncio": {
            "raw_artifact_sha256": (
                "c87c1f19527ee264726c1c41ac8bc9300fb8e6adef5f88ed8b4c9590d19dfd56"
            ),
            "total": {"p50": 7.584464999999874, "p95": 8.67549800000038},
            "daemon": {"p50": 5.331084500001637, "p95": 5.843138550000759},
            "caller_transport_overhead": {
                "p50": 2.286975499999677,
                "p95": 2.8009997999999916,
            },
        },
    }
    for backend, expected in expected_arms.items():
        arm = block[backend]
        assert arm["raw_artifact"] == (
            f"benchmark-results/subprocess-runner-ab-2026-07-30/{backend}.json"
        )
        assert arm["raw_artifact_sha256"] == expected["raw_artifact_sha256"]
        assert arm["successful_iterations"] == 30
        assert arm["failures"] == 0
        for label in ("total", "daemon", "caller_transport_overhead"):
            assert arm[label] == expected[label]

    # The ordering claim the artifact exists to support.
    assert (
        block["isolated-asyncio"]["total"]["p50"]
        < block["threaded"]["total"]["p50"]
        < block["asyncio"]["total"]["p50"]
    )

    # Recompute from the raw arms when they are present in an ignored working tree.
    for backend, arm in expected_arms.items():
        raw_path = REPO_ROOT / block[backend]["raw_artifact"]
        if not raw_path.exists():
            continue
        raw_bytes = raw_path.read_bytes()
        assert hashlib.sha256(raw_bytes).hexdigest() == arm["raw_artifact_sha256"]
        run = json.loads(raw_bytes)["runs"]["modal_colocated_runner"]
        assert run["warmup_iterations"] == block["warmup_iterations"]
        case = run["surfaces"]["daemon-http"]["cases"][block["case"]]
        assert case["iterations"] == block["iterations_per_arm"]
        assert case["failures"] == []
        for label, samples_key in (
            ("total", "samples_ms"),
            ("daemon", "daemon_samples_ms"),
            ("caller_transport_overhead", "overhead_samples_ms"),
        ):
            samples = sorted(case[samples_key])
            assert len(samples) == block["iterations_per_arm"]
            assert block[backend][label] == {
                "p50": float(statistics.median(samples)),
                "p95": _percentile(samples, 95),
            }

    limitations = " ".join(artifact["limitations"])
    assert "connect runner path" in limitations
    assert "did not request resources explicitly" in limitations
    assert "not drop-in replacements" in limitations

    serialized = artifact_path.read_text().lower()
    for forbidden in (
        "modal.host",
        "sb-",
        "run_",
        "api_key",
        "access_token",
        "base_url",
        "bearer",
        "://",
    ):
        assert forbidden not in serialized
