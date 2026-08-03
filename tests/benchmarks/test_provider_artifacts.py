from __future__ import annotations

import hashlib
import json
import re
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

# Shapes of every identifier class the raw benchmark arms carry. The 1-core subprocess A/B
# guard harvests these out of its ignored raw arms and asserts none of them reached the
# tracked artifact, so the leak check does not depend on this file listing real identifiers.
_IDENTIFIER_PATTERNS = (
    r"sb-[A-Za-z0-9_-]{8,}",
    r"ta-[A-Za-z0-9]{8,}",
    r"[A-Za-z0-9.-]+\.modal\.host",
    r"https?://[^\s\"']+",
    r"/(?:Users|home|tmp|var)/[A-Za-z0-9._/-]+",
    r"\b\d{1,3}(?:\.\d{1,3}){3}\b",
)


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
    artifact_path = REPO_ROOT / "benchmark-data/modal-subprocess-runner-ab-2026-07-30.json"
    artifact = json.loads(artifact_path.read_text())

    assert artifact["status"] == "candidate"
    assert artifact["provenance"]["source_sha"] == "7c8e6810ee7fc1da4046590525b0e8d48e1fd919"
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
    assert hashlib.sha256(serialized_configuration).hexdigest() == expected_configuration_sha256
    assert configuration["requested"]["modal_ingress"] == "attested-tunnel"
    assert configuration["requested"]["runner_path"] == "inherited"
    assert configuration["requested"]["runner_only"] is True
    assert configuration["requested"]["modal_cpu"] == 4.0
    assert configuration["requested"]["modal_memory_mib"] == 8192
    assert configuration["requested"]["runner_cpu"] == 4.0
    assert configuration["requested"]["runner_memory_mib"] == 8192
    assert configuration["observed"]["canonical_surface_name"] == "modal-daemon-attested-tunnel"
    assert configuration["observed"]["external_caller_included"] is False
    assert configuration["observed"]["input_backend"] == "xtest"

    block = artifact["subprocess_runner_ab"]
    assert block["metric"] == "modal-colocated shell-command-echo-v2 milliseconds"
    assert block["case"] == "command_nonlogin_shell_echo"
    assert block["iterations_per_arm"] == 30
    assert block["warmup_iterations"] == 1
    assert block["p50_method"] == "statistics.median"
    assert block["p95_method"] == "linear interpolation on sorted values at rank 0.95*(n-1)"
    assert artifact["verification"]["subprocess_runner_ab_failures"] == 0
    assert artifact["verification"]["subprocess_runner_ab_successful_iterations_per_arm"] == 30

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


