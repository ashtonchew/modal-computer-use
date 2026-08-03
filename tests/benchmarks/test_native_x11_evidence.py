from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path

import evidence_assertions as evidence
import pytest


def test_native_x11_replication_is_recomputable_and_non_superseding() -> None:
    path, artifact = evidence.load_benchmark_artifact(
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
    report_path = evidence.REPO_ROOT / artifact["historical_context"]["source_report"]
    assert (
        hashlib.sha256(report_path.read_bytes()).hexdigest()
        == artifact["historical_context"]["source_report_sha256"]
    )

    for arm in artifact["arms"].values():
        assert arm["verification"]["cursor_position"]["status"] == "ok"
        assert arm["verification"]["type_text"]["status"] == "ok"
        for case in cases:
            measured = arm["cases"][case]
            evidence.assert_recomputed_summary(measured["samples_ms"], measured["summary_ms"])
            evidence.assert_recomputed_summary(
                measured["daemon_samples_ms"], measured["daemon_summary_ms"]
            )

        raw_path = evidence.REPO_ROOT / arm["raw_artifact"]
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

    evidence.assert_artifact_secret_safe(path)


def test_native_x11_runner_matrix_recomputes_samples_effects_and_preregistered_gates() -> None:
    path, artifact = evidence.load_benchmark_artifact(
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
        "b01-xtest-asyncio": ("b4d52bb9cb28d7dfc379c77b01872934d6b071f83cd4bb0fbc6a32eff925da03"),
        "b01-xdotool-asyncio": ("071afb532347c201e5542c82a911a1c506289835ca7958c8b8cc6eef06d9fbb4"),
        "b01-xtest-isolated-asyncio": (
            "887dfb21f67bd2aa0e2439274739e6bb830c98b5e30505cafd2d4c6b3d73a001"
        ),
        "b02-xdotool-asyncio": ("65cbbd0db4cff0b03bb4d2a82629220f359e8583c2e7e2be7dc1d45b7d5b0e64"),
        "b02-xdotool-isolated-asyncio": (
            "9f83dcf8aa7a0e7583a8f7c94830a587d8e4730aa7982df7c58e8dc0f2cd041d"
        ),
        "b02-xtest-asyncio": ("a2dfa1d05db64d0ab9da24b4f1102c8a03c98280279941692010aa5a9c408e8c"),
        "b02-xtest-isolated-asyncio": (
            "b274968e8e27bcc469da25d9ef02d23bfa3b6736326a1739eedd56057a6060ad"
        ),
        "b03-xtest-asyncio": ("4e128feee40ca8cdd3c75ca05f6443bfaec62972bb93da609cb1ec315449fcf7"),
        "b03-xdotool-asyncio": ("7625f508c7040f20ddbb7b8924afdb82a7985f23920a5eeba3bac9e920a2682d"),
        "b03-xtest-isolated-asyncio": (
            "3d7732cbb8d302ac8919d03da6d6d4b3f90a87c3a6d8e95ace15a2156f7e2b68"
        ),
        "b03-xdotool-isolated-asyncio": (
            "337b9fa92ab47288b1d5a4e60720a3aa200c2bf18241fadfebcddfd2afe6b691"
        ),
    }

    assert "canonical_evidence" not in artifact
    claim = artifact["runner_effect_claim"]
    assert claim["role"] == "primary_evidence_entry_point"
    assert claim["entry_point"] == ("benchmark-data/modal-native-x11-runner-matrix-2026-08-02.json")
    assert claim["fresh_clone_capabilities"] == {
        "controlled_matrix_recomputable_from_tracked_samples": True,
        "historical_aggregate_exactly_quotable": True,
        "historical_raw_samples_recomputable": False,
        "linked_dependencies_digest_verified": True,
    }
    assert claim["supersedes_dated_measurements"] is False
    dependency_roles = {
        dependency["role"]: dependency["path"] for dependency in claim["dependencies"]
    }
    assert dependency_roles == {
        "historical source and aggregate provenance": (
            "benchmark-data/modal-native-x11-historical-source-2026-07-23.json"
        ),
        "clean isolated-runner replication with tracked samples": (
            "benchmark-data/modal-native-x11-backend-ab-replication-2026-08-02.json"
        ),
        "independent subprocess-runner mechanism control": (
            "benchmark-data/modal-subprocess-runner-ab-1cpu-2026-07-31.json"
        ),
        "bounded interpretation and primary-source citations": (
            "research/native-x11-latency-discrepancy-2026-08-02.md"
        ),
    }
    for dependency in claim["dependencies"]:
        dependency_path = evidence.REPO_ROOT / dependency["path"]
        assert hashlib.sha256(dependency_path.read_bytes()).hexdigest() == dependency["sha256"]
    assert claim["claim"].endswith("separate dated measurements.")

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
    runner = evidence.REPO_ROOT / artifact["provenance"]["source"]["runner"]["path"]
    assert (
        hashlib.sha256(runner.read_bytes()).hexdigest()
        == artifact["provenance"]["source"]["runner"]["sha256"]
    )
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

        raw_path = evidence.REPO_ROOT / cell["raw_artifact"]
        if not raw_path.exists():
            raw_path = (
                Path("/private/tmp/native-x11-runner-matrix-2026-08-02/raw")
                / Path(cell["raw_artifact"]).name
            )
        if raw_path.exists():
            assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == cell["raw_artifact_sha256"]
            raw_cases = json.loads(raw_path.read_text())["surfaces"]["daemon-http"]["cases"]
            for case in cases:
                assert cell["cases"][case]["wall_samples_ms"] == raw_cases[case]["samples_ms"]
                assert (
                    cell["cases"][case]["daemon_samples_ms"] == raw_cases[case]["daemon_samples_ms"]
                )

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
                assert block_effect["wall_mean_delta_shared_minus_isolated_ms"] == pytest.approx(
                    shared["cases"][case]["wall_mean_ms"] - isolated["cases"][case]["wall_mean_ms"],
                    abs=1e-12,
                )
                assert block_effect["daemon_mean_delta_shared_minus_isolated_ms"] == pytest.approx(
                    shared["cases"][case]["daemon_mean_ms"]
                    - isolated["cases"][case]["daemon_mean_ms"],
                    abs=1e-12,
                )
            shared = artifact["aggregates"][f"{input_backend}/asyncio"]["cases"][case]
            isolated = artifact["aggregates"][f"{input_backend}/isolated-asyncio"]["cases"][case]
            assert effect["pooled_wall_mean_delta_shared_minus_isolated_ms"] == pytest.approx(
                shared["pooled_wall_mean_ms"] - isolated["pooled_wall_mean_ms"], abs=1e-12
            )
            assert effect["pooled_daemon_mean_delta_shared_minus_isolated_ms"] == pytest.approx(
                shared["pooled_daemon_mean_ms"] - isolated["pooled_daemon_mean_ms"],
                abs=1e-12,
            )

    gates = artifact["gates"]
    assert gates["preregistered_before_results"] is True
    assert gates["validity"]["passed"] is all(
        item["measurements_30_of_30"] and item["cursor_and_type_verification_succeeded"]
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
        assert block["two_launch_delta_per_launch_ms"] == pytest.approx(move_delta / 2, abs=1e-12)
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
        isolated = artifact["aggregates"]["xtest/isolated-asyncio"]["cases"]["move_click"][
            "per_block"
        ][block["block"] - 1]["daemon_mean_ms"]
        absolute = abs(effect["daemon_mean_delta_shared_minus_isolated_ms"])
        relative = absolute / isolated
        assert block["absolute_daemon_mean_delta_ms"] == pytest.approx(absolute, abs=1e-12)
        assert block["relative_to_isolated"] == pytest.approx(relative, abs=1e-12)
        assert block["passed"] is (absolute <= 1.0 or relative <= 0.25)
    assert gates["xtest_negative_control"]["passed"] is all(
        block["passed"] for block in gates["xtest_negative_control"]["blocks"]
    )

    historical = gates["historical_magnitude"]
    pooled_shared_xdotool = artifact["aggregates"]["xdotool/asyncio"]["cases"]["move_click"][
        "pooled_daemon_mean_ms"
    ]
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
    evidence.assert_artifact_secret_safe(path)


def test_native_x11_historical_source_manifest_binds_provenance_and_claims() -> None:
    path, manifest = evidence.load_benchmark_artifact(
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
        (parent["role"], parent["commit"], parent["tree"]) for parent in snapshot["parents"]
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
    report_path = evidence.REPO_ROOT / report["path"]
    report_bytes = report_path.read_bytes()
    assert hashlib.sha256(report_bytes).hexdigest() == report["sha256"]
    git_blob_payload = f"blob {len(report_bytes)}\0".encode() + report_bytes
    assert hashlib.sha1(git_blob_payload, usedforsecurity=False).hexdigest() == report["git_blob"]
    assert report["claim_origin_commit"] == "3fee2c52611114c1a6598dc61889b6cff52e3ea5"
    assert report["claim_origin_blob"] == "fa63b8046a5058f98653d722f531626a4a6d5406"
    assert report["claim_origin_sha256"] == (
        "f6e9e6e6df2edd33a2fb55458c9a36d1d03b5ec605d053c112ae371dd79f761e"
    )

    assert "source_session" not in manifest
    timeline = manifest["recorded_run_timeline"]
    assert (
        timeline["events"]["comparison_recorded_at"]
        < timeline["events"]["source_stash_recorded_at"]
    )
    assert timeline["raw_record"] == {
        "tracked": False,
        "sha256": "450b642f4ae2638f2ada049dbfa42667d84d33101a0e34e2ba3c49075272f13f",
        "byte_count": 20445755,
        "line_count": 10340,
    }

    commands = manifest["measurement"]["commands"]
    assert "--input-backend xtest --iterations 3" in commands["xtest"]
    assert "--input-backend xdotool --iterations 3" in commands["xdotool"]
    assert commands["xtest"].endswith("native-x11-input-xtest.json --json")
    assert commands["xdotool"].endswith("native-x11-input-xdotool.json --json")

    columns = report["published_result_columns"]
    recorded_results = manifest["measurement"]["recorded_comparison_results"]
    for case, published_row in report["published_results"].items():
        recorded = recorded_results[case]
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


def test_native_x11_termination_reconciliation_is_pinned_complete_and_secret_safe() -> None:
    path, artifact = evidence.load_benchmark_artifact(
        "modal-native-x11-sandbox-termination-reconciliation-2026-08-03.json"
    )
    matrix_source = artifact["source_artifacts"]["matrix"]
    runner_source = artifact["source_artifacts"]["runner"]
    matrix_path = evidence.REPO_ROOT / matrix_source["path"]
    runner_path = evidence.REPO_ROOT / runner_source["path"]

    evidence.assert_sha256(matrix_path, matrix_source["sha256"])
    evidence.assert_sha256(runner_path, runner_source["sha256"])

    matrix = json.loads(matrix_path.read_text())
    reconciliation = artifact["reconciliation"]
    reconciled_cells = reconciliation["cells"]
    matrix_cell_ids = {cell["cell_id"] for cell in matrix["cells"]}
    reconciled_cell_ids = {cell["cell_id"] for cell in reconciled_cells}

    assert len(matrix_cell_ids) == 12
    assert reconciled_cell_ids == matrix_cell_ids
    assert reconciliation["expected_cell_count"] == len(matrix_cell_ids)
    assert reconciliation["private_id_count"] == 12
    assert reconciliation["private_id_unique_count"] == 12
    assert reconciliation["found_count"] == 12
    assert reconciliation["finished_count"] == 12
    assert reconciliation["running_count"] == 0
    assert reconciliation["lookup_error_count"] == 0
    assert reconciliation["exit_code_counts"] == {"137": 12}
    assert all(
        cell["state"] == "finished" and cell["exit_code"] == 137 for cell in reconciled_cells
    )

    assert artifact["runner_cleanup"]["historical_runner_detach_attempted"] is False
    assert artifact["runner_cleanup"]["recorded_operations"] == [
        {"operation": "ComputerSandbox.terminate", "wait": True},
        {"operation": "DaemonClient.close"},
    ]
    assert artifact["reconciliation_scope"] == {
        "modal_control_plane_reconciled": True,
        "audit_log_reconciled": False,
        "billing_reconciled": False,
    }
    assert artifact["secret_safety"]["secret_safe_projection"] is True
    assert reconciliation["private_ids_tracked"] is False

    serialized = path.read_text().lower()
    assert serialized.count("https://") == 1
    assert "https://modal.com/docs/guide/sandboxes#return-codes" in serialized
    for forbidden in (
        "modal.host",
        "sb-",
        'sandbox_id"',
        'run_id"',
        "access_token",
        "api_key",
        "bearer ",
        "base_url",
        'endpoint"',
        "/users/",
    ):
        assert forbidden not in serialized
