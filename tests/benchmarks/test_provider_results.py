from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from modal_computer_use import cli
from modal_computer_use.benchmarks.provider_results import (
    OPAQUE_TZAFON_SETTLE_SENTENCE,
    ProviderResultsError,
    build_provider_results,
    render_provider_results_json,
    render_provider_results_markdown,
    validate_provider_results,
)

HARNESS_SHA = "c6afa9d0384e664f5ccd7426014dcf5d20266e0e"


def _case(value: float, *, iterations: int) -> dict[str, object]:
    return {
        "status": "ok",
        "iterations": iterations,
        "successful_iterations": iterations,
        "samples_ms": [value] * (iterations - 1) + [value + 2],
        "summary_ms": {"p50": value, "p95": value + 1},
        "failures": [],
    }


def _provider_artifact() -> dict[str, object]:
    providers = {}
    for index, name in enumerate(("modal-daemon", "daytona", "e2b", "tzafon"), 1):
        cases = {
            "product_create_to_first_screenshot": _case(index * 1000.0, iterations=3),
            "screenshot_full": _case(index * 10.0, iterations=3),
            "coordinate_click": {
                **_case(index * 11.0, iterations=3),
                "benchmark_semantics": "coordinate-click-v1",
            },
            "coordinate_click_sequence": {
                **_case(index * 12.0, iterations=3),
                "benchmark_semantics": "coordinate-click-v1",
            },
            "type_100_chars": _case(index * 13.0, iterations=3),
            "type_1000_chars": _case(index * 14.0, iterations=3),
            "command_nonlogin_shell_echo": {
                **_case(index * 15.0, iterations=3),
                "benchmark_semantics": "shell-command-echo-v2",
                "shell_mode": "non_login",
            },
        }
        providers[name] = {
            "status": "ok",
            "provider": name,
            "failures": [],
            "cases": cases,
            "verification": {
                "cursor_position": {"status": "ok"},
                "type_text": {"status": "ok"},
            },
            "metadata": {
                "daytona": {
                    "api_url": None,
                    "snapshot": None,
                    "target": None,
                    "sandbox_source": "default_snapshot",
                    "sdk_package": "daytona",
                    "sdk_version": "0.175.0",
                },
                "e2b": {
                    "template": None,
                    "template_source": "default_desktop",
                    "sdk_package": "e2b-desktop",
                    "sdk_version": "2.3.1",
                },
                "tzafon": {
                    "api_origin": None,
                    "computer_kind": "desktop",
                    "persistent": False,
                    "sdk_retry_policy": "provider_default",
                    "sdk_max_retries": 2,
                    "sdk_package": "tzafon",
                    "sdk_version": "2.44.1",
                },
            }.get(name, {}),
        }
    return {
        "ok": True,
        "benchmark": "provider-compare",
        "iterations": 3,
        "failures": [],
        "metadata": {
            "providers": ["modal-daemon", "daytona", "e2b", "tzafon"],
            "environment": {
                "browser": "chromium",
                "modal_ingress": "attested-tunnel",
                "daemon_http_version": "1.1",
                "resource_profile": "browser",
                "input_rate_limit_per_sec": 0,
                "subprocess_backend": "isolated-asyncio",
                "provenance": {
                    "git_revision": HARNESS_SHA,
                    "git_worktree_clean": True,
                    "region": "provider-default",
                    "resolved_resources": {
                        "cpu": {
                            "requested": None,
                            "resolved": None,
                            "status": "provider_default_unavailable",
                        },
                        "memory": {
                            "requested": None,
                            "resolved": None,
                            "status": "provider_default_unavailable",
                            "unit": "GiB",
                        },
                        "gpu": {
                            "requested": None,
                            "resolved": None,
                            "status": "provider_default_unavailable",
                        },
                    },
                },
            },
        },
        "providers": providers,
        "provenance": {
            "harness_commit": HARNESS_SHA,
            "harness_state": "clean",
            "status": "current_reference",
        },
    }