def test_modal_caller_placement_2026_07_31_is_pinned_and_secret_safe() -> None:
    artifact_path = REPO_ROOT / "benchmark-data/modal-caller-placement-us-west-2-2026-07-31.json"
    artifact = json.loads(artifact_path.read_text())

    assert artifact["status"] == "candidate"
    assert artifact["benchmark"] == "modal-caller-placement"
    assert artifact["provenance"]["source_sha"] == "df6543483fbe06c3ac1b070e7824de99ffb5f9d4"
    assert artifact["provenance"]["harness_state"] == "clean"
    assert artifact["provenance"]["git_worktree_clean"] is True
    assert artifact["provenance"]["raw_artifacts_tracked"] is False
    assert artifact["provenance"]["generator"] is None

    # Draw 1 is pinned on provenance grounds. Draw 2's co-located arm launched at a
    # newer revision than its own external arm, so it is replication only.
    selection = artifact["draw_selection"]
    assert selection["pinned_draw"] == 1
    assert selection["replication_draw"] == 2
    assert selection["basis"] == "provenance rather than the measured values"

    configuration = dict(artifact["configuration"])
    expected_configuration_sha256 = configuration.pop("safe_configuration_sha256")
    serialized_configuration = json.dumps(
        configuration, sort_keys=True, separators=(",", ":")
    ).encode()
    assert hashlib.sha256(serialized_configuration).hexdigest() == expected_configuration_sha256
    assert configuration["requested"]["modal_ingress"] == "attested-tunnel"
    assert configuration["requested"]["runner_path"] == "inherited"
    assert configuration["requested"]["modal_cpu"] == 1.0
    assert configuration["requested"]["modal_memory_mib"] == 2048
    assert configuration["requested"]["runner_cpu"] == 1.0
    assert configuration["requested"]["runner_memory_mib"] == 2048
    assert configuration["observed"]["canonical_surface_name"] == "modal-daemon-attested-tunnel"
    assert configuration["observed"]["external_caller_included"] is True
    assert configuration["observed"]["ingress_shared_by_both_arms"] is True
    assert configuration["observed"]["input_backend"] == "xtest"

    # Both arms share one ingress, so no measurement key may name a transport.
    for key in _walk_keys(artifact["caller_placement"]):
        assert "connect" not in key
        assert "tunnel" not in key

    block = artifact["caller_placement"]
    assert block["metric"] == "modal daemon-http operation milliseconds, by caller location"
    assert block["iterations_per_arm"] == 30
    assert block["warmup_iterations"] == 1
    assert block["p50_method"] == "statistics.median"
    assert block["p95_method"] == "linear interpolation on sorted values at rank 0.95*(n-1)"
    assert artifact["verification"]["caller_placement_failures"] == 0
    assert artifact["verification"]["caller_placement_successful_iterations_per_arm"] == 30
    assert artifact["verification"]["caller_placement_leaked_sandboxes"] == 0

    expected_pinned_cases = {
        "click_then_screenshot": {
            "external_caller": {
                "total": {"p50": 84.99343751464039, "p95": 87.44509988464415},
                "daemon": {"p50": 25.23418200000016, "p95": 25.87979964999878},
                "caller_transport_overhead": {"p50": 59.96349915355381, "p95": 62.846012178058785},
            },
            "colocated_runner": {
                "total": {"p50": 38.230560499998845, "p95": 39.593814649999985},
                "daemon": {"p50": 29.702908500013336, "p95": 31.4607581499871},
                "caller_transport_overhead": {"p50": 8.107572000001895, "p95": 8.892650550009762},
            },
        },
        "command_echo": {
            "external_caller": {
                "total": {"p50": 71.58064539544284, "p95": 169.21185448300048},
                "daemon": {"p50": 32.666868499998, "p95": 130.54658585000416},
                "caller_transport_overhead": {"p50": 38.552270499767616, "p95": 39.578415412943},
            },
            "colocated_runner": {
                "total": {"p50": 42.42843649999983, "p95": 88.28117144999953},
                "daemon": {"p50": 38.55147250000357, "p95": 80.60145495000484},
                "caller_transport_overhead": {"p50": 3.7614845000062402, "p95": 7.356180700008784},
            },
        },
        "command_nonlogin_shell_echo": {
            "external_caller": {
                "total": {"p50": 44.43214600905776, "p95": 46.8014896963723},
                "daemon": {"p50": 6.190512499998135, "p95": 7.808737250000062},
                "caller_transport_overhead": {"p50": 37.841657136379325, "p95": 39.63790188474654},
            },
            "colocated_runner": {
                "total": {"p50": 11.148588999999376, "p95": 14.103811799999551},
                "daemon": {"p50": 7.371789500012937, "p95": 9.970872099994207},
                "caller_transport_overhead": {"p50": 3.535652999992145, "p95": 4.683394400009355},
            },
        },
        "coordinate_click": {
            "external_caller": {
                "total": {"p50": 40.334312594495714, "p95": 41.345439467113465},
                "daemon": {"p50": 1.1320060000006293, "p95": 1.5433924500005247},
                "caller_transport_overhead": {"p50": 39.11250293412216, "p95": 40.21366296711282},
            },
            "colocated_runner": {
                "total": {"p50": 3.958654999999922, "p95": 4.475016199999793},
                "daemon": {"p50": 0.7469255000103203, "p95": 0.9303746000213662},
                "caller_transport_overhead": {"p50": 3.157823499993384, "p95": 3.739074549997845},
            },
        },
        "coordinate_click_sequence": {
            "external_caller": {
                "total": {"p50": 43.718541390262544, "p95": 51.2169935624115},
                "daemon": {"p50": 4.810929999999658, "p95": 8.11138825000057},
                "caller_transport_overhead": {"p50": 38.76441794808194, "p95": 43.480717760854745},
            },
            "colocated_runner": {
                "total": {"p50": 7.4880680000006805, "p95": 11.544678849999976},
                "daemon": {"p50": 3.9228159999993295, "p95": 7.113682549999101},
                "caller_transport_overhead": {"p50": 3.6448544999911903, "p95": 4.71259595000557},
            },
        },
        "move_click": {
            "external_caller": {
                "total": {"p50": 39.26943801343441, "p95": 40.16364378621802},
                "daemon": {"p50": 1.4245140000017642, "p95": 1.798180599999632},
                "caller_transport_overhead": {"p50": 37.85689152735472, "p95": 38.558389154529316},
            },
            "colocated_runner": {
                "total": {"p50": 4.6504235000002225, "p95": 6.159562050000654},
                "daemon": {"p50": 1.3103704999934962, "p95": 2.2252358000031327},
                "caller_transport_overhead": {"p50": 3.3729335000014515, "p95": 4.150260550001228},
            },
        },
        "move_click_sequence": {
            "external_caller": {
                "total": {"p50": 45.17360404133797, "p95": 48.581758432555944},
                "daemon": {"p50": 5.880869000000288, "p95": 8.230420799999823},
                "caller_transport_overhead": {"p50": 38.9993905609316, "p95": 41.73729342166314},
            },
            "colocated_runner": {
                "total": {"p50": 9.433956499999674, "p95": 15.107657900000856},
                "daemon": {"p50": 5.498563499997999, "p95": 9.554019550004963},
                "caller_transport_overhead": {"p50": 3.9033674999933154, "p95": 5.395066900004862},
            },
        },
        "screenshot_full": {
            "external_caller": {
                "total": {"p50": 86.79583354387432, "p95": 90.12950604083017},
                "daemon": {"p50": 22.6038415000005, "p95": 23.298692550001476},
                "caller_transport_overhead": {"p50": 64.44636752330268, "p95": 67.014113440832},
            },
            "colocated_runner": {
                "total": {"p50": 38.110908000000165, "p95": 40.2548785999997},
                "daemon": {"p50": 22.57310499999221, "p95": 23.285406949992193},
                "caller_transport_overhead": {"p50": 15.53878700000011, "p95": 17.2312528500143},
            },
        },
        "type_1000_chars": {
            "external_caller": {
                "total": {"p50": 88.00533344037831, "p95": 102.20272679580376},
                "daemon": {"p50": 49.677799000001244, "p95": 64.3860569000008},
                "caller_transport_overhead": {"p50": 38.45023989725149, "p95": 39.815136062762235},
            },
            "colocated_runner": {
                "total": {"p50": 54.10872600000083, "p95": 65.70995714999971},
                "daemon": {"p50": 49.94912600000134, "p95": 61.64651489998504},
                "caller_transport_overhead": {"p50": 4.217830499990249, "p95": 4.907071450001688},
            },
        },
        "type_100_chars": {
            "external_caller": {
                "total": {"p50": 45.988500118255615, "p95": 47.819591825827956},
                "daemon": {"p50": 7.545984000000061, "p95": 8.682693199998148},
                "caller_transport_overhead": {"p50": 38.44593854170242, "p95": 39.5292597559596},
            },
            "colocated_runner": {
                "total": {"p50": 10.250275000000642, "p95": 10.705646100000443},
                "daemon": {"p50": 6.761088000004634, "p95": 7.306425550009976},
                "caller_transport_overhead": {"p50": 3.4394255000034235, "p95": 3.840164100004273},
            },
        },
    }
    expected_replication_total_p50 = {
        "click_then_screenshot": (84.0304164448753, 42.812091499999205),
        "command_echo": (68.99158307351172, 48.7761674999998),
        "command_nonlogin_shell_echo": (41.79383302107453, 13.329555500000367),
        "coordinate_click": (36.614728975109756, 4.57555799999998),
        "coordinate_click_sequence": (40.4612289275974, 8.706572500000398),
        "move_click": (36.70289600268006, 4.680361499999286),
        "move_click_sequence": (41.977666900493205, 13.033381500000552),
        "screenshot_full": (84.77506251074374, 39.53991449999972),
        "type_1000_chars": (83.94287503324449, 59.660332499999136),
        "type_100_chars": (42.85108402837068, 11.195665000000687),
    }

    assert sorted(block["cases_measured"]) == sorted(expected_pinned_cases)
    pinned = block["pinned_run"]
    replication = block["replication_run"]
    assert pinned["draw"] == 1
    assert replication["draw"] == 2
    assert pinned["source_revision_consistent"] is True
    assert replication["source_revision_consistent"] is False
    assert pinned["source_sha_external_caller"] == pinned["source_sha_colocated_runner"]
    assert pinned["raw_artifact_sha256"] == (
        "99fd420f14156b64af3af179b711ecdafa291e8927ae82d1477e54ca15fe40cb"
    )
    assert replication["raw_artifact_sha256"] == (
        "282214aeb69f850e14b485998bfdf5ec85c0899aa06a5a64c7d3f2c1b57151c9"
    )
    for run, draw in ((pinned, 1), (replication, 2)):
        assert run["both_arms_measured_one_target_desktop"] is True
        assert run["raw_artifact"] == (
            f"benchmark-results/caller-placement-2026-07-31/attested-tunnel-1cpu-draw{draw}.json"
        )

    # Literal pin of every reported value in the pinned run.
    assert pinned["cases"] == expected_pinned_cases
    for case, (external_p50, colocated_p50) in expected_replication_total_p50.items():
        assert replication["cases"][case]["external_caller"]["total"]["p50"] == external_p50
        assert replication["cases"][case]["colocated_runner"]["total"]["p50"] == colocated_p50

    # The ordering claim the artifact exists to support: moving the caller next to the
    # target cuts caller transport overhead for every measured case, in both draws.
    for run in (pinned, replication):
        for case, arms in run["cases"].items():
            external = arms["external_caller"]["caller_transport_overhead"]["p50"]
            colocated = arms["colocated_runner"]["caller_transport_overhead"]["p50"]
            assert colocated < external, case
            colocated_total = arms["colocated_runner"]["total"]["p50"]
            assert colocated_total < arms["external_caller"]["total"]["p50"], case

    # Independent recomputation from the raw draws when they are present in an
    # ignored working tree. This does not read any summary the harness stored.
    raw_arm_names = {
        "external_caller": "external_caller",
        "colocated_runner": "modal_colocated_runner",
    }
    for run in (pinned, replication):
        raw_path = REPO_ROOT / run["raw_artifact"]
        if not raw_path.exists():
            continue
        raw_bytes = raw_path.read_bytes()
        assert hashlib.sha256(raw_bytes).hexdigest() == run["raw_artifact_sha256"]
        document = json.loads(raw_bytes)
        for arm, raw_arm in raw_arm_names.items():
            measured = document["runs"][raw_arm]
            assert measured["warmup_iterations"] == block["warmup_iterations"]
            cases = measured["surfaces"]["daemon-http"]["cases"]
            for case in block["cases_measured"]:
                entry = cases[case]
                assert entry["iterations"] == block["iterations_per_arm"]
                assert entry["failures"] == []
                for label, samples_key in (
                    ("total", "samples_ms"),
                    ("daemon", "daemon_samples_ms"),
                    ("caller_transport_overhead", "overhead_samples_ms"),
                ):
                    samples = sorted(entry[samples_key])
                    assert len(samples) == block["iterations_per_arm"]
                    assert run["cases"][case][arm][label] == {
                        "p50": float(statistics.median(samples)),
                        "p95": _percentile(samples, 95),
                    }, (run["draw"], case, arm, label)

    limitations = " ".join(artifact["limitations"])
    assert "Draw 2 is replication only" in limitations
    assert "does not hold the moment fixed" in limitations
    assert "1 CPU" in limitations

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
        ".w.modal",
        "/users/",
    ):
        assert forbidden not in serialized


