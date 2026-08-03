from __future__ import annotations

import hashlib
import json
import re
import statistics

import evidence_assertions as evidence

from modal_computer_use.benchmarks.measurement import _percentile

_IDENTIFIER_PATTERNS = (
    r"sb-[A-Za-z0-9_-]{8,}",
    r"ta-[A-Za-z0-9]{8,}",
    r"[A-Za-z0-9.-]+\.modal\.host",
    r"https?://[^\s\"']+",
    r"/(?:Users|home|tmp|var)/[A-Za-z0-9._/-]+",
    r"\b\d{1,3}(?:\.\d{1,3}){3}\b",
)


def test_modal_subprocess_runner_ab_2026_07_30_is_pinned_and_secret_safe() -> None:
    artifact_path = evidence.REPO_ROOT / "benchmark-data/modal-subprocess-runner-ab-2026-07-30.json"
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
        raw_path = evidence.REPO_ROOT / block[backend]["raw_artifact"]
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


def test_modal_subprocess_runner_ab_1cpu_2026_07_31_is_pinned_and_secret_safe() -> None:
    artifact_path = (
        evidence.REPO_ROOT / "benchmark-data/modal-subprocess-runner-ab-1cpu-2026-07-31.json"
    )
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

    baseline_path = evidence.REPO_ROOT / "benchmark-data/modal-subprocess-runner-ab-2026-07-30.json"
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
        raw_path = evidence.REPO_ROOT / block[backend]["raw_artifact"]
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
    raw_directory = evidence.REPO_ROOT / artifact["provenance"]["raw_artifact_directory"]
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