def _optimized_artifact() -> dict[str, object]:
    cases = {
        name: {
            **_case(float(index), iterations=30),
            **(
                {"input_backends": ["xtest"]}
                if name
                in {
                    "coordinate_click",
                    "coordinate_click_sequence",
                    "type_100_chars",
                    "type_1000_chars",
                }
                else {}
            ),
            **(
                {"benchmark_semantics": "coordinate-click-v1"}
                if name in {"coordinate_click", "coordinate_click_sequence"}
                else {}
            ),
            **(
                {"benchmark_semantics": "shell-command-echo-v2", "shell_mode": "non_login"}
                if name == "command_nonlogin_shell_echo"
                else {}
            ),
        }
        for index, name in enumerate(
            (
                "screenshot_full",
                "coordinate_click",
                "coordinate_click_sequence",
                "type_100_chars",
                "type_1000_chars",
                "command_nonlogin_shell_echo",
            ),
            1,
        )
    }
    environment = {
        "modal_region": "us-west-2",
        "modal_runner_path": "connect",
        "modal_ingress": "connect",
        "daemon_http_version": "1.1",
        "browser": "chromium",
        "input_rate_limit_per_sec": 0,
        "subprocess_backend": "isolated-asyncio",
        "provenance": {"git_revision": HARNESS_SHA, "git_worktree_clean": True},
    }
    run = {
        "ok": True,
        "failures": [],
        "metadata": {
            "environment": environment,
            "runner_preflight": {"status": "ok"},
        },
        "surfaces": {
            "daemon-http": {
                "status": "ok",
                "failures": [],
                "cases": cases,
                "verification": {
                    "cursor_position": {"status": "ok"},
                    "type_text": {"status": "ok"},
                },
            }
        },
    }
    return {
        "ok": True,
        "benchmark": "modal-colocated-client",
        "iterations": 30,
        "failures": [],
        "metadata": {
            "primary_runner_path": "connect",
            "runner_paths": ["connect"],
            "surfaces": ["daemon-http"],
            "external_caller_included": False,
        },
        "runs": {"modal_colocated_runner": run},
    }


def _observation_artifact() -> dict[str, object]:
    case = {
        **_case(7.5, iterations=30),
        "benchmark_semantics": "first-hash-confirmed-change-v1",
        "metric": "action_to_first_hash_confirmed_change_ms",
        "experimental": True,
        "change_timeout_ms": 200,
        "replacement_samples": 0,
        "last_result": {
            "input_backend": "xtest",
            "action_result": {"ok": True},
            "change_result": {
                "detected": True,
                "timeout_reached": False,
                "baseline_source_sha256": "b" * 64,
                "source_sha256": "c" * 64,
            },
        },
    }
    return {
        "ok": True,
        "benchmark": "modal-colocated-client",
        "iterations": 30,
        "failures": [],
        "metadata": {
            "primary_runner_path": "connect",
            "runner_paths": ["connect"],
            "surfaces": ["daemon-observation-stream"],
            "external_caller_included": False,
            "modal_region": "us-west-2",
            "modal_ingress": "connect",
            "daemon_http_version": "1.1",
        },
        "runs": {
            "modal_colocated_runner": {
                "ok": True,
                "iterations": 30,
                "failures": [],
                "metadata": {
                    "environment": {
                        "modal_region": "us-west-2",
                        "modal_runner_path": "connect",
                        "modal_ingress": "connect",
                        "daemon_http_version": "1.1",
                        "browser": "chromium",
                        "input_rate_limit_per_sec": 0,
                        "subprocess_backend": "isolated-asyncio",
                        "provenance": {
                            "git_revision": HARNESS_SHA,
                            "git_worktree_clean": True,
                        },
                    }
                },
                "surfaces": {
                    "daemon-observation-stream": {
                        "status": "ok",
                        "failures": [],
                        "cases": {"observation_action_click_observe_change_http_raw": case},
                    }
                },
            }
        },
    }


def _build() -> dict[str, object]:
    inputs = (_provider_artifact(), _optimized_artifact(), _observation_artifact())
    raw = tuple(json.dumps(item, sort_keys=True).encode() for item in inputs)
    return build_provider_results(
        *inputs,
        input_sha256=tuple(hashlib.sha256(item).hexdigest() for item in raw),
        report_source_sha=HARNESS_SHA,
        evidence_harness_sha=HARNESS_SHA,
    )