def test_modal_subprocess_runner_ab_1cpu_2026_07_31_is_pinned_and_secret_safe() -> None:
    artifact_path = REPO_ROOT / "benchmark-data/modal-subprocess-runner-ab-1cpu-2026-07-31.json"
    artifact = json.loads(artifact_path.read_text())

    assert artifact["status"] == "candidate"
    assert artifact["benchmark"] == "modal-subprocess-runner-ab"
    assert artifact["variant"] == "canonical-1cpu-shape"
    assert artifact["provenance"]["source_sha"] == "f330baaf4c2d020829cd22fdc2d83ef0646948d7"
    assert artifact["provenance"]["harness_state"] == "clean"
    assert artifact["provenance"]["git_worktree_clean"] is True
    assert artifact["provenance"]["raw_artifacts_tracked"] is False
    assert artifact["provenance"]["generator"] is None
    assert artifact["provenance"]["source_revision_consistent"] is True
    assert artifact["provenance"]["default_branch_moved_during_measurement"] is False
    assert artifact["provenance"]["arms_measured_sequentially"] is True
    assert artifact["provenance"]["arm_order"] == ["asyncio", "threaded", "isolated-asyncio"]
    assert artifact["provenance"]["raw_artifact_directory"] == (
        "benchmark-results/subprocess-ab-1cpu-2026-07-31"
    )
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
    assert hashlib.sha256(serialized_configuration).hexdigest() == expected_configuration_sha256
    assert configuration["requested"]["modal_ingress"] == "attested-tunnel"
    assert configuration["requested"]["runner_path"] == "inherited"
    assert configuration["requested"]["runner_only"] is True
    assert configuration["requested"]["modal_cpu"] == 1.0
    assert configuration["requested"]["modal_memory_mib"] == 2048
    assert configuration["requested"]["runner_cpu"] == 1.0
    assert configuration["requested"]["runner_memory_mib"] == 2048
    assert configuration["requested"]["iterations"] == 30
    assert configuration["observed"]["canonical_surface_name"] == "modal-daemon-attested-tunnel"
    assert configuration["observed"]["external_caller_included"] is False
    assert configuration["observed"]["input_backend"] == "xtest"
    assert configuration["observed"]["resolved_cpu"] == 1.0
    assert configuration["observed"]["resolved_memory_gib"] == 2.0

    block = artifact["subprocess_runner_ab"]
    assert block["metric"] == "modal-colocated shell-command-echo-v2 milliseconds"
    assert block["case"] == "command_nonlogin_shell_echo"
    assert block["iterations_per_arm"] == 30
    assert block["warmup_iterations"] == 1
    assert block["p50_method"] == "statistics.median"
    assert block["p95_method"] == "linear interpolation on sorted values at rank 0.95*(n-1)"
    assert artifact["verification"]["subprocess_runner_ab_failures"] == 0
    assert artifact["verification"]["subprocess_runner_ab_successful_iterations_per_arm"] == 30

    expected_arms = {
        "asyncio": {
            "raw_artifact_sha256": (
                "48e4d009eb8882013eec591bacc5edac890f3638420763acc4cfae305836a6e1"
            ),
            "measured_at": "2026-07-31T18:02:14.270552+00:00",
            "sample_stability_status": "outlier_sensitive",
            "total": {"p50": 63.28590300000059, "p95": 247.8512548999992},
            "daemon": {"p50": 56.53446200000012, "p95": 241.07133524999767},
            "caller_transport_overhead": {
                "p50": 6.721340999995981,
                "p95": 7.7782156000013805,
            },
            "total_distribution": {
                "sample_count": 30,
                "mean": 85.38093453333359,
                "max": 398.02226399999796,
                "mean_excluding_largest": 74.6001990344831,
                "mean_excluding_two_largest": 63.14149700000035,
            },
        },
        "threaded": {
            "raw_artifact_sha256": (
                "1f78851dca70abc290fb615b925da8b21d8a2801532160f53370117a69257f18"
            ),
            "measured_at": "2026-07-31T18:05:21.372796+00:00",
            "sample_stability_status": "stable",
            "total": {"p50": 10.66654249999921, "p95": 13.04391950000019},
            "daemon": {"p50": 6.873940000000189, "p95": 9.282359200000379},
            "caller_transport_overhead": {
                "p50": 3.618637999999841,
                "p95": 4.527189849997802,
            },
            "total_distribution": {
                "sample_count": 30,
                "mean": 10.653114633333397,
                "max": 13.496486000001084,
                "mean_excluding_largest": 10.555067344827615,
                "mean_excluding_two_largest": 10.465730892857168,
            },
        },
        "isolated-asyncio": {
            "raw_artifact_sha256": (
                "0983e8e10ae311b48bd57401bbb0be084552059260e0d3a14f875b0fa19472af"
            ),
            "measured_at": "2026-07-31T18:08:03.225965+00:00",
            "sample_stability_status": "outlier_sensitive",
            "total": {"p50": 13.75396549999941, "p95": 18.776513500000647},
            "daemon": {"p50": 8.56449700000006, "p95": 12.740329900001868},
            "caller_transport_overhead": {
                "p50": 5.001920499998036,
                "p95": 6.001190399999954,
            },
            "total_distribution": {
                "sample_count": 30,
                "mean": 21.530461399999727,
                "max": 232.72994899999944,
                "mean_excluding_largest": 14.247720448275595,
                "mean_excluding_two_largest": 14.08311342857108,
            },
        },
    }

    # Literal pin of every published value. Begin.
    for backend, expected in expected_arms.items():
        arm = block[backend]
        assert arm["raw_artifact"] == (
            f"benchmark-results/subprocess-ab-1cpu-2026-07-31/{backend}.json"
        )
        assert arm["raw_artifact_sha256"] == expected["raw_artifact_sha256"]
        assert arm["measured_at"] == expected["measured_at"]
        assert arm["successful_iterations"] == 30
        assert arm["failures"] == 0
        assert arm["sample_stability_status"] == expected["sample_stability_status"]
        for label in ("total", "daemon", "caller_transport_overhead"):
            assert arm[label] == expected[label]
        assert arm["total_distribution"] == expected["total_distribution"]
    # Literal pin of every published value. End.

    # The ordering this artifact exists to record, and its flip against the 4-core run.
    measured_p50 = {backend: block[backend]["total"]["p50"] for backend in expected_arms}
    assert block["observed_p50_ordering"] == sorted(measured_p50, key=measured_p50.__getitem__)
    assert block["observed_p50_ordering"] == ["threaded", "isolated-asyncio", "asyncio"]

    baseline_path = REPO_ROOT / "benchmark-data/modal-subprocess-runner-ab-2026-07-30.json"
    comparison = artifact["comparison_baseline"]
    assert comparison["artifact"] == "benchmark-data/modal-subprocess-runner-ab-2026-07-30.json"
    assert comparison["artifact_sha256"] == hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    assert comparison["date"] == "2026-07-30"
    assert comparison["is_shape_ablation"] is False
    assert comparison["supersedes_baseline"] is False
    assert comparison["differs_from_this_run"] == ["date", "requested cpu and memory"]
    assert comparison["requested_shape"] == {
        "modal_cpu": 4.0,
        "modal_memory_mib": 8192,
        "runner_cpu": 4.0,
        "runner_memory_mib": 8192,
    }

    # Every restated baseline figure is bound to the tracked 2026-07-30 artifact, so the
    # comparison block cannot drift away from the run it names.
    baseline = json.loads(baseline_path.read_text())
    for key, value in comparison["requested_shape"].items():
        assert baseline["configuration"]["requested"][key] == value
    assert sorted(comparison["arms"]) == sorted(expected_arms)
    for backend, restated in comparison["arms"].items():
        source = baseline["subprocess_runner_ab"][backend]
        assert sorted(restated) == ["caller_transport_overhead", "daemon", "total"]
        for label, measurement in restated.items():
            assert measurement == {key: source[label][key] for key in ("p50", "p95")}
    baseline_p50 = {
        backend: restated["total"]["p50"] for backend, restated in comparison["arms"].items()
    }
    assert comparison["observed_p50_ordering"] == sorted(baseline_p50, key=baseline_p50.__getitem__)
    assert comparison["observed_p50_ordering"] != block["observed_p50_ordering"]

    # Independent recomputation from the raw arms when they are present in an ignored
    # working tree. This reads no summary the harness stored, so it fails on a perturbed
    # published value even with the literal pin above removed. It is inert on CI, where
    # benchmark-results/ is gitignored and the raw arms are absent.
    for backend in expected_arms:
        raw_path = REPO_ROOT / block[backend]["raw_artifact"]
        if not raw_path.exists():
            continue
        raw_bytes = raw_path.read_bytes()
        assert hashlib.sha256(raw_bytes).hexdigest() == block[backend]["raw_artifact_sha256"]
        document = json.loads(raw_bytes)
        assert document["generated_at"] == block[backend]["measured_at"]
        measured = document["runs"]["modal_colocated_runner"]
        environment = measured["metadata"]["environment"]
        assert environment["subprocess_backend"] == backend
        assert environment["provenance"]["git_revision"] == artifact["provenance"]["source_sha"]
        assert environment["provenance"]["git_worktree_clean"] is True
        assert measured["warmup_iterations"] == block["warmup_iterations"]
        case = measured["surfaces"]["daemon-http"]["cases"][block["case"]]
        assert case["iterations"] == block["iterations_per_arm"]
        assert case["failures"] == []
        assert case["successful_iterations"] == block[backend]["successful_iterations"]
        assert case["sample_stability"]["status"] == block[backend]["sample_stability_status"]
        assert case["command"]["argv"] == artifact["semantics"][block["case"]]["argv"]
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
            }, (backend, label)
        totals = sorted(case["samples_ms"])
        assert block[backend]["total_distribution"] == {
            "sample_count": len(totals),
            "mean": statistics.fmean(totals),
            "max": totals[-1],
            "mean_excluding_largest": statistics.fmean(totals[:-1]),
            "mean_excluding_two_largest": statistics.fmean(totals[:-2]),
        }, backend

    limitations = " ".join(artifact["limitations"])
    assert "no across-day replication" in limitations
    assert "not a clean shape ablation" in limitations
    assert "232.73 ms total sample" in limitations
    assert "publication branch" in limitations
    assert "does not supersede" in limitations
    assert "docs/drafts/" not in limitations

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
        ".w.modal",
        "/users/",
        "sandbox_id",
    ):
        assert forbidden not in serialized

    # Leak scan against the identifiers the raw arms actually carry. Also inert on CI.
    raw_directory = REPO_ROOT / artifact["provenance"]["raw_artifact_directory"]
    harvested: set[str] = set()
    for backend in expected_arms:
        raw_path = raw_directory / f"{backend}.json"
        if not raw_path.exists():
            continue
        text = raw_path.read_text()
        for pattern in _IDENTIFIER_PATTERNS:
            harvested.update(re.findall(pattern, text))
    if raw_directory.exists():
        assert harvested
    published = artifact_path.read_text()
    for identifier in sorted(harvested):
        assert identifier not in published, identifier


