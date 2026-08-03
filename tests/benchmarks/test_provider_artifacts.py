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