def test_builder_renders_exact_headline_order_and_one_modal_experiment() -> None:
    result = _build()
    markdown = render_provider_results_markdown(result)

    assert list(result["headline"]["columns"]) == [
        "Modal optimized",
        "Modal default",
        "Daytona default",
        "E2B default",
        "Tzafon default",
    ]
    assert [row["label"] for row in result["headline"]["rows"]] == [
        "Product create to validated screenshot",
        "Full screenshot native/default",
        "One coordinate click",
        "Four coordinate clicks",
        "Type 100",
        "Type 1000",
        "Non-login shell command",
    ]
    assert markdown.count("| Case | Modal optimized |") == 1
    assert markdown.count("## Modal-only experimental result") == 1
    assert OPAQUE_TZAFON_SETTLE_SENTENCE in markdown
    assert "Tzafon experimental" not in markdown
    assert "not measured" in markdown
    for banned in (
        "canoni" + "cal protocol",
        "porta" + "ble",
        "fastest" + "_setup",
        "same" + "_task",
        "sdk" + "_default",
        "claim" + "_check",
    ):
        assert banned not in markdown.lower()
    assert [item["sha256"] for item in result["provenance"]["inputs"]] == [
        hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
        for item in (_provider_artifact(), _optimized_artifact(), _observation_artifact())
    ]
    assert render_provider_results_json(result).endswith("\n")


def test_reporting_policy_keeps_small_sample_p95_machine_only() -> None:
    result = _build()
    policy = result["reporting_policy"]
    assert policy == {
        "small_sample_threshold": 20,
        "small_sample_display": "median [observed min–max]",  # noqa: RUF001
        "large_sample_display": "p50 / p95",
        "p50_method": "statistics.median",
        "p95_method": (
            "linear interpolation on sorted values at rank 0.95*(n-1)"
        ),
    }
    default_value = result["headline"]["rows"][1]["values"]["modal-daemon"]
    assert set(default_value) == {
        "status",
        "sample_count",
        "min_ms",
        "max_ms",
        "p50_ms",
        "p95_ms",
    }
    markdown = render_provider_results_markdown(result)
    assert "10.00 [10.00–12.00]" in markdown  # noqa: RUF001
    assert "10.00 / 11.00" not in markdown
    assert default_value["p95_ms"] == pytest.approx(11.8)


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda p, _o, _x: p.update(ok=False), "provider artifact"),
        (
            lambda p, _o, _x: p["provenance"].update(harness_commit="a" * 40),
            "harness commit",
        ),
        (
            lambda p, _o, _x: p["providers"]["e2b"]["cases"]["coordinate_click"].update(
                successful_iterations=2
            ),
            "3/3",
        ),
        (
            lambda p, _o, _x: p["providers"]["daytona"]["verification"]["type_text"].update(
                status="failed"
            ),
            "readback",
        ),
        (lambda _p, o, _x: o["metadata"].update(primary_runner_path="inherited"), "Connect"),
        (
            lambda _p, o, _x: o["metadata"].update(external_caller_included=True),
            "runner-only",
        ),
        (
            lambda _p, _o, x: x["metadata"].update(external_caller_included=True),
            "runner-only",
        ),
        (
            lambda _p, _o, x: x["runs"]["modal_colocated_runner"]["surfaces"][
                "daemon-observation-stream"
            ]["cases"]["observation_action_click_observe_change_http_raw"].update(
                replacement_samples=1
            ),
            "replacement",
        ),
        (
            lambda _p, _o, x: x["runs"]["modal_colocated_runner"]["surfaces"][
                "daemon-observation-stream"
            ]["cases"]["observation_action_click_observe_change_http_raw"]["last_result"][
                "change_result"
            ].update(timeout_reached=True),
            "timeout",
        ),
        (
            lambda _p, _o, x: x["runs"]["modal_colocated_runner"]["surfaces"][
                "daemon-observation-stream"
            ]["cases"]["observation_action_click_observe_change_http_raw"]["last_result"][
                "action_result"
            ].update(ok=False),
            "action failed",
        ),
        (
            lambda _p, _o, x: x["runs"]["modal_colocated_runner"]["surfaces"][
                "daemon-observation-stream"
            ]["cases"]["observation_action_click_observe_change_http_raw"]["last_result"].update(
                input_backend="xdotool"
            ),
            "XTest",
        ),
        (
            lambda p, _o, _x: p["providers"]["modal-daemon"]["cases"]["screenshot_full"][
                "samples_ms"
            ].__setitem__(0, float("nan")),
            "sample",
        ),
        (
            lambda p, _o, _x: p["providers"]["modal-daemon"]["cases"]["screenshot_full"][
                "summary_ms"
            ].update(p95=1.0),
            "p95",
        ),
        (
            lambda _p, o, _x: o["runs"]["modal_colocated_runner"]["metadata"]["environment"][
                "provenance"
            ].update(git_worktree_clean=False),
            "clean",
        ),
        (
            lambda _p, o, _x: o["runs"]["modal_colocated_runner"]["metadata"]["environment"][
                "provenance"
            ].update(git_revision="a" * 40),
            "optimized source",
        ),
        (
            lambda _p, _o, x: x["runs"]["modal_colocated_runner"]["metadata"]["environment"][
                "provenance"
            ].update(git_worktree_clean=False),
            "clean",
        ),
        (
            lambda _p, _o, x: x["runs"]["modal_colocated_runner"]["metadata"]["environment"][
                "provenance"
            ].update(git_revision="a" * 40),
            "observation source",
        ),
    ],
)
def test_builder_rejects_ineligible_inputs(mutator, match: str) -> None:
    provider, optimized, observation = (
        _provider_artifact(),
        _optimized_artifact(),
        _observation_artifact(),
    )
    mutator(provider, optimized, observation)
    with pytest.raises(ProviderResultsError, match=match):
        build_provider_results(
            provider,
            optimized,
            observation,
            input_sha256=("1" * 64, "2" * 64, "3" * 64),
            report_source_sha=HARNESS_SHA,
            evidence_harness_sha=HARNESS_SHA,
        )