def _walk_keys(node: object) -> list[str]:
    if isinstance(node, dict):
        found: list[str] = []
        for key, value in node.items():
            found.append(key)
            found.extend(_walk_keys(value))
        return found
    if isinstance(node, list):
        return [key for item in node for key in _walk_keys(item)]
    return []


def _load_benchmark_artifact(name: str) -> tuple[Path, dict[str, object]]:
    path = REPO_ROOT / "benchmark-data" / name
    return path, json.loads(path.read_text())


def _assert_recomputed_summary(samples: list[float], summary: dict[str, float]) -> None:
    assert len(samples) == 30
    assert summary["mean"] == pytest.approx(statistics.fmean(samples), abs=1e-12)
    assert summary["p50"] == float(statistics.median(samples))
    assert summary["p95"] == pytest.approx(_percentile(sorted(samples), 95), abs=1e-12)


def _assert_companion_secret_safe(path: Path) -> None:
    serialized = path.read_text().lower()
    for forbidden in (
        "modal.host",
        "sb-",
        "run_",
        "api_key",
        "access_token",
        "base_url",
        "bearer",
        "://",
        "/users/",
        "sandbox_id",
    ):
        assert forbidden not in serialized


def test_subprocess_sample_companion_restores_exact_recomputation() -> None:
    path, samples = _load_benchmark_artifact("modal-subprocess-runner-ab-samples-2026-07-30.json")
    summary_path = REPO_ROOT / samples["provenance"]["summary_artifact"]
    summary = json.loads(summary_path.read_text())

    assert samples["status"] == "recovered_supporting_evidence"
    assert samples["provenance"]["samples_tracked"] is True
    assert (
        hashlib.sha256(summary_path.read_bytes()).hexdigest()
        == samples["provenance"]["summary_artifact_sha256"]
    )

    for backend, arm in samples["arms"].items():
        summary_arm = summary["subprocess_runner_ab"][backend]
        for label, samples_key in (
            ("total", "samples_ms"),
            ("daemon", "daemon_samples_ms"),
            ("caller_transport_overhead", "overhead_samples_ms"),
        ):
            values = arm[samples_key]
            assert len(values) == samples["measurement"]["iterations_per_arm"]
            assert summary_arm[label] == {
                "p50": float(statistics.median(values)),
                "p95": _percentile(sorted(values), 95),
            }

        raw_path = REPO_ROOT / arm["raw_artifact"]
        if raw_path.exists():
            assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == arm["raw_artifact_sha256"]
            raw_case = json.loads(raw_path.read_text())["runs"]["modal_colocated_runner"][
                "surfaces"
            ]["daemon-http"]["cases"][samples["measurement"]["case"]]
            for samples_key in (
                "samples_ms",
                "daemon_samples_ms",
                "overhead_samples_ms",
            ):
                assert arm[samples_key] == raw_case[samples_key]

    _assert_companion_secret_safe(path)