def test_builder_allows_report_source_to_follow_evidence_commit() -> None:
    result = build_provider_results(
        _provider_artifact(),
        _optimized_artifact(),
        _observation_artifact(),
        input_sha256=("1" * 64, "2" * 64, "3" * 64),
        report_source_sha="a" * 40,
        evidence_harness_sha=HARNESS_SHA,
    )
    assert result["provenance"]["report_source_sha"] == "a" * 40
    assert result["provenance"]["evidence_harness_sha"] == HARNESS_SHA


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda p: p["provenance"].update(status="historical"), "current_reference"),
        (lambda p: p["provenance"].update(harness_state="dirty"), "clean"),
        (lambda p: p["metadata"].update(providers=["modal-daemon"]), "provider list"),
        (
            lambda p: p["metadata"]["environment"].update(modal_ingress="connect"),
            "attested-tunnel",
        ),
        (
            lambda p: p["metadata"]["environment"]["provenance"].update(git_worktree_clean=False),
            "clean",
        ),
        (
            lambda p: p["metadata"]["environment"]["provenance"].update(region="us-west-2"),
            "provider-default",
        ),
        (
            lambda p: p["metadata"]["environment"]["provenance"]["resolved_resources"][
                "cpu"
            ].update(requested=4.0, resolved=4.0, status="explicit"),
            "resources",
        ),
        (
            lambda p: p["providers"]["daytona"]["metadata"].update(snapshot="custom"),
            "Daytona",
        ),
        (
            lambda p: p["providers"]["e2b"]["metadata"].update(template="custom"),
            "E2B",
        ),
        (
            lambda p: p["providers"]["tzafon"]["metadata"].update(sdk_max_retries=3),
            "Tzafon",
        ),
    ],
)
def test_builder_rejects_nondefault_provider_evidence(mutator, match: str) -> None:
    provider = _provider_artifact()
    mutator(provider)
    with pytest.raises(ProviderResultsError, match=match):
        build_provider_results(
            provider,
            _optimized_artifact(),
            _observation_artifact(),
            input_sha256=("1" * 64, "2" * 64, "3" * 64),
            report_source_sha=HARNESS_SHA,
            evidence_harness_sha=HARNESS_SHA,
        )


def test_builder_rejects_observation_outside_exact_topology() -> None:
    observation = _observation_artifact()
    observation["metadata"]["modal_region"] = "us-east"
    with pytest.raises(ProviderResultsError, match="modal_region"):
        build_provider_results(
            _provider_artifact(),
            _optimized_artifact(),
            observation,
            input_sha256=("1" * 64, "2" * 64, "3" * 64),
            report_source_sha=HARNESS_SHA,
            evidence_harness_sha=HARNESS_SHA,
        )


@pytest.mark.parametrize("artifact_name", ["optimized", "observation"])
@pytest.mark.parametrize("contradiction", ["external_run", "comparison", "metadata_comparison"])
def test_builder_rejects_structurally_external_runner_only_evidence(
    artifact_name: str,
    contradiction: str,
) -> None:
    optimized = _optimized_artifact()
    observation = _observation_artifact()
    artifact = optimized if artifact_name == "optimized" else observation
    if contradiction == "external_run":
        artifact["runs"]["external_caller"] = {"ok": True}
    elif contradiction == "comparison":
        artifact["comparison"] = {"ratio": 1.0}
    else:
        artifact["metadata"]["comparison"] = "external versus runner"

    with pytest.raises(ProviderResultsError, match="runner-only"):
        build_provider_results(
            _provider_artifact(),
            optimized,
            observation,
            input_sha256=("1" * 64, "2" * 64, "3" * 64),
            report_source_sha=HARNESS_SHA,
            evidence_harness_sha=HARNESS_SHA,
        )


@pytest.mark.parametrize(
    "bad", [{"token": "secret"}, {"endpoint": "https://x.test"}, {"resource_id": "sb-1"}]
)
def test_combined_artifact_rejects_secret_or_resource_fields(bad: dict[str, str]) -> None:
    result = _build()
    result["provenance"].update(bad)
    with pytest.raises(ProviderResultsError):
        validate_provider_results(result)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda r: r.update(status="draft"),
        lambda r: r.update(extra="value"),
        lambda r: r["sample_counts"].update(experiment=29),
        lambda r: r["headline"]["rows"][1]["values"]["modal_optimized"].update(p50_ms=float("nan")),
        lambda r: r["headline"]["rows"][1]["values"]["modal_optimized"].update(p95_ms=float("inf")),
        lambda r: r["headline"]["rows"][1]["values"]["modal_optimized"].update(p50_ms=-1),
        lambda r: r["headline"]["rows"][1]["values"]["modal_optimized"].update(p50_ms=10, p95_ms=9),
        lambda r: r["experiment"].update(iterations=29, successful_iterations=29),
        lambda r: r["experiment"].update(replacement_samples=1),
        lambda r: r["boundaries"].update(extra_url="https://unsafe.example"),
    ],
)
def test_combined_validator_rejects_contradictions(mutator) -> None:
    result = _build()
    mutator(result)
    with pytest.raises(ProviderResultsError):
        validate_provider_results(result)