def test_batching_replication_is_recomputable_and_non_superseding() -> None:
    path, artifact = _load_benchmark_artifact(
        "modal-action-batching-ab-replication-2026-08-02.json"
    )
    historical_path = REPO_ROOT / artifact["historical_context"]["source_artifact"]

    assert artifact["status"] == "replication"
    assert artifact["provenance"]["harness_state"] == "clean"
    assert artifact["historical_context"]["historical_samples_available"] is False
    assert artifact["historical_context"]["supersedes_historical_result"] is False
    assert (
        hashlib.sha256(historical_path.read_bytes()).hexdigest()
        == artifact["historical_context"]["source_artifact_sha256"]
    )

    for case in ("batch_4_clicks", "separate_4_clicks"):
        measured = artifact["cases"][case]
        _assert_recomputed_summary(measured["samples_ms"], measured["summary_ms"])

    comparison = artifact["comparison"]
    batch_p50 = artifact["cases"]["batch_4_clicks"]["summary_ms"]["p50"]
    separate_p50 = artifact["cases"]["separate_4_clicks"]["summary_ms"]["p50"]
    assert comparison["batch_p50_ms"] == batch_p50
    assert comparison["separate_p50_ms"] == separate_p50
    assert comparison["speedup"] == separate_p50 / batch_p50
    assert comparison["delta_ms"] == separate_p50 - batch_p50
    assert artifact["verification"] == {
        "eligibility": "publishable",
        "failures": 0,
        "replacement_samples": 0,
        "placement_verified": True,
        "target_cleanup_succeeded": True,
        "final_cleanup_succeeded": True,
        "remaining_sandboxes": 0,
    }

    raw_path = REPO_ROOT / artifact["provenance"]["raw_artifact"]
    if raw_path.exists():
        assert (
            hashlib.sha256(raw_path.read_bytes()).hexdigest()
            == artifact["provenance"]["raw_artifact_sha256"]
        )
    _assert_companion_secret_safe(path)


def test_native_x11_replication_is_recomputable_and_non_superseding() -> None:
    path, artifact = _load_benchmark_artifact(
        "modal-native-x11-backend-ab-replication-2026-08-02.json"
    )
    cases = ("move_click", "move_click_sequence", "type_100_chars", "type_1000_chars")

    assert artifact["status"] == "replication"
    assert artifact["provenance"]["harness_state"] == "clean"
    assert artifact["historical_context"]["historical_samples_available"] is False
    assert artifact["historical_context"]["supersedes_historical_result"] is False
    assert artifact["historical_context"]["historical_status"] == (
        "archived_dirty_worktree_diagnostic"
    )
    report_path = REPO_ROOT / artifact["historical_context"]["source_report"]
    assert hashlib.sha256(report_path.read_bytes()).hexdigest() == artifact[
        "historical_context"
    ]["source_report_sha256"]

    for arm in artifact["arms"].values():
        assert arm["verification"]["cursor_position"]["status"] == "ok"
        assert arm["verification"]["type_text"]["status"] == "ok"
        for case in cases:
            measured = arm["cases"][case]
            _assert_recomputed_summary(measured["samples_ms"], measured["summary_ms"])
            _assert_recomputed_summary(measured["daemon_samples_ms"], measured["daemon_summary_ms"])

        raw_path = REPO_ROOT / arm["raw_artifact"]
        if raw_path.exists():
            assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == arm["raw_artifact_sha256"]
            raw_cases = json.loads(raw_path.read_text())["surfaces"]["daemon-http"]["cases"]
            for case in cases:
                assert arm["cases"][case]["samples_ms"] == raw_cases[case]["samples_ms"]
                assert (
                    arm["cases"][case]["daemon_samples_ms"] == raw_cases[case]["daemon_samples_ms"]
                )

    for case in cases:
        xtest = artifact["arms"]["xtest"]["cases"][case]["daemon_samples_ms"]
        xdotool = artifact["arms"]["xdotool"]["cases"][case]["daemon_samples_ms"]
        comparison = artifact["comparison"]["cases"][case]
        assert comparison["xtest_daemon_mean_ms"] == pytest.approx(
            statistics.fmean(xtest), abs=1e-12
        )
        assert comparison["xdotool_daemon_mean_ms"] == pytest.approx(
            statistics.fmean(xdotool), abs=1e-12
        )
        assert comparison["xdotool_over_xtest"] == pytest.approx(
            statistics.fmean(xdotool) / statistics.fmean(xtest), abs=1e-12
        )

    _assert_companion_secret_safe(path)