def test_sanitizer_source_verification_rejects_unrelated_commit(monkeypatch) -> None:
    script = Path("scripts/sanitize_provider_results.py")
    spec = importlib.util.spec_from_file_location("sanitize_provider_results_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls: list[tuple[str, ...]] = []

    def fake_git_output(*args: str) -> str:
        calls.append(args)
        if args == ("rev-parse", "HEAD"):
            return "b" * 40
        raise subprocess.CalledProcessError(1, ["git", *args])

    monkeypatch.setattr(module, "_git_output", fake_git_output)
    with pytest.raises(RuntimeError, match="minimum eligible"):
        module._verify_source_revisions("b" * 40, "a" * 40)
    assert calls[-1] == (
        "merge-base",
        "--is-ancestor",
        HARNESS_SHA,
        "a" * 40,
    )


def test_sanitizer_source_verification_rejects_evidence_after_report(monkeypatch) -> None:
    script = Path("scripts/sanitize_provider_results.py")
    spec = importlib.util.spec_from_file_location("sanitize_provider_results_ancestry_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def fake_git_output(*args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return "b" * 40
        if args == ("merge-base", "--is-ancestor", HARNESS_SHA, HARNESS_SHA):
            return ""
        raise subprocess.CalledProcessError(1, ["git", *args])

    monkeypatch.setattr(module, "_git_output", fake_git_output)
    with pytest.raises(RuntimeError, match="ancestor of the report source"):
        module._verify_source_revisions("b" * 40, HARNESS_SHA)


def test_sanitizer_source_verification_rejects_dirty_tracked_worktree(monkeypatch) -> None:
    script = Path("scripts/sanitize_provider_results.py")
    spec = importlib.util.spec_from_file_location("sanitize_provider_results_dirty_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def fake_git_output(*args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return HARNESS_SHA
        if args[:2] == ("merge-base", "--is-ancestor"):
            return ""
        if args == ("status", "--porcelain", "--untracked-files=no"):
            return " M tracked.py"
        raise AssertionError(args)

    monkeypatch.setattr(module, "_git_output", fake_git_output)
    with pytest.raises(RuntimeError, match="clean tracked worktree"):
        module._verify_source_revisions(HARNESS_SHA, HARNESS_SHA)


def test_sanitizer_check_accepts_clean_descendant_of_report_source(monkeypatch) -> None:
    script = Path("scripts/sanitize_provider_results.py")
    spec = importlib.util.spec_from_file_location("sanitize_provider_results_check_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report_sha = "b" * 40
    head_sha = "c" * 40
    calls: list[tuple[str, ...]] = []

    def fake_git_output(*args: str) -> str:
        calls.append(args)
        if args == ("rev-parse", "HEAD"):
            return head_sha
        if args[:2] == ("merge-base", "--is-ancestor"):
            return ""
        if args == ("status", "--porcelain", "--untracked-files=no"):
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(module, "_git_output", fake_git_output)
    module._verify_source_revisions(report_sha, HARNESS_SHA, check=True)
    assert ("merge-base", "--is-ancestor", report_sha, head_sha) in calls


def test_sanitizer_check_rejects_report_source_not_ancestor_of_head(monkeypatch) -> None:
    script = Path("scripts/sanitize_provider_results.py")
    spec = importlib.util.spec_from_file_location(
        "sanitize_provider_results_check_ancestry_test", script
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report_sha = "b" * 40
    head_sha = "c" * 40

    def fake_git_output(*args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return head_sha
        if args in {
            ("merge-base", "--is-ancestor", HARNESS_SHA, HARNESS_SHA),
            ("merge-base", "--is-ancestor", HARNESS_SHA, report_sha),
        }:
            return ""
        raise subprocess.CalledProcessError(1, ["git", *args])

    monkeypatch.setattr(module, "_git_output", fake_git_output)
    with pytest.raises(RuntimeError, match="report source must be an ancestor of HEAD"):
        module._verify_source_revisions(report_sha, HARNESS_SHA, check=True)


def test_offline_cli_renders_markdown_json_and_output(tmp_path: Path, capsys) -> None:
    artifact = tmp_path / "combined.json"
    artifact.write_text(render_provider_results_json(_build()), encoding="utf-8")
    assert cli.main(["benchmark", "provider-results", str(artifact), "--format", "markdown"]) == 0
    assert "Modal optimized" in capsys.readouterr().out
    output = tmp_path / "out.json"
    assert (
        cli.main(
            [
                "benchmark",
                "provider-results",
                str(artifact),
                "--format",
                "json",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert json.loads(output.read_text())["schema_version"] == 2
    artifact.write_text("{}", encoding="utf-8")
    with pytest.raises((ProviderResultsError, SystemExit)):
        cli.main(["benchmark", "provider-results", str(artifact)])


def test_pre_fix_experiment_is_rejected() -> None:
    observation = _observation_artifact()
    case = observation["runs"]["modal_colocated_runner"]["surfaces"]["daemon-observation-stream"][
        "cases"
    ]["observation_action_click_observe_change_http_raw"]
    del case["benchmark_semantics"]
    with pytest.raises(ProviderResultsError, match="semantics"):
        build_provider_results(
            _provider_artifact(),
            _optimized_artifact(),
            observation,
            input_sha256=("1" * 64, "2" * 64, "3" * 64),
            report_source_sha=HARNESS_SHA,
            evidence_harness_sha=HARNESS_SHA,
        )


def test_sanitizer_generation_is_deterministic_and_check_detects_drift(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = [tmp_path / name for name in ("provider.json", "optimized.json", "observation.json")]
    for path, payload in zip(
        paths,
        (_provider_artifact(), _optimized_artifact(), _observation_artifact()),
        strict=True,
    ):
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    output = tmp_path / "combined.json"
    argv = [
        *(str(path) for path in paths),
        str(output),
        "--report-source-sha",
        HARNESS_SHA,
        "--evidence-harness-sha",
        HARNESS_SHA,
    ]

    script = Path("scripts/sanitize_provider_results.py")
    spec = importlib.util.spec_from_file_location(
        "sanitize_provider_results_generation_test", script
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "_verify_source_revisions",
        lambda _report_sha, _evidence_sha, **_kwargs: None,
    )

    assert module.main(argv) == 0
    first = output.read_bytes()
    assert module.main(argv) == 0
    assert output.read_bytes() == first
    assert module.main([*argv, "--check"]) == 0
    output.write_text("{}\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        module.main([*argv, "--check"])