def test_native_x11_runner_matrix_recomputes_samples_effects_and_preregistered_gates() -> None:
    path, artifact = _load_benchmark_artifact(
        "modal-native-x11-runner-matrix-2026-08-02.json"
    )
    cases = ("move_click", "move_click_sequence", "type_100_chars", "type_1000_chars")
    conditions = (
        ("xtest", "asyncio"),
        ("xtest", "isolated-asyncio"),
        ("xdotool", "asyncio"),
        ("xdotool", "isolated-asyncio"),
    )
    expected_raw_digests = {
        "b01-xdotool-isolated-asyncio": (
            "e014e6ba1d0e48bc40f30d110d6dd74e948a54584db81931a52a4a45b8881d7e"
        ),
        "b01-xtest-asyncio": (
            "b4d52bb9cb28d7dfc379c77b01872934d6b071f83cd4bb0fbc6a32eff925da03"
        ),
        "b01-xdotool-asyncio": (
            "071afb532347c201e5542c82a911a1c506289835ca7958c8b8cc6eef06d9fbb4"
        ),
        "b01-xtest-isolated-asyncio": (
            "887dfb21f67bd2aa0e2439274739e6bb830c98b5e30505cafd2d4c6b3d73a001"
        ),
        "b02-xdotool-asyncio": (
            "65cbbd0db4cff0b03bb4d2a82629220f359e8583c2e7e2be7dc1d45b7d5b0e64"
        ),
        "b02-xdotool-isolated-asyncio": (
            "9f83dcf8aa7a0e7583a8f7c94830a587d8e4730aa7982df7c58e8dc0f2cd041d"
        ),
        "b02-xtest-asyncio": (
            "a2dfa1d05db64d0ab9da24b4f1102c8a03c98280279941692010aa5a9c408e8c"
        ),
        "b02-xtest-isolated-asyncio": (
            "b274968e8e27bcc469da25d9ef02d23bfa3b6736326a1739eedd56057a6060ad"
        ),
        "b03-xtest-asyncio": (
            "4e128feee40ca8cdd3c75ca05f6443bfaec62972bb93da609cb1ec315449fcf7"
        ),
        "b03-xdotool-asyncio": (
            "7625f508c7040f20ddbb7b8924afdb82a7985f23920a5eeba3bac9e920a2682d"
        ),
        "b03-xtest-isolated-asyncio": (
            "3d7732cbb8d302ac8919d03da6d6d4b3f90a87c3a6d8e95ace15a2156f7e2b68"
        ),
        "b03-xdotool-isolated-asyncio": (
            "337b9fa92ab47288b1d5a4e60720a3aa200c2bf18241fadfebcddfd2afe6b691"
        ),
    }

    assert artifact["status"] == "diagnostic_matrix"
    assert artifact["provenance"]["source"] == {
        "git_branch": "chore/blog-public-prep",
        "git_revision": "968f542163b07de38f5d35c03801314c07c99293",
        "git_worktree_clean": True,
        "runner": {
            "name": "native_x11_runner_matrix.py",
            "path": "scripts/benchmarks/native_x11_runner_matrix.py",
            "sha256": "5cebfd25d3063f0db55528c06b5c6137ae94356442feaf4d2f1fdbdc17dc0027",
        },
    }
    runner = REPO_ROOT / artifact["provenance"]["source"]["runner"]["path"]
    assert hashlib.sha256(runner.read_bytes()).hexdigest() == artifact["provenance"][
        "source"
    ]["runner"]["sha256"]
    assert artifact["environment"]["actual_placement"] == {
        "cloud": "CLOUD_PROVIDER_AWS",
        "region": "us-west-2",
    }
    assert artifact["controls"]["blocks"] == 3
    assert artifact["controls"]["iterations_per_cell"] == 30
    assert artifact["controls"]["fresh_sandbox_per_cell"] is True
    assert artifact["order_seed"] == 20260802
    assert artifact["schedule"] == [
        {
            "block": cell["block"],
            "block_order": cell["block_order"],
            "cell_id": cell["cell_id"],
            "input_backend": cell["input_backend"],
            "raw_artifact": f"raw/{Path(cell['raw_artifact']).name}",
            "sequence": cell["sequence"],
            "subprocess_backend": cell["subprocess_backend"],
        }
        for cell in artifact["cells"]
    ]

    cells_by_id = {cell["cell_id"]: cell for cell in artifact["cells"]}
    assert len(cells_by_id) == 12
    assert {
        cell_id: cell["raw_artifact_sha256"] for cell_id, cell in cells_by_id.items()
    } == expected_raw_digests
    for cell in artifact["cells"]:
        compact = dict(cell)
        digest = compact.pop("canonical_cell_sha256")
        canonical = json.dumps(compact, sort_keys=True, separators=(",", ":")).encode()
        assert hashlib.sha256(canonical).hexdigest() == digest
        assert cell["status"] == "complete"
        assert cell["cleanup"] == {"attempted": True, "errors": [], "succeeded": True}
        assert cell["verification"]["cursor_position"]["status"] == "ok"
        assert cell["verification"]["type_text"]["status"] == "ok"
        for case in cases:
            measured = cell["cases"][case]
            assert measured["successful_iterations"] == 30
            assert measured["failure_count"] == 0
            assert len(measured["wall_samples_ms"]) == 30
            assert len(measured["daemon_samples_ms"]) == 30
            assert measured["wall_mean_ms"] == pytest.approx(
                statistics.fmean(measured["wall_samples_ms"]), abs=1e-12
            )
            assert measured["daemon_mean_ms"] == pytest.approx(
                statistics.fmean(measured["daemon_samples_ms"]), abs=1e-12
            )

        raw_path = REPO_ROOT / cell["raw_artifact"]
        if not raw_path.exists():
            raw_path = (
                Path("/private/tmp/native-x11-runner-matrix-2026-08-02/raw")
                / Path(cell["raw_artifact"]).name
            )
        if raw_path.exists():
            assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == cell[
                "raw_artifact_sha256"
            ]
            raw_cases = json.loads(raw_path.read_text())["surfaces"]["daemon-http"]["cases"]
            for case in cases:
                assert cell["cases"][case]["wall_samples_ms"] == raw_cases[case]["samples_ms"]
                assert cell["cases"][case]["daemon_samples_ms"] == raw_cases[case][
                    "daemon_samples_ms"
                ]

    for input_backend, subprocess_backend in conditions:
        condition = f"{input_backend}/{subprocess_backend}"
        condition_cells = [
            cell
            for cell in artifact["cells"]
            if cell["input_backend"] == input_backend
            and cell["subprocess_backend"] == subprocess_backend
        ]
        assert len(condition_cells) == 3
        for case in cases:
            aggregate = artifact["aggregates"][condition]["cases"][case]
            wall_samples = [
                sample
                for cell in condition_cells
                for sample in cell["cases"][case]["wall_samples_ms"]
            ]
            daemon_samples = [
                sample
                for cell in condition_cells
                for sample in cell["cases"][case]["daemon_samples_ms"]
            ]
            assert aggregate["pooled_sample_count"] == 90
            assert aggregate["pooled_wall_mean_ms"] == pytest.approx(
                statistics.fmean(wall_samples), abs=1e-12
            )
            assert aggregate["pooled_daemon_mean_ms"] == pytest.approx(
                statistics.fmean(daemon_samples), abs=1e-12
            )
            for block in aggregate["per_block"]:
                cell = cells_by_id[block["cell_id"]]
                assert block["block"] == cell["block"]
                assert block["wall_mean_ms"] == pytest.approx(
                    statistics.fmean(cell["cases"][case]["wall_samples_ms"]), abs=1e-12
                )
                assert block["daemon_mean_ms"] == pytest.approx(
                    statistics.fmean(cell["cases"][case]["daemon_samples_ms"]), abs=1e-12
                )

    for input_backend in ("xtest", "xdotool"):
        for case in cases:
            effect = artifact["runner_effects"][input_backend]["cases"][case]
            for block_effect in effect["per_block"]:
                block = block_effect["block"]
                shared = next(
                    cell
                    for cell in artifact["cells"]
                    if cell["block"] == block
                    and cell["input_backend"] == input_backend
                    and cell["subprocess_backend"] == "asyncio"
                )
                isolated = next(
                    cell
                    for cell in artifact["cells"]
                    if cell["block"] == block
                    and cell["input_backend"] == input_backend
                    and cell["subprocess_backend"] == "isolated-asyncio"
                )
                assert block_effect[
                    "wall_mean_delta_shared_minus_isolated_ms"
                ] == pytest.approx(
                    shared["cases"][case]["wall_mean_ms"]
                    - isolated["cases"][case]["wall_mean_ms"],
                    abs=1e-12,
                )
                assert block_effect[
                    "daemon_mean_delta_shared_minus_isolated_ms"
                ] == pytest.approx(
                    shared["cases"][case]["daemon_mean_ms"]
                    - isolated["cases"][case]["daemon_mean_ms"],
                    abs=1e-12,
                )
            shared = artifact["aggregates"][f"{input_backend}/asyncio"]["cases"][case]
            isolated = artifact["aggregates"][f"{input_backend}/isolated-asyncio"][
                "cases"
            ][case]
            assert effect[
                "pooled_wall_mean_delta_shared_minus_isolated_ms"
            ] == pytest.approx(
                shared["pooled_wall_mean_ms"] - isolated["pooled_wall_mean_ms"], abs=1e-12
            )
            assert effect[
                "pooled_daemon_mean_delta_shared_minus_isolated_ms"
            ] == pytest.approx(
                shared["pooled_daemon_mean_ms"] - isolated["pooled_daemon_mean_ms"],
                abs=1e-12,
            )

    gates = artifact["gates"]
    assert gates["preregistered_before_results"] is True
    assert gates["validity"]["passed"] is all(
        item["measurements_30_of_30"]
        and item["cursor_and_type_verification_succeeded"]
        for item in gates["validity"]["cells"]
    )
    for case in ("move_click", "move_click_sequence"):
        observed = gates["runner_direction"]["cases"][case]
        expected = [
            item["daemon_mean_delta_shared_minus_isolated_ms"]
            for item in artifact["runner_effects"]["xdotool"]["cases"][case]["per_block"]
        ]
        assert observed["per_block_daemon_mean_delta_shared_minus_isolated_ms"] == expected
        assert observed["passed"] is all(delta > 0 for delta in expected)
    assert gates["runner_direction"]["passed"] is all(
        item["passed"] for item in gates["runner_direction"]["cases"].values()
    )

    assert gates["launch_scaling"]["numeric_tolerance_preregistered"] is False
    assert gates["launch_scaling"]["passed"] is None
    assert gates["launch_scaling"]["status"] == "supported_qualitatively"
    for block in gates["launch_scaling"]["blocks"]:
        index = block["block"] - 1
        move_delta = gates["runner_direction"]["cases"]["move_click"][
            "per_block_daemon_mean_delta_shared_minus_isolated_ms"
        ][index]
        sequence_delta = gates["runner_direction"]["cases"]["move_click_sequence"][
            "per_block_daemon_mean_delta_shared_minus_isolated_ms"
        ][index]
        assert block["two_launch_delta_per_launch_ms"] == pytest.approx(
            move_delta / 2, abs=1e-12
        )
        assert block["eight_launch_delta_per_launch_ms"] == pytest.approx(
            sequence_delta / 8, abs=1e-12
        )
        assert block["absolute_per_launch_difference_ms"] == pytest.approx(
            abs(move_delta / 2 - sequence_delta / 8), abs=1e-12
        )

    for block in gates["xtest_negative_control"]["blocks"]:
        effect = artifact["runner_effects"]["xtest"]["cases"]["move_click"]["per_block"][
            block["block"] - 1
        ]
        isolated = artifact["aggregates"]["xtest/isolated-asyncio"]["cases"][
            "move_click"
        ]["per_block"][block["block"] - 1]["daemon_mean_ms"]
        absolute = abs(effect["daemon_mean_delta_shared_minus_isolated_ms"])
        relative = absolute / isolated
        assert block["absolute_daemon_mean_delta_ms"] == pytest.approx(absolute, abs=1e-12)
        assert block["relative_to_isolated"] == pytest.approx(relative, abs=1e-12)
        assert block["passed"] is (absolute <= 1.0 or relative <= 0.25)
    assert gates["xtest_negative_control"]["passed"] is all(
        block["passed"] for block in gates["xtest_negative_control"]["blocks"]
    )

    historical = gates["historical_magnitude"]
    pooled_shared_xdotool = artifact["aggregates"]["xdotool/asyncio"]["cases"][
        "move_click"
    ]["pooled_daemon_mean_ms"]
    assert historical["inclusive_range_ms"] == [109.7475, 182.9125]
    assert historical["observed_pooled_daemon_mean_ms"] == pooled_shared_xdotool
    assert historical["passed"] is (109.7475 <= pooled_shared_xdotool <= 182.9125)
    assert gates["all_quantitative_gates_passed"] is all(
        gates[name]["passed"]
        for name in (
            "validity",
            "runner_direction",
            "xtest_negative_control",
            "historical_magnitude",
        )
    )
    assert artifact["historical_context"]["supersedes_historical_result"] is False
    assert artifact["historical_context"]["supersedes_clean_replication"] is False
    assert artifact["cleanup"]["successful_cell_count"] == 12
    assert artifact["cleanup"]["all_succeeded"] is True
    assert artifact["cleanup"]["total_measured_resource_lifetime_ms"] == sum(
        cell["modal_resource_lifetime_ms"] for cell in artifact["cells"]
    )
    _assert_companion_secret_safe(path)


def test_native_x11_historical_source_manifest_binds_provenance_and_claims() -> None:
    path, manifest = _load_benchmark_artifact(
        "modal-native-x11-historical-source-2026-07-23.json"
    )

    assert manifest["status"] == "historical_source_manifest"
    assert manifest["archive_ref"] == {
        "name": "archive/native-x11-input-2026-07-23",
        "kind": "annotated_tag",
        "tag_object": "d07551628ff5ef3f05af67eba9175c297fd649fc",
        "target": "5ada640b090d5716c5bc31f7aeeb0fd2c05b6a66",
        "tagged_at": "2026-08-02T18:55:45-07:00",
        "message": "Archive native X11 input benchmark source",
    }

    snapshot = manifest["source_snapshot"]
    assert snapshot["stash_commit"] == "5ada640b090d5716c5bc31f7aeeb0fd2c05b6a66"
    assert snapshot["tree"] == "d0d968a080b5aabfa3d0a754c2674ee807259548"
    assert snapshot["base"] == {
        "commit": "d7790daf2a81655610f1988b23cc6f5caddf7a16",
        "tree": "85343ec7fc0fa35ed182c444a37b41eacbdb992a",
        "authored_at": "2026-07-23T17:22:15-07:00",
        "committed_at": "2026-07-23T17:22:15-07:00",
        "subject": "fix(benchmarks): price narrow us-west-2 runs",
    }
    observed_parents = [
        (parent["role"], parent["commit"], parent["tree"])
        for parent in snapshot["parents"]
    ]
    assert observed_parents == [
        (
            "base",
            "d7790daf2a81655610f1988b23cc6f5caddf7a16",
            "85343ec7fc0fa35ed182c444a37b41eacbdb992a",
        ),
        (
            "index",
            "48c59d01496669a7640e513f1c8c67f661723abf",
            "85343ec7fc0fa35ed182c444a37b41eacbdb992a",
        ),
        (
            "untracked",
            "ee54578b98fc482c200b0774348d198deaa47fd5",
            "cf9a6a89aecb9b367cf445c9822f9c446cf575c0",
        ),
    ]
    assert snapshot["harness_reported_revision"] == snapshot["base"]["commit"]
    assert snapshot["harness_reported_worktree_clean"] is False

    report = manifest["historical_report"]
    report_path = REPO_ROOT / report["path"]
    report_bytes = report_path.read_bytes()
    assert hashlib.sha256(report_bytes).hexdigest() == report["sha256"]
    git_blob_payload = f"blob {len(report_bytes)}\0".encode() + report_bytes
    assert (
        hashlib.sha1(git_blob_payload, usedforsecurity=False).hexdigest()
        == report["git_blob"]
    )
    assert report["claim_origin_commit"] == "3fee2c52611114c1a6598dc61889b6cff52e3ea5"
    assert report["claim_origin_blob"] == "fa63b8046a5058f98653d722f531626a4a6d5406"
    assert report["claim_origin_sha256"] == (
        "f6e9e6e6df2edd33a2fb55458c9a36d1d03b5ec605d053c112ae371dd79f761e"
    )

    session = manifest["source_session"]
    assert session["session_id"] == "019f913b-8e85-77a0-9268-7e9533462e2f"
    assert session["events"]["comparison_recorded_at"] < session["events"][
        "source_stash_recorded_at"
    ]
    local_session_path = Path.home() / session["locator"]
    if local_session_path.exists():
        session_bytes = local_session_path.read_bytes()
        assert hashlib.sha256(session_bytes).hexdigest() == session["sha256"]
        assert len(session_bytes) == session["byte_count"]
        assert len(session_bytes.splitlines()) == session["line_count"]

    commands = manifest["measurement"]["commands"]
    assert "--input-backend xtest --iterations 3" in commands["xtest"]
    assert "--input-backend xdotool --iterations 3" in commands["xdotool"]
    assert commands["xtest"].endswith("native-x11-input-xtest.json --json")
    assert commands["xdotool"].endswith("native-x11-input-xdotool.json --json")

    columns = report["published_result_columns"]
    session_results = manifest["measurement"]["session_reported_results"]
    for case, published_row in report["published_results"].items():
        recorded = session_results[case]
        expected_row = [
            round(recorded[columns[0]], 2),
            round(recorded[columns[1]], 2),
            round(recorded[columns[2]], 1),
            round(recorded[columns[3]], 2),
            round(recorded[columns[4]], 2),
        ]
        assert published_row == expected_row

    assert manifest["raw_samples"] == {
        "tracked": False,
        "available": False,
        "digests_available": False,
        "arrays_reconstructable": False,
        "historical_paths": [
            "/private/tmp/native-x11-input-xtest.json",
            "/private/tmp/native-x11-input-xdotool.json",
        ],
    }
    assert manifest["semantics"] == {
        "supports_exact_historical_source_reconstruction": True,
        "supports_exact_historical_aggregate_quotation": True,
        "supports_independent_historical_sample_recomputation": False,
        "restores_historical_sample_arrays": False,
        "supersedes_historical_result": False,
        "superseded_by_later_replication": False,
        "later_replication": (
            "benchmark-data/modal-native-x11-backend-ab-replication-2026-08-02.json"
        ),
    }

    serialized = path.read_text().lower()
    for forbidden in ("modal.host", "sandbox_id", "run_id", "bearer", "/users/"):
        assert forbidden not in serialized


def test_six_cent_estimate_recomputes_but_is_not_billing_evidence() -> None:
    _, estimate = _load_benchmark_artifact("modal-optimized-provider-cost-estimate-2026-07-30.json")
    measurement_path = REPO_ROOT / estimate["provenance"]["measurement_artifact"]
    measurement = json.loads(measurement_path.read_text())
    pricing_path = REPO_ROOT / estimate["provenance"]["pricing_research"]

    assert estimate["status"] == "estimate"
    assert estimate["billing_reconciled"] is False
    assert (
        hashlib.sha256(measurement_path.read_bytes()).hexdigest()
        == estimate["provenance"]["measurement_artifact_sha256"]
    )
    assert hashlib.sha256(pricing_path.read_bytes()).hexdigest() == estimate[
        "provenance"
    ]["pricing_research_sha256"]
    assert estimate["inputs"]["run_wall_clock_ms"] == measurement["run_wall_clock_ms"]
    assert estimate["inputs"]["billed_duration_seconds_assumption"] == (
        measurement["run_wall_clock_ms"] / 1000
    )
    assert estimate["inputs"]["region_multiplier"] == 1.75
    assert estimate["inputs"]["rates"] == {
        "function_cpu_usd_per_core_second": 0.0000131,
        "function_memory_usd_per_gib_second": 0.00000222,
        "sandbox_cpu_usd_per_core_second": 0.00003942,
        "sandbox_memory_usd_per_gib_second": 0.00000667,
    }
    assert estimate["inputs"]["function_runner"] == {"cpu_cores": 1, "memory_gib": 2}
    assert estimate["inputs"]["target_sandbox"] == {"cpu_cores": 1, "memory_gib": 2}
    assert measurement["configuration"]["runner_cpu"] == 1.0
    assert measurement["configuration"]["runner_memory_mib"] == 2048
    assert measurement["configuration"]["target_cpu"] == 1.0
    assert measurement["configuration"]["target_memory_mib"] == 2048

    inputs = estimate["inputs"]
    rates = inputs["rates"]
    function_rate = (
        rates["function_cpu_usd_per_core_second"] * inputs["function_runner"]["cpu_cores"]
        + rates["function_memory_usd_per_gib_second"] * inputs["function_runner"]["memory_gib"]
    )
    sandbox_rate = (
        rates["sandbox_cpu_usd_per_core_second"] * inputs["target_sandbox"]["cpu_cores"]
        + rates["sandbox_memory_usd_per_gib_second"] * inputs["target_sandbox"]["memory_gib"]
    )
    combined_rate = inputs["region_multiplier"] * (function_rate + sandbox_rate)
    total = inputs["billed_duration_seconds_assumption"] * combined_rate

    assert estimate["formula"]["function_usd_per_second_before_region"] == function_rate
    assert estimate["formula"]["sandbox_usd_per_second_before_region"] == sandbox_rate
    assert estimate["formula"]["combined_usd_per_second_after_region"] == combined_rate
    assert estimate["formula"]["estimated_usd_per_minute"] == combined_rate * 60
    assert estimate["formula"]["estimated_total_usd"] == total
    assert total == pytest.approx(0.06408062060917732)
    assert estimate["formula"]["rounded_claim_usd"] == 0.06
    assert "image builds" in estimate["exclusions"]
    assert (
        "additional fresh lifecycle Sandboxes and any non-overlapping resource lifetimes"
        in (estimate["exclusions"])
    )
