from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from typing import Any
from urllib.parse import urlsplit

from .modal_optimized_provider import (
    PRODUCT_CREATE_CASE,
    validate_modal_optimized_provider_artifact,
)

SCHEMA_VERSION = 4
SANITIZED_MODAL_INPUT_SCHEMA_VERSION = 1
MINIMUM_ELIGIBLE_SOURCE_SHA = "e57ea35f04efdec4100ffa44196ee8599e9811b2"
OPAQUE_TZAFON_SETTLE_SENTENCE = (
    "Tzafon settle semantics are opaque at this API boundary, so its action "
    "acknowledgement is not treated as equivalent to Modal’s hash-confirmed first visual change."  # noqa: RUF001
)
NATIVE_SCREENSHOT_CAVEAT = (
    "Full screenshots use each provider's native/default format and are not "
    "pixel- or codec-normalized."
)
OBSERVED_SCREENSHOT_BOUNDARY = (
    "Observed native/default screenshots were Tzafon 1280x720 JPEG and Modal, Daytona, and E2B "
    "1024x768 PNG."
)
CHANGE_TIMEOUT_BOUNDARY = (
    "The 200 ms change timeout is the maximum wait for a hash-confirmed first visual change, "
    "not a fixed wait, settle period, or application-readiness signal."
)
SHELL_LATENCY_BOUNDARY = (
    "Shell latency covers transport, authentication, request handling and admission, process "
    "spawn, output collection, process wait, cleanup, and exact-output validation."
)
SUBPROCESS_BACKEND_BOUNDARY = (
    "isolated-asyncio affects only subprocess-backed command and compatibility paths; it does "
    "not select the native input or screenshot implementation."
)
TYPING_DEFAULT_BOUNDARY = (
    "Modal default typing requests the public TypeAction defaults (auto with a 10 ms character "
    "delay); for these 100- and 1000-character inputs auto resolves to clipboard, so that delay "
    "is not applied per character. Modal optimized explicitly resolves to keystrokes with zero "
    "delay. Modal default uses 1.05 seconds of untimed pacing before every warmup and measured "
    "action invocation to respect the default 20-actions-per-second input limit."
)
CLEANUP_BOUNDARY = (
    "Eligibility requires successful command and top-level outcomes. Cleanup errors "
    "are terminal in the producer, but this combined artifact does not independently "
    "prove cleanup beyond those recorded outcomes."
)
PRODUCT_CREATE_BOUNDARY = (
    "The lifecycle timer starts immediately before the public create call and ends after the first "
    "full-screen image is decoded, parsed, and validated. Cleanup is outside the timer."
)
WARM_OPERATION_BOUNDARY = (
    "Warm-operation timers measure the selected public SDK or daemon request from the caller. "
    "They exclude target creation and cleanup."
)
COMMAND_BOUNDARY = (
    "The command case requests argv [\"sh\", \"-c\", \"printf '42\\n'\"] with non-login "
    "shell semantics and requires exit code 0 with exact stdout \"42\\n\"."
)
LIGHTCONE_TZAFON_BOUNDARY = (
    "Lightcone is the computer infrastructure and public API; tzafon 2.44.1 is the pinned Python "
    "SDK package used for the Tzafon default column."
)
RUNNER_ONLY_BOUNDARY = (
    "Modal optimized and experimental evidence uses a Modal runner with the same requested "
    "Modal region as its target; the unrelated external caller diagnostic is not executed "
    "or included. Publishable optimized evidence separately requires every observed target "
    "cloud and region to match the runner."
)
REPORTING_POLICY = {
    "small_sample_threshold": 20,
    "small_sample_display": "median [observed min–max]",  # noqa: RUF001
    "large_sample_display": "p50 / p95",
    "p50_method": "statistics.median",
    "p95_method": "linear interpolation on sorted values at rank 0.95*(n-1)",
}

_COLUMNS = (
    ("modal_optimized", "Modal optimized"),
    ("modal-daemon", "Modal default"),
    ("daytona", "Daytona default"),
    ("e2b", "E2B default"),
    ("tzafon", "Tzafon default"),
)
_ROWS = (
    ("product_create_to_first_screenshot", "Product create to validated screenshot"),
    ("screenshot_full", "Full screenshot native/default"),
    ("coordinate_click", "One coordinate click"),
    ("coordinate_click_sequence", "Four coordinate clicks"),
    ("type_100_chars", "Type 100"),
    ("type_1000_chars", "Type 1000"),
    ("command_nonlogin_shell_echo", "Non-login shell command"),
)
_SECRET_KEYS = {
    "access_key",
    "api_key",
    "authorization",
    "artifact_bytes",
    "base_url",
    "bearer",
    "clipboard_text",
    "credential",
    "credentials",
    "endpoint",
    "password",
    "private_key",
    "resource_id",
    "run_id",
    "sandbox_id",
    "screenshot",
    "secret",
    "secret_key",
    "token",
    "typed_content",
    "typed_text",
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


class ProviderResultsError(ValueError):
    """Raised when evidence is not eligible for the provider results report."""


def build_provider_results(
    provider_artifact: dict[str, Any],
    modal_optimized_artifact: dict[str, Any],
    modal_observation_artifact: dict[str, Any],
    *,
    input_sha256: tuple[str, str, str],
    report_source_sha: str,
    evidence_harness_sha: str,
) -> dict[str, Any]:
    """Validate three source artifacts and return a secret-safe combined record."""
    _require_digest_tuple(input_sha256)
    _require_commit(report_source_sha, "report_source_sha")
    _require_commit(evidence_harness_sha, "evidence_harness_sha")

    provider_cases = _validated_provider_cases(provider_artifact)
    provider_harness_sha = _mapping(provider_artifact.get("provenance"), "provider provenance").get(
        "harness_commit"
    )
    _require_commit(provider_harness_sha, "provider harness commit")
    if provider_harness_sha != evidence_harness_sha:
        raise ProviderResultsError("provider source does not match the evidence harness commit")
    optimized_cases, optimized_config = _validated_sanitized_optimized_cases(
        modal_optimized_artifact, expected_harness_commit=evidence_harness_sha
    )
    experiment = _validated_sanitized_experiment(
        modal_observation_artifact,
        expected_harness_commit=evidence_harness_sha,
    )
    request_topology = _validated_four_click_topology(
        provider_cases, modal_optimized_artifact
    )

    rows: list[dict[str, Any]] = []
    for case_name, label in _ROWS:
        values: dict[str, Any] = {}
        for key, _column_label in _COLUMNS:
            case = (
                optimized_cases[case_name]
                if key == "modal_optimized"
                else provider_cases[key][case_name]
            )
            values[key] = _summary_value(case)
        rows.append({"case": case_name, "label": label, "values": values})

    configuration = {
        "modal_optimized": optimized_config,
        "provider_defaults": {
            "measured_iterations": 3,
            "caller_topology": "external provider SDK caller",
        },
    }
    configuration_sha256 = hashlib.sha256(
        json.dumps(configuration, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    result = {
        "schema_version": SCHEMA_VERSION,
        "benchmark": "provider-results",
        "status": "eligible",
        "evidence_date": "2026-07-26",
        "provenance": {
            "report_source_sha": report_source_sha,
            "evidence_harness_sha": evidence_harness_sha,
            "minimum_eligible_evidence_sha": MINIMUM_ELIGIBLE_SOURCE_SHA,
            "safe_configuration_sha256": configuration_sha256,
            "inputs": [
                {"role": "sanitized_provider_defaults", "sha256": input_sha256[0]},
                {"role": "sanitized_modal_optimized", "sha256": input_sha256[1]},
                {"role": "sanitized_modal_observation", "sha256": input_sha256[2]},
            ],
        },
        "configuration": configuration,
        "headline": {
            "columns": [label for _, label in _COLUMNS],
            "rows": rows,
        },
        "experiment": experiment,
        "tracked_artifacts": {
            "provider_defaults": (
                "benchmark-data/provider-compare-coordinate-command-2026-07-26.json"
            ),
            "modal_optimized": "benchmark-data/modal-optimized-provider-2026-07-26.json",
            "modal_observation": "benchmark-data/modal-observation-2026-07-26.json",
            "combined": "benchmark-data/provider-results-2026-07-26.json",
        },
        "request_topology": request_topology,
        "reporting_policy": REPORTING_POLICY,
        "boundaries": {
            "native_screenshot_caveat": NATIVE_SCREENSHOT_CAVEAT,
            "cleanup": CLEANUP_BOUNDARY,
            "product_create": PRODUCT_CREATE_BOUNDARY,
            "warm_operations": WARM_OPERATION_BOUNDARY,
            "command": COMMAND_BOUNDARY,
            "lightcone_tzafon": LIGHTCONE_TZAFON_BOUNDARY,
            "tzafon_settle": OPAQUE_TZAFON_SETTLE_SENTENCE,
            "runner_only_evidence": RUNNER_ONLY_BOUNDARY,
        },
        "sample_counts": {"provider_defaults": 3, "modal_optimized": 30, "experiment": 30},
    }
    validate_provider_results(result)
    return result


def validate_provider_results(payload: dict[str, Any]) -> None:
    _require_exact_keys(
        payload,
        {
            "schema_version",
            "benchmark",
            "status",
            "evidence_date",
            "provenance",
            "configuration",
            "headline",
            "experiment",
            "tracked_artifacts",
            "request_topology",
            "reporting_policy",
            "boundaries",
            "sample_counts",
        },
        "combined artifact",
    )
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("benchmark") != "provider-results"
    ):
        raise ProviderResultsError("combined provider results schema is unsupported")
    if payload.get("status") != "eligible":
        raise ProviderResultsError("combined provider results status must be eligible")
    if payload.get("evidence_date") != "2026-07-26":
        raise ProviderResultsError("combined provider results date is not exact")
    _validate_safe_value(payload)
    headline = _mapping(payload.get("headline"), "headline")
    _require_exact_keys(headline, {"columns", "rows"}, "headline")
    if headline.get("columns") != [label for _, label in _COLUMNS]:
        raise ProviderResultsError("headline columns are not in the required order")
    rows = headline.get("rows")
    if not isinstance(rows, list) or len(rows) != len(_ROWS):
        raise ProviderResultsError("headline rows are not in the required order")
    for index, (case_name, label) in enumerate(_ROWS):
        row = _mapping(rows[index], f"headline row {index}")
        _require_exact_keys(row, {"case", "label", "values"}, f"headline row {index}")
        if row.get("case") != case_name or row.get("label") != label:
            raise ProviderResultsError("headline rows are not in the required order")
        values = _mapping(row.get("values"), f"headline values {case_name}")
        _require_exact_keys(values, {key for key, _ in _COLUMNS}, f"headline values {case_name}")
        for provider_key, _ in _COLUMNS:
            value = _mapping(values.get(provider_key), f"headline {case_name} {provider_key}")
            _require_exact_keys(
                value,
                {"status", "sample_count", "min_ms", "max_ms", "p50_ms", "p95_ms"},
                f"headline {case_name} {provider_key}",
            )
            if value.get("status") != "measured":
                raise ProviderResultsError("headline measured values require measured status")
            expected_count = 30 if provider_key == "modal_optimized" else 3
            if value.get("sample_count") != expected_count:
                raise ProviderResultsError("headline sample count does not match its column")
            minimum = _require_finite_nonnegative(value.get("min_ms"), "headline minimum")
            maximum = _require_finite_nonnegative(value.get("max_ms"), "headline maximum")
            p50, p95 = _require_summary_order(
                value.get("p50_ms"), value.get("p95_ms"), "headline summary"
            )
            if not minimum <= p50 <= p95 <= maximum:
                raise ProviderResultsError("headline statistics must remain within observed range")
    experiment = _mapping(payload.get("experiment"), "experiment")
    _require_exact_keys(
        experiment,
        {
            "provider",
            "case",
            "benchmark_semantics",
            "metric",
            "experimental",
            "change_timeout_ms",
            "eligibility",
            "iterations",
            "successful_iterations",
            "replacement_samples",
            "p50_ms",
            "p95_ms",
        },
        "experiment",
    )
    expected_experiment = {
        "provider": "Modal",
        "case": "observation_action_click_observe_change_http_raw",
        "benchmark_semantics": "first-hash-confirmed-change-v1",
        "metric": "action_to_first_hash_confirmed_change_ms",
        "experimental": True,
        "change_timeout_ms": 200,
        "eligibility": (
            "30/30 successful actions with hash-confirmed changes and no replacement samples"
        ),
        "iterations": 30,
        "successful_iterations": 30,
        "replacement_samples": 0,
    }
    if any(experiment.get(key) != value for key, value in expected_experiment.items()):
        raise ProviderResultsError("combined artifact requires one Modal-only experiment")
    _require_summary_order(experiment.get("p50_ms"), experiment.get("p95_ms"), "experiment summary")
    if payload.get("reporting_policy") != REPORTING_POLICY:
        raise ProviderResultsError("combined artifact reporting policy is not exact")
    boundaries = _mapping(payload.get("boundaries"), "boundaries")
    expected_boundaries = {
        "native_screenshot_caveat": NATIVE_SCREENSHOT_CAVEAT,
        "cleanup": CLEANUP_BOUNDARY,
        "product_create": PRODUCT_CREATE_BOUNDARY,
        "warm_operations": WARM_OPERATION_BOUNDARY,
        "command": COMMAND_BOUNDARY,
        "lightcone_tzafon": LIGHTCONE_TZAFON_BOUNDARY,
        "tzafon_settle": OPAQUE_TZAFON_SETTLE_SENTENCE,
        "runner_only_evidence": RUNNER_ONLY_BOUNDARY,
    }
    if boundaries != expected_boundaries:
        raise ProviderResultsError("combined artifact boundaries are not exact")
    if payload.get("tracked_artifacts") != {
        "provider_defaults": "benchmark-data/provider-compare-coordinate-command-2026-07-26.json",
        "modal_optimized": "benchmark-data/modal-optimized-provider-2026-07-26.json",
        "modal_observation": "benchmark-data/modal-observation-2026-07-26.json",
        "combined": "benchmark-data/provider-results-2026-07-26.json",
    }:
        raise ProviderResultsError("combined artifact links are not exact")
    _validate_four_click_topology(payload.get("request_topology"))
    sample_counts = _mapping(payload.get("sample_counts"), "sample counts")
    if sample_counts != {"provider_defaults": 3, "modal_optimized": 30, "experiment": 30}:
        raise ProviderResultsError("combined artifact sample counts are not exact")
    provenance = _mapping(payload.get("provenance"), "provenance")
    _require_exact_keys(
        provenance,
        {
            "report_source_sha",
            "evidence_harness_sha",
            "minimum_eligible_evidence_sha",
            "safe_configuration_sha256",
            "inputs",
        },
        "provenance",
    )
    _require_commit(provenance.get("report_source_sha"), "report source SHA")
    _require_commit(provenance.get("evidence_harness_sha"), "evidence harness SHA")
    if provenance.get("minimum_eligible_evidence_sha") != MINIMUM_ELIGIBLE_SOURCE_SHA:
        raise ProviderResultsError("combined artifact has the wrong minimum evidence commit")
    configuration = _mapping(payload.get("configuration"), "configuration")
    expected_configuration = {
        "modal_optimized": {
            "modal_region": "us-west-2",
            "modal_cloud_provider": configuration.get("modal_optimized", {}).get(
                "modal_cloud_provider"
            ),
            "modal_runner_kind": "modal-function",
            "modal_ingress": "connect",
            "daemon_http_version": "1.1",
            "browser": "chromium",
            "browser_prewarm": False,
            "image_revision": provenance.get("evidence_harness_sha"),
            "runner_cpu": 4.0,
            "runner_memory_mib": 8192,
            "target_cpu": 4.0,
            "target_memory_mib": 8192,
            "input_rate_limit_per_sec": 0,
            "subprocess_backend": "isolated-asyncio",
            "measured_iterations": 30,
            "caller_topology": "single Modal Function with the same requested Modal region",
            "observed_target_placement_match": True,
            "external_caller_included": False,
            "runner_startup_in_product_create_boundary": False,
        },
        "provider_defaults": {
            "measured_iterations": 3,
            "caller_topology": "external provider SDK caller",
        },
    }
    if configuration != expected_configuration:
        raise ProviderResultsError("combined artifact configuration is not exact")
    cloud_provider = expected_configuration["modal_optimized"]["modal_cloud_provider"]
    if not isinstance(cloud_provider, str) or not cloud_provider.strip():
        raise ProviderResultsError("combined artifact requires observed Modal cloud placement")
    configuration_digest = hashlib.sha256(
        json.dumps(configuration, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    if provenance.get("safe_configuration_sha256") != configuration_digest:
        raise ProviderResultsError("safe configuration digest does not match configuration")
    inputs = provenance.get("inputs")
    expected_roles = [
        "sanitized_provider_defaults",
        "sanitized_modal_optimized",
        "sanitized_modal_observation",
    ]
    if not isinstance(inputs, list) or len(inputs) != 3:
        raise ProviderResultsError("combined artifact must bind all three inputs")
    for item, role in zip(inputs, expected_roles, strict=True):
        _require_exact_keys(_mapping(item, "provenance input"), {"role", "sha256"}, "input")
        if item.get("role") != role:
            raise ProviderResultsError("input provenance roles are not exact")
        digest = _mapping(item, "provenance input").get("sha256")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise ProviderResultsError("input provenance requires SHA-256 digests")


def render_provider_results_json(payload: dict[str, Any]) -> str:
    validate_provider_results(payload)
    return f"{json.dumps(payload, indent=2, sort_keys=True)}\n"


def render_provider_results_markdown(payload: dict[str, Any]) -> str:
    validate_provider_results(payload)
    headline = payload["headline"]
    provenance = payload["provenance"]
    policy = payload["reporting_policy"]
    boundaries = payload["boundaries"]
    artifacts = payload["tracked_artifacts"]
    topology = payload["request_topology"]
    lines = [
        f"# Provider benchmark results, {payload['evidence_date']}",
        "",
        "**Evidence status:** eligible",
        "",
        "Provider-default values are median [observed min–max] milliseconds. Modal optimized "  # noqa: RUF001
        "values are p50 / p95 milliseconds.",
        "",
        f"| Case | {' | '.join(headline['columns'])} |",
        f"| --- | {' | '.join('---:' for _ in headline['columns'])} |",
    ]
    for row in headline["rows"]:
        values = [_format_value(row["values"][key]) for key, _ in _COLUMNS]
        lines.append(f"| {row['label']} | {' | '.join(values)} |")
    experiment = payload["experiment"]
    counts = payload["sample_counts"]
    lines.extend(
        [
            "",
            "## Tzafon claim boundary",
            "",
            "[Tzafon's status post](https://x.com/tzafon_company/status/2080351293533753736) "
            "reports 63 ms for its browser, 71 ms for its desktop, and 188 ms for an E2B base "
            "sandbox. It reports the median of five runs from San Francisco and measures "
            "server-side TTFB minus the TLS handshake. Those figures are vendor-claim context. "
            "They are not compared numerically with this report's public create call through "
            "decoded and validated screenshot boundary.",
            "",
            "## Modal-only experimental result",
            "",
            "Action click to first hash-confirmed visual change: "
            f"{experiment['p50_ms']:.2f} / {experiment['p95_ms']:.2f} ms p50 / p95 "
            f"({experiment['successful_iterations']}/{experiment['iterations']}, "
            "no replacement samples).",
            "",
            boundaries["tzafon_settle"],
            "",
            CHANGE_TIMEOUT_BOUNDARY,
            "",
            "## Measurement and fairness boundaries",
            "",
            boundaries["product_create"],
            "",
            boundaries["warm_operations"],
            "",
            boundaries["command"],
            "",
            SHELL_LATENCY_BOUNDARY,
            "",
            SUBPROCESS_BACKEND_BOUNDARY,
            "",
            TYPING_DEFAULT_BOUNDARY,
            "",
            boundaries["native_screenshot_caveat"],
            "",
            OBSERVED_SCREENSHOT_BOUNDARY,
            "",
            boundaries["lightcone_tzafon"],
            "",
            f"Sample counts: provider defaults {counts['provider_defaults']}/"
            f"{counts['provider_defaults']}; Modal optimized {counts['modal_optimized']}/"
            f"{counts['modal_optimized']}; Modal experiment {counts['experiment']}/"
            f"{counts['experiment']}. The default and optimized Modal columns use explicitly "
            "different caller topologies.",
            "",
            f"p50 uses `{policy['p50_method']}`. p95 uses {policy['p95_method']}. The report "
            f"shows p95 only when the sample count is at least {policy['small_sample_threshold']}.",
            "",
            "Four coordinate clicks use these request paths: "
            + "; ".join(
                f"{label} {topology[key]['provider_sdk_calls']} SDK / "
                f"{topology[key]['transport_requests']} transport"
                for key, label in _COLUMNS
            )
            + ".",
            "",
            "Modal optimized excludes Modal Function startup from the product-create samples. "
            "Provider-default measurements use an external public-SDK caller; Modal optimized "
            "uses one Modal Function with the same requested Modal region as its targets.",
            "",
            boundaries["cleanup"],
            "",
            boundaries["runner_only_evidence"],
            "",
            "## Evidence and reproducibility",
            "",
            f"Evidence harness SHA: `{provenance['evidence_harness_sha']}`. Report source SHA: "
            f"`{provenance['report_source_sha']}`.",
            "",
            "Tracked inputs:",
            "",
            f"- [Provider defaults](../{artifacts['provider_defaults']})",
            f"- [Modal optimized](../{artifacts['modal_optimized']})",
            f"- [Modal observation](../{artifacts['modal_observation']})",
            f"- [Combined result](../{artifacts['combined']})",
            "",
            "The combined result binds the exact bytes of all three tracked inputs by SHA-256. "
            "Its p50 and p95 values are recomputed from the numeric samples in those inputs.",
            "",
        ]
    )
    return "\n".join(lines)


def _validated_provider_cases(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    _require_ok(payload, "provider artifact")
    if payload.get("benchmark") != "provider-compare":
        raise ProviderResultsError("provider artifact benchmark must be provider-compare")
    if payload.get("iterations") != 3:
        raise ProviderResultsError("provider artifact must use exactly 3 measured iterations")
    provenance = _mapping(payload.get("provenance"), "provider provenance")
    if provenance.get("status") != "current_reference":
        raise ProviderResultsError("provider provenance must be current_reference")
    if provenance.get("harness_state") != "clean":
        raise ProviderResultsError("provider provenance harness state must be clean")
    expected_commit = provenance.get("harness_commit")
    metadata = _mapping(payload.get("metadata"), "provider metadata")
    expected_providers = ["modal-daemon", "daytona", "e2b", "tzafon"]
    if metadata.get("providers") != expected_providers:
        raise ProviderResultsError("provider artifact has the wrong exact provider list")
    environment = _mapping(metadata.get("environment"), "provider environment")
    expected_environment = {
        "browser": None,
        "modal_ingress": "attested-tunnel",
        "daemon_http_version": "1.1",
        "resource_profile": "standard",
        "input_rate_limit_per_sec": 20,
        "action_case_pacing_ms": 1050,
        "subprocess_backend": "isolated-asyncio",
    }
    for key, value in expected_environment.items():
        if environment.get(key) != value:
            raise ProviderResultsError(f"provider default requires {key}={value!r}")
    raw_provenance = _mapping(environment.get("provenance"), "provider raw provenance")
    if raw_provenance.get("git_revision") != expected_commit:
        raise ProviderResultsError("provider raw provenance does not match harness commit")
    if raw_provenance.get("git_worktree_clean") is not True:
        raise ProviderResultsError("provider raw provenance must have a clean worktree")
    if raw_provenance.get("region") != "provider-default":
        raise ProviderResultsError("provider raw provenance region must be provider-default")
    _validate_provider_default_resources(raw_provenance.get("resolved_resources"))
    providers = _mapping(payload.get("providers"), "providers")
    if set(providers) != set(expected_providers):
        raise ProviderResultsError("provider artifact has the wrong exact provider keys")
    result: dict[str, dict[str, Any]] = {}
    for provider in ("modal-daemon", "daytona", "e2b", "tzafon"):
        item = _mapping(providers.get(provider), f"provider {provider}")
        _require_ok(item, f"provider {provider}", status_key=True)
        verification = _mapping(item.get("verification"), f"provider {provider} verification")
        if any(
            _mapping(verification.get(name), f"provider {provider} {name}").get("status") != "ok"
            for name in ("cursor_position", "type_text")
        ):
            raise ProviderResultsError(f"provider {provider} readback verification failed")
        cases = _mapping(item.get("cases"), f"provider {provider} cases")
        if provider != "modal-daemon":
            _validate_provider_default_metadata(provider, item.get("metadata"))
        for case_name, _ in _ROWS:
            case = _mapping(cases.get(case_name), f"provider {provider} case {case_name}")
            _require_case(case, expected=3, label=f"provider {provider} {case_name}")
            _require_case_semantics(case_name, case)
        _validate_observed_screenshot(provider, cases.get("screenshot_full"))
        if provider == "modal-daemon":
            _validate_modal_default_typing(cases)
        result[provider] = cases
    return result


def _validate_observed_screenshot(provider: str, value: Any) -> None:
    case = _mapping(value, f"provider {provider} screenshot case")
    observation = _mapping(case.get("last_result"), f"provider {provider} screenshot result")
    payload = (
        observation
        if provider == "modal-daemon"
        else _mapping(observation.get("payload"), f"provider {provider} screenshot payload")
    )
    expected = ("jpeg", 1280, 720) if provider == "tzafon" else ("png", 1024, 768)
    if (payload.get("format"), payload.get("width"), payload.get("height")) != expected:
        raise ProviderResultsError(
            f"provider {provider} screenshot format and geometry do not match the observed run"
        )


def _validate_modal_default_typing(cases: dict[str, Any]) -> None:
    for name, characters in (("type_100_chars", 100), ("type_1000_chars", 1000)):
        case = _mapping(cases.get(name), f"Modal default {name}")
        request = _mapping(case.get("request"), f"Modal default {name} request")
        expected = {"character_count": characters, "method": "auto", "delay_ms": 10}
        if request != expected:
            raise ProviderResultsError(
                "Modal provider-default typing must use the public TypeAction defaults"
            )
        if case.get("resolved_methods") != ["clipboard"]:
            raise ProviderResultsError(
                "Modal provider-default auto typing must resolve to clipboard for long text"
            )


def _validate_provider_default_metadata(provider: str, value: Any) -> None:
    metadata = _mapping(value, f"{provider} default metadata")
    expected_by_provider = {
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
    }
    labels = {"daytona": "Daytona", "e2b": "E2B", "tzafon": "Tzafon"}
    for key, expected in expected_by_provider[provider].items():
        if metadata.get(key) != expected:
            raise ProviderResultsError(
                f"{labels[provider]} default metadata requires {key}={expected!r}"
            )


def _validate_provider_default_resources(value: Any) -> None:
    resources = _mapping(value, "provider resolved resources")
    if set(resources) != {"cpu", "memory", "gpu"}:
        raise ProviderResultsError("provider resolved resources must cover CPU, memory, and GPU")
    expected = {
        "requested": None,
        "resolved": None,
        "status": "provider_default_unavailable",
    }
    for name in ("cpu", "memory", "gpu"):
        resource = _mapping(resources.get(name), f"provider {name} resources")
        if any(resource.get(key) != item for key, item in expected.items()):
            raise ProviderResultsError(
                "provider resolved resources must remain provider-default unavailable"
            )


def _validated_four_click_topology(
    provider_cases: dict[str, dict[str, Any]], optimized_payload: dict[str, Any]
) -> dict[str, Any]:
    optimized_topology = _mapping(
        _mapping(optimized_payload.get("request_topology"), "optimized request topology").get(
            "four_coordinate_clicks"
        ),
        "optimized four-click topology",
    )
    result: dict[str, Any] = {
        "modal_optimized": {
            "provider_sdk_calls": optimized_topology.get("provider_sdk_calls"),
            "transport_requests": optimized_topology.get("transport_requests"),
            "batching": optimized_topology.get("batching"),
        }
    }
    for provider in ("modal-daemon", "daytona", "e2b", "tzafon"):
        case = _mapping(
            provider_cases[provider].get("coordinate_click_sequence"),
            f"{provider} four-click case",
        )
        result[provider] = {
            "provider_sdk_calls": case.get("provider_sdk_call_count"),
            "transport_requests": case.get("transport_request_count"),
            "batching": case.get("batching"),
        }
    _validate_four_click_topology(result)
    return result


def _validate_four_click_topology(value: Any) -> None:
    topology = _mapping(value, "four-click request topology")
    expected = {
        "modal_optimized": {
            "provider_sdk_calls": 1,
            "transport_requests": 1,
            "batching": "single_request",
        },
        "modal-daemon": {
            "provider_sdk_calls": 1,
            "transport_requests": 1,
            "batching": "single_request",
        },
        "daytona": {
            "provider_sdk_calls": 4,
            "transport_requests": 4,
            "batching": "sequential_requests",
        },
        "e2b": {
            "provider_sdk_calls": 4,
            "transport_requests": 8,
            "batching": "sequential_requests",
        },
        "tzafon": {
            "provider_sdk_calls": 1,
            "transport_requests": 1,
            "batching": "single_request",
        },
    }
    if topology != expected:
        raise ProviderResultsError("four-click request topology is not exact")


def _validated_runner_only_runs(
    payload: dict[str, Any], metadata: dict[str, Any], *, label: str
) -> dict[str, Any]:
    if metadata.get("external_caller_included") is not False:
        raise ProviderResultsError(f"{label} evidence must be runner-only")
    runs = _mapping(payload.get("runs"), f"{label} runs")
    if (
        "external_caller" in runs
        or "comparison" in payload
        or "comparison" in metadata
    ):
        raise ProviderResultsError(
            f"{label} runner-only evidence cannot contain external comparison structure"
        )
    return runs


def sanitize_modal_optimized_input(
    payload: dict[str, Any], *, raw_sha256: str, evidence_harness_sha: str
) -> dict[str, Any]:
    """Reduce a publishable raw optimized run to a strict, reviewable evidence record."""
    _require_sha256(raw_sha256, "optimized raw SHA-256")
    cases, configuration = _validated_optimized_cases(
        payload, expected_harness_commit=evidence_harness_sha
    )
    run = _mapping(
        _mapping(payload.get("runs"), "optimized runs").get("modal_optimized_runner"),
        "optimized run",
    )
    product_create = _mapping(run.get("product_create"), "optimized product create")
    surface = _mapping(
        _mapping(run.get("surfaces"), "optimized surfaces").get("daemon-http"),
        "optimized daemon-http surface",
    )
    verification = _mapping(surface.get("verification"), "optimized verification")
    result = {
        "schema_version": SANITIZED_MODAL_INPUT_SCHEMA_VERSION,
        "benchmark": "sanitized-modal-optimized-provider",
        "status": "eligible",
        "provenance": {
            "evidence_harness_sha": evidence_harness_sha,
            "raw_sha256": raw_sha256,
        },
        "configuration": configuration,
        "schedule": {
            "measured_iterations": 30,
            "warmup_iterations": 1,
            "replacement_samples": 0,
            "fresh_target_per_attempt": True,
            "target_attempts": 31,
            "targets_created": 31,
            "targets_reused": 0,
        },
        "placement": {
            "runner": dict(_mapping(run.get("runner_placement"), "optimized placement")),
            "product_targets_verified": product_create.get("target_placements_verified"),
            "warm_target_verified": run.get("warm_target_placement_verified"),
        },
        "request_topology": {
            "four_coordinate_clicks": {
                "provider_sdk_calls": 1,
                "transport_requests": 1,
                "batching": "single_request",
                "source": "daemon benchmark contract",
            }
        },
        "cleanup": {
            "product_targets": dict(
                _mapping(product_create.get("cleanup"), "optimized lifecycle cleanup")
            ),
            "warm_target": dict(
                _mapping(run.get("warm_target_cleanup"), "optimized warm cleanup")
            ),
            "final_sweep": dict(
                _mapping(payload.get("final_cleanup"), "optimized final cleanup")
            ),
        },
        "success_attestation": {
            "artifact_ok": payload.get("ok") is True,
            "runner_ok": run.get("ok") is True,
            "failures": 0,
            "cursor_readback_ok": _mapping(
                verification.get("cursor_position"), "cursor verification"
            ).get("status")
            == "ok",
            "type_readback_ok": _mapping(
                verification.get("type_text"), "type verification"
            ).get("status")
            == "ok",
        },
        "cases": {
            name: _sanitized_case(case, name=name) for name, case in cases.items()
        },
    }
    validate_sanitized_modal_optimized_input(result)
    return result


def sanitize_modal_observation_input(
    payload: dict[str, Any], *, raw_sha256: str, evidence_harness_sha: str
) -> dict[str, Any]:
    """Reduce the selected observation run without retaining frames, hashes, URLs, or IDs."""
    _require_sha256(raw_sha256, "observation raw SHA-256")
    _validated_experiment(payload, expected_harness_commit=evidence_harness_sha)
    metadata = _mapping(payload.get("metadata"), "observation metadata")
    run = _mapping(
        _mapping(payload.get("runs"), "observation runs").get("modal_colocated_runner"),
        "observation run",
    )
    environment = _mapping(
        _mapping(run.get("metadata"), "observation metadata").get("environment"),
        "observation environment",
    )
    surface = _mapping(
        _mapping(run.get("surfaces"), "observation surfaces").get(
            "daemon-observation-stream"
        ),
        "observation surface",
    )
    case = _mapping(
        _mapping(surface.get("cases"), "observation cases").get(
            "observation_action_click_observe_change_http_raw"
        ),
        "observation case",
    )
    last = _mapping(case.get("last_result"), "observation last result")
    change = _mapping(last.get("change_result"), "observation change result")
    result = {
        "schema_version": SANITIZED_MODAL_INPUT_SCHEMA_VERSION,
        "benchmark": "sanitized-modal-observation",
        "status": "eligible",
        "provenance": {
            "evidence_harness_sha": evidence_harness_sha,
            "raw_sha256": raw_sha256,
        },
        "configuration": {
            "modal_region": metadata.get("modal_region"),
            "modal_ingress": metadata.get("modal_ingress"),
            "daemon_http_version": metadata.get("daemon_http_version"),
            "primary_runner_path": metadata.get("primary_runner_path"),
            "runner_paths": metadata.get("runner_paths"),
            "surface": "daemon-observation-stream",
            "browser": environment.get("browser"),
            "input_rate_limit_per_sec": environment.get("input_rate_limit_per_sec"),
            "subprocess_backend": environment.get("subprocess_backend"),
            "external_caller_included": metadata.get("external_caller_included"),
            "caller_topology": "Modal runner and target with the same requested Modal region",
        },
        "placement": {
            "requested_runner_region": environment.get("modal_runner_region"),
            "requested_target_region": metadata.get("modal_region"),
            "observed_match": "not_recorded_in_source",
        },
        "cleanup": {"status": "not_recorded_in_source"},
        "success_attestation": {
            "artifact_ok": payload.get("ok") is True,
            "runner_ok": run.get("ok") is True,
            "failures": 0,
            "last_action_ok": _mapping(
                last.get("action_result"), "observation action result"
            ).get("ok")
            is True,
            "last_change_detected": change.get("detected") is True,
            "last_timeout_reached": change.get("timeout_reached") is True,
            "last_hashes_distinct": change.get("baseline_source_sha256")
            != change.get("source_sha256"),
        },
        "case": {
            **_sanitized_case(
                case, name="observation_action_click_observe_change_http_raw"
            ),
            "metric": case.get("metric"),
            "experimental": case.get("experimental"),
            "change_timeout_ms": case.get("change_timeout_ms"),
            "replacement_samples": case.get("replacement_samples", 0),
            "input_backend": last.get("input_backend"),
        },
    }
    validate_sanitized_modal_observation_input(result)
    return result


def validate_sanitized_modal_optimized_input(payload: dict[str, Any]) -> None:
    _require_exact_keys(
        payload,
        {
            "schema_version",
            "benchmark",
            "status",
            "provenance",
            "configuration",
            "schedule",
            "placement",
            "request_topology",
            "cleanup",
            "success_attestation",
            "cases",
        },
        "sanitized optimized input",
    )
    if (
        payload.get("schema_version") != SANITIZED_MODAL_INPUT_SCHEMA_VERSION
        or payload.get("benchmark") != "sanitized-modal-optimized-provider"
        or payload.get("status") != "eligible"
    ):
        raise ProviderResultsError("sanitized optimized input schema is unsupported")
    _validate_safe_value(payload)
    provenance = _validated_sanitized_provenance(payload)
    configuration = _mapping(payload.get("configuration"), "optimized configuration")
    _validate_sanitized_optimized_configuration(
        configuration, evidence_harness_sha=str(provenance["evidence_harness_sha"])
    )
    expected_schedule = {
        "measured_iterations": 30,
        "warmup_iterations": 1,
        "replacement_samples": 0,
        "fresh_target_per_attempt": True,
        "target_attempts": 31,
        "targets_created": 31,
        "targets_reused": 0,
    }
    if payload.get("schedule") != expected_schedule:
        raise ProviderResultsError("sanitized optimized schedule is not exact")
    placement = _mapping(payload.get("placement"), "optimized placement")
    _require_exact_keys(
        placement,
        {"runner", "product_targets_verified", "warm_target_verified"},
        "optimized placement",
    )
    runner = _mapping(placement.get("runner"), "optimized runner placement")
    if (
        set(runner) != {"cloud", "region"}
        or runner.get("cloud") != configuration.get("modal_cloud_provider")
        or runner.get("region") != "us-west-2"
        or placement.get("product_targets_verified") != 31
        or placement.get("warm_target_verified") is not True
    ):
        raise ProviderResultsError("sanitized optimized placement is not exact")
    if payload.get("request_topology") != {
        "four_coordinate_clicks": {
            "provider_sdk_calls": 1,
            "transport_requests": 1,
            "batching": "single_request",
            "source": "daemon benchmark contract",
        }
    }:
        raise ProviderResultsError("sanitized optimized request topology is not exact")
    cleanup = _mapping(payload.get("cleanup"), "optimized cleanup")
    expected_cleanup = {
        "product_targets": {"attempted": 31, "succeeded": 31, "failures": []},
        "warm_target": {"attempted": True, "succeeded": True, "error_type": None},
        "final_sweep": {"cleanup_succeeded": True, "remaining_sandboxes": 0},
    }
    if cleanup != expected_cleanup:
        raise ProviderResultsError("sanitized optimized cleanup is not exact")
    if payload.get("success_attestation") != {
        "artifact_ok": True,
        "runner_ok": True,
        "failures": 0,
        "cursor_readback_ok": True,
        "type_readback_ok": True,
    }:
        raise ProviderResultsError("sanitized optimized success attestation failed")
    cases = _mapping(payload.get("cases"), "sanitized optimized cases")
    if set(cases) != {name for name, _label in _ROWS}:
        raise ProviderResultsError("sanitized optimized cases are not exact")
    for name, case in cases.items():
        _validate_sanitized_case(_mapping(case, f"sanitized {name}"), name=name, expected=30)


def validate_sanitized_modal_observation_input(payload: dict[str, Any]) -> None:
    _require_exact_keys(
        payload,
        {
            "schema_version",
            "benchmark",
            "status",
            "provenance",
            "configuration",
            "placement",
            "cleanup",
            "success_attestation",
            "case",
        },
        "sanitized observation input",
    )
    if (
        payload.get("schema_version") != SANITIZED_MODAL_INPUT_SCHEMA_VERSION
        or payload.get("benchmark") != "sanitized-modal-observation"
        or payload.get("status") != "eligible"
    ):
        raise ProviderResultsError("sanitized observation input schema is unsupported")
    _validate_safe_value(payload)
    _validated_sanitized_provenance(payload)
    expected_configuration = {
        "modal_region": "us-west-2",
        "modal_ingress": "connect",
        "daemon_http_version": "1.1",
        "primary_runner_path": "connect",
        "runner_paths": ["connect"],
        "surface": "daemon-observation-stream",
        "browser": "chromium",
        "input_rate_limit_per_sec": 0,
        "subprocess_backend": "isolated-asyncio",
        "external_caller_included": False,
        "caller_topology": "Modal runner and target with the same requested Modal region",
    }
    if payload.get("configuration") != expected_configuration:
        raise ProviderResultsError("sanitized observation configuration is not exact")
    if payload.get("placement") != {
        "requested_runner_region": "us-west-2",
        "requested_target_region": "us-west-2",
        "observed_match": "not_recorded_in_source",
    }:
        raise ProviderResultsError("sanitized observation placement is not exact")
    if payload.get("cleanup") != {"status": "not_recorded_in_source"}:
        raise ProviderResultsError("sanitized observation cleanup boundary is not exact")
    if payload.get("success_attestation") != {
        "artifact_ok": True,
        "runner_ok": True,
        "failures": 0,
        "last_action_ok": True,
        "last_change_detected": True,
        "last_timeout_reached": False,
        "last_hashes_distinct": True,
    }:
        raise ProviderResultsError("sanitized observation success attestation failed")
    case = _mapping(payload.get("case"), "sanitized observation case")
    _validate_sanitized_case(
        case,
        name="observation_action_click_observe_change_http_raw",
        expected=30,
        extra_keys={
            "metric",
            "experimental",
            "change_timeout_ms",
            "replacement_samples",
            "input_backend",
        },
    )
    if (
        case.get("benchmark_semantics") != "first-hash-confirmed-change-v1"
        or case.get("metric") != "action_to_first_hash_confirmed_change_ms"
        or case.get("experimental") is not True
        or case.get("change_timeout_ms") != 200
        or case.get("replacement_samples") != 0
        or case.get("input_backend") != "xtest"
    ):
        raise ProviderResultsError("sanitized observation case semantics are not exact")


def _validated_sanitized_optimized_cases(
    payload: dict[str, Any], *, expected_harness_commit: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_sanitized_modal_optimized_input(payload)
    provenance = _mapping(payload.get("provenance"), "optimized provenance")
    if provenance.get("evidence_harness_sha") != expected_harness_commit:
        raise ProviderResultsError("optimized source does not match expected harness commit")
    return (
        _mapping(payload.get("cases"), "optimized cases"),
        _mapping(payload.get("configuration"), "optimized configuration"),
    )


def _validated_sanitized_experiment(
    payload: dict[str, Any], *, expected_harness_commit: str
) -> dict[str, Any]:
    validate_sanitized_modal_observation_input(payload)
    provenance = _mapping(payload.get("provenance"), "observation provenance")
    if provenance.get("evidence_harness_sha") != expected_harness_commit:
        raise ProviderResultsError("observation source does not match expected harness commit")
    case = _mapping(payload.get("case"), "observation case")
    p50, p95 = _sample_quantiles(case)
    return {
        "provider": "Modal",
        "case": "observation_action_click_observe_change_http_raw",
        "benchmark_semantics": case["benchmark_semantics"],
        "metric": case["metric"],
        "experimental": True,
        "change_timeout_ms": 200,
        "eligibility": (
            "30/30 successful actions with hash-confirmed changes and no replacement samples"
        ),
        "iterations": 30,
        "successful_iterations": 30,
        "replacement_samples": 0,
        "p50_ms": p50,
        "p95_ms": p95,
    }


def _validated_optimized_cases(
    payload: dict[str, Any], *, expected_harness_commit: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        validate_modal_optimized_provider_artifact(payload, require_publishable=True)
    except ValueError as exc:
        raise ProviderResultsError(f"Modal optimized artifact is not publishable: {exc}") from exc
    if payload.get("failures") != []:
        raise ProviderResultsError("Modal optimized artifact has failures")
    metadata = _mapping(payload.get("metadata"), "Modal optimized metadata")
    if metadata.get("external_caller_included") is not False:
        raise ProviderResultsError("Modal optimized evidence must be runner-only")
    if (
        metadata.get("caller_topology")
        != "single-modal-function-same-requested-modal-region"
    ):
        raise ProviderResultsError("Modal optimized caller topology is invalid")
    if metadata.get("runner_kind") != "modal-function" or metadata.get("runner_invocations") != 1:
        raise ProviderResultsError("Modal optimized evidence requires one Modal Function runner")
    if metadata.get("runner_startup_in_product_create_boundary") is not False:
        raise ProviderResultsError("Modal Function startup must remain outside lifecycle samples")
    runs = _mapping(payload.get("runs"), "Modal optimized runs")
    if (
        set(runs) != {"modal_optimized_runner"}
        or "comparison" in payload
        or "comparison" in metadata
    ):
        raise ProviderResultsError("Modal optimized runner-only evidence has invalid structure")
    run = _mapping(runs.get("modal_optimized_runner"), "optimized run")
    _require_ok(run, "Modal optimized runner")
    expected = {
        "modal_region": "us-west-2",
        "modal_ingress": "connect",
        "daemon_http_version": "1.1",
        "browser": "chromium",
        "browser_prewarm": False,
        "image_revision": expected_harness_commit,
        "runner_cpu": 4.0,
        "runner_memory_mib": 8192,
        "target_cpu": 4.0,
        "target_memory_mib": 8192,
        "input_rate_limit_per_sec": 0,
        "subprocess_backend": "isolated-asyncio",
    }
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise ProviderResultsError(f"Modal optimized configuration requires {key}={value!r}")
    provenance = _mapping(metadata.get("provenance"), "optimized provenance")
    if provenance.get("git_revision") != expected_harness_commit:
        raise ProviderResultsError("Modal optimized source does not match expected harness commit")
    if provenance.get("git_worktree_clean") is not True:
        raise ProviderResultsError("Modal optimized source must have a clean worktree")
    if provenance.get("image_identity") != f"named:{expected_harness_commit}":
        raise ProviderResultsError("Modal optimized image does not match expected harness commit")
    product_create = _mapping(run.get("product_create"), "optimized product create")
    _require_case(product_create, expected=30, label="optimized product create")
    if (
        product_create.get("warmup_iterations") != 1
        or product_create.get("successful_warmup_iterations") != 1
        or product_create.get("replacement_samples") != 0
        or product_create.get("fresh_target_per_attempt") is not True
        or product_create.get("targets_created") != 31
        or product_create.get("target_attempts") != 31
        or product_create.get("targets_reused") != 0
        or product_create.get("target_placements_verified") != 31
    ):
        raise ProviderResultsError("optimized product create lifecycle schedule is invalid")
    lifecycle_cleanup = _mapping(product_create.get("cleanup"), "optimized lifecycle cleanup")
    if lifecycle_cleanup != {"attempted": 31, "succeeded": 31, "failures": []}:
        raise ProviderResultsError("optimized product create cleanup failed")
    warm_cleanup = _mapping(run.get("warm_target_cleanup"), "optimized warm cleanup")
    if warm_cleanup != {"attempted": True, "succeeded": True, "error_type": None}:
        raise ProviderResultsError("optimized warm target cleanup failed")
    if run.get("warm_target_placement_verified") is not True:
        raise ProviderResultsError("optimized warm target placement was not verified")
    placement = _mapping(run.get("runner_placement"), "optimized runner placement")
    cloud_provider = placement.get("cloud")
    if (
        not isinstance(cloud_provider, str)
        or not cloud_provider.strip()
        or placement.get("region") != "us-west-2"
        or set(placement) != {"cloud", "region"}
    ):
        raise ProviderResultsError("optimized runner placement is invalid")
    surfaces = _mapping(run.get("surfaces"), "optimized surfaces")
    surface = _mapping(surfaces.get("daemon-http"), "optimized daemon-http surface")
    _require_ok(surface, "optimized daemon-http surface", status_key=True)
    raw_cases = _mapping(surface.get("cases"), "optimized cases")
    source_names = {name: name for name, _label in _ROWS[1:]}
    if set(raw_cases) != set(source_names):
        raise ProviderResultsError("optimized daemon-http cases are not exact")
    cases = {PRODUCT_CREATE_CASE: product_create}
    for case_name, source_name in source_names.items():
        case = _mapping(raw_cases.get(source_name), f"optimized case {case_name}")
        _require_case(case, expected=30, label=f"optimized {case_name}")
        _require_case_semantics(case_name, case)
        if case_name in {
            "coordinate_click",
            "coordinate_click_sequence",
            "type_100_chars",
            "type_1000_chars",
        } and case.get("input_backends") != ["xtest"]:
            raise ProviderResultsError(f"optimized {case_name} must use XTest")
        if case_name in {"type_100_chars", "type_1000_chars"}:
            request = _mapping(case.get("request"), f"optimized {case_name} request")
            expected_request = {
                "character_count": 100 if case_name == "type_100_chars" else 1000,
                "method": "keystrokes",
                "delay_ms": 0,
            }
            if case_name == "type_1000_chars":
                expected_request["timeout_ms"] = 30_000
            if request != expected_request:
                raise ProviderResultsError(
                    "Modal optimized typing must retain explicit keystrokes with zero delay"
                )
            if case.get("resolved_methods") != ["keystrokes"]:
                raise ProviderResultsError(
                    "Modal optimized typing must resolve to keystrokes"
                )
        cases[case_name] = case
    verification = _mapping(surface.get("verification"), "optimized verification")
    for name in ("cursor_position", "type_text"):
        if _mapping(verification.get(name), f"optimized {name}").get("status") != "ok":
            raise ProviderResultsError("Modal optimized cursor/type readback failed")
    return cases, {
        **expected,
        "modal_cloud_provider": cloud_provider,
        "modal_runner_kind": "modal-function",
        "measured_iterations": 30,
        "caller_topology": "single Modal Function with the same requested Modal region",
        "observed_target_placement_match": True,
        "external_caller_included": False,
        "runner_startup_in_product_create_boundary": False,
    }


def _validated_experiment(
    payload: dict[str, Any], *, expected_harness_commit: str
) -> dict[str, Any]:
    _require_ok(payload, "Modal observation artifact")
    if payload.get("iterations") != 30:
        raise ProviderResultsError("Modal observation experiment must use 30 iterations")
    metadata = _mapping(payload.get("metadata"), "observation metadata")
    if metadata.get("primary_runner_path") != "connect" or metadata.get("runner_paths") != [
        "connect"
    ]:
        raise ProviderResultsError(
            "Modal observation experiment must select only the Connect runner"
        )
    runs = _validated_runner_only_runs(payload, metadata, label="Modal observation")
    expected_topology = {
        "surfaces": ["daemon-observation-stream"],
        "modal_region": "us-west-2",
        "modal_ingress": "connect",
        "daemon_http_version": "1.1",
    }
    for key, value in expected_topology.items():
        if metadata.get(key) != value:
            raise ProviderResultsError(f"observation topology requires {key}={value!r}")
    run = _mapping(
        runs.get("modal_colocated_runner"), "observation run"
    )
    _require_ok(run, "Modal observation runner")
    if run.get("iterations") != 30:
        raise ProviderResultsError("Modal observation runner must use 30 iterations")
    run_metadata = _mapping(run.get("metadata"), "observation run metadata")
    environment = _mapping(run_metadata.get("environment"), "observation environment")
    expected_environment = {
        "modal_region": "us-west-2",
        "modal_runner_path": "connect",
        "modal_ingress": "connect",
        "daemon_http_version": "1.1",
        "browser": "chromium",
        "input_rate_limit_per_sec": 0,
        "subprocess_backend": "isolated-asyncio",
    }
    for key, value in expected_environment.items():
        if environment.get(key) != value:
            raise ProviderResultsError(f"observation runner environment requires {key}={value!r}")
    provenance = _mapping(environment.get("provenance"), "observation provenance")
    if provenance.get("git_revision") != expected_harness_commit:
        raise ProviderResultsError("observation source does not match expected harness commit")
    if provenance.get("git_worktree_clean") is not True:
        raise ProviderResultsError("observation source must have a clean worktree")
    surface = _mapping(
        _mapping(run.get("surfaces"), "observation surfaces").get("daemon-observation-stream"),
        "observation surface",
    )
    _require_ok(surface, "observation surface", status_key=True)
    observation_cases = _mapping(surface.get("cases"), "observation cases")
    if set(observation_cases) != {"observation_action_click_observe_change_http_raw"}:
        raise ProviderResultsError("observation artifact must contain only the selected case")
    case = _mapping(
        observation_cases.get("observation_action_click_observe_change_http_raw"),
        "observation experiment case",
    )
    _require_case(case, expected=30, label="observation experiment")
    if (
        case.get("benchmark_semantics") != "first-hash-confirmed-change-v1"
        or case.get("metric") != "action_to_first_hash_confirmed_change_ms"
        or case.get("experimental") is not True
    ):
        raise ProviderResultsError("observation experiment semantics are pre-fix or ineligible")
    if case.get("change_timeout_ms") != 200:
        raise ProviderResultsError("observation experiment change deadline is ineligible")
    if case.get("replacement_samples", 0) != 0:
        raise ProviderResultsError("observation experiment cannot contain replacement samples")
    last = _mapping(case.get("last_result"), "observation last result")
    if last.get("input_backend") != "xtest":
        raise ProviderResultsError("observation experiment must prove the XTest input backend")
    if _mapping(last.get("action_result"), "observation action result").get("ok") is not True:
        raise ProviderResultsError("observation experiment action failed")
    change = _mapping(last.get("change_result"), "observation change result")
    if change.get("detected") is not True:
        raise ProviderResultsError("observation experiment did not detect a change")
    if change.get("timeout_reached") is not False:
        raise ProviderResultsError("observation experiment reached its timeout")
    baseline_hash = change.get("baseline_source_sha256")
    source_hash = change.get("source_sha256")
    if (
        not all(
            isinstance(value, str) and _SHA256_RE.fullmatch(value)
            for value in (baseline_hash, source_hash)
        )
        or baseline_hash == source_hash
    ):
        raise ProviderResultsError(
            "observation experiment requires different baseline/source hashes"
        )
    p50, p95 = _sample_quantiles(case)
    return {
        "provider": "Modal",
        "case": "observation_action_click_observe_change_http_raw",
        "benchmark_semantics": case["benchmark_semantics"],
        "metric": case["metric"],
        "experimental": True,
        "change_timeout_ms": 200,
        "eligibility": (
            "30/30 successful actions with hash-confirmed changes and no replacement samples"
        ),
        "iterations": 30,
        "successful_iterations": 30,
        "replacement_samples": 0,
        "p50_ms": p50,
        "p95_ms": p95,
    }


def _summary_value(case: dict[str, Any]) -> dict[str, Any]:
    samples = [_number(value, "case sample") for value in case["samples_ms"]]
    p50, p95 = _sample_quantiles(case)
    return {
        "status": "measured",
        "sample_count": len(samples),
        "min_ms": min(samples),
        "max_ms": max(samples),
        "p50_ms": p50,
        "p95_ms": p95,
    }


def _sanitized_case(case: dict[str, Any], *, name: str) -> dict[str, Any]:
    samples = [_number(value, f"{name} sample") for value in case.get("samples_ms", [])]
    return {
        "status": "ok",
        "iterations": case.get("iterations"),
        "successful_iterations": case.get("successful_iterations"),
        "samples_ms": samples,
        "summary_ms": _sample_summary(samples),
        "benchmark_semantics": case.get("benchmark_semantics"),
        "input_backends": case.get("input_backends"),
        "shell_mode": case.get("shell_mode"),
        "resolved_methods": case.get("resolved_methods"),
    }


def _sample_summary(samples: list[float]) -> dict[str, float]:
    if not samples:
        raise ProviderResultsError("sanitized evidence requires numeric samples")
    case = {"samples_ms": samples}
    p50, p95 = _sample_quantiles(case)
    return {"min": min(samples), "max": max(samples), "p50": p50, "p95": p95}


def _validate_sanitized_case(
    case: dict[str, Any],
    *,
    name: str,
    expected: int,
    extra_keys: set[str] | None = None,
) -> None:
    expected_keys = {
        "status",
        "iterations",
        "successful_iterations",
        "samples_ms",
        "summary_ms",
        "benchmark_semantics",
        "input_backends",
        "shell_mode",
        "resolved_methods",
        *(extra_keys or set()),
    }
    _require_exact_keys(case, expected_keys, f"sanitized {name}")
    if (
        case.get("status") != "ok"
        or case.get("iterations") != expected
        or case.get("successful_iterations") != expected
    ):
        raise ProviderResultsError(f"sanitized {name} success counts are not exact")
    samples = case.get("samples_ms")
    if not isinstance(samples, list) or len(samples) != expected:
        raise ProviderResultsError(f"sanitized {name} requires {expected} samples")
    numeric = [_require_finite_nonnegative(value, f"sanitized {name} sample") for value in samples]
    summary = _mapping(case.get("summary_ms"), f"sanitized {name} summary")
    _require_exact_keys(summary, {"min", "max", "p50", "p95"}, f"sanitized {name} summary")
    if summary != _sample_summary(numeric):
        raise ProviderResultsError(f"sanitized {name} summary does not match its samples")
    _require_case_semantics(name, case)


def _validated_sanitized_provenance(payload: dict[str, Any]) -> dict[str, Any]:
    provenance = _mapping(payload.get("provenance"), "sanitized provenance")
    _require_exact_keys(
        provenance,
        {"evidence_harness_sha", "raw_sha256"},
        "sanitized provenance",
    )
    _require_commit(provenance.get("evidence_harness_sha"), "evidence harness SHA")
    _require_sha256(provenance.get("raw_sha256"), "raw SHA-256")
    return provenance


def _validate_sanitized_optimized_configuration(
    configuration: dict[str, Any], *, evidence_harness_sha: str
) -> None:
    expected = {
        "modal_region": "us-west-2",
        "modal_cloud_provider": "CLOUD_PROVIDER_AWS",
        "modal_runner_kind": "modal-function",
        "modal_ingress": "connect",
        "daemon_http_version": "1.1",
        "browser": "chromium",
        "browser_prewarm": False,
        "image_revision": evidence_harness_sha,
        "runner_cpu": 4.0,
        "runner_memory_mib": 8192,
        "target_cpu": 4.0,
        "target_memory_mib": 8192,
        "input_rate_limit_per_sec": 0,
        "subprocess_backend": "isolated-asyncio",
        "measured_iterations": 30,
        "caller_topology": "single Modal Function with the same requested Modal region",
        "observed_target_placement_match": True,
        "external_caller_included": False,
        "runner_startup_in_product_create_boundary": False,
    }
    if configuration != expected:
        raise ProviderResultsError("sanitized optimized configuration is not exact")


def _sample_quantiles(case: dict[str, Any]) -> tuple[float, float]:
    samples = sorted(_number(value, "case sample") for value in case["samples_ms"])
    p50 = float(statistics.median(samples))
    rank = 0.95 * (len(samples) - 1)
    lower = math.floor(rank)
    fraction = rank - lower
    p95 = samples[lower] + fraction * (samples[math.ceil(rank)] - samples[lower])
    return p50, p95


def _require_case(case: dict[str, Any], *, expected: int, label: str) -> None:
    if case.get("status") != "ok" or case.get("failures") != []:
        raise ProviderResultsError(f"{label} failed")
    if case.get("iterations") != expected or case.get("successful_iterations") != expected:
        raise ProviderResultsError(
            f"{label} requires exactly {expected}/{expected} successful samples"
        )
    samples = case.get("samples_ms")
    if not isinstance(samples, list) or len(samples) != expected:
        raise ProviderResultsError(f"{label} requires exactly {expected}/{expected} samples")
    for sample in samples:
        _require_finite_nonnegative(sample, f"{label} sample")
    summary = _mapping(case.get("summary_ms"), f"{label} summary")
    _require_summary_order(summary.get("p50"), summary.get("p95"), f"{label} summary")


def _require_case_semantics(name: str, case: dict[str, Any]) -> None:
    if (
        name in {"coordinate_click", "coordinate_click_sequence"}
        and case.get("benchmark_semantics") != "coordinate-click-v1"
    ):
        raise ProviderResultsError(f"{name} requires exact coordinate-click-v1 semantics")
    if name == "command_nonlogin_shell_echo" and (
        case.get("benchmark_semantics") != "shell-command-echo-v2"
        or case.get("shell_mode") != "non_login"
    ):
        raise ProviderResultsError("command case requires exact non-login shell semantics")


def _require_ok(payload: dict[str, Any], label: str, *, status_key: bool = False) -> None:
    ok = payload.get("status") == "ok" if status_key else payload.get("ok") is True
    if not ok or payload.get("failures") != []:
        raise ProviderResultsError(f"{label} is not successful")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProviderResultsError(f"{label} must be an object")
    return value


def _require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ProviderResultsError(f"{label} has unexpected or missing fields")


def _require_finite_nonnegative(value: Any, label: str) -> float:
    number = _number(value, label)
    if not math.isfinite(number) or number < 0:
        raise ProviderResultsError(f"{label} must be finite and nonnegative")
    return number


def _require_summary_order(p50: Any, p95: Any, label: str) -> tuple[float, float]:
    p50_number = _require_finite_nonnegative(p50, f"{label} p50")
    p95_number = _require_finite_nonnegative(p95, f"{label} p95")
    if p95_number < p50_number:
        raise ProviderResultsError(f"{label} p95 must be greater than or equal to p50")
    return p50_number, p95_number


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProviderResultsError(f"{label} must be numeric")
    return float(value)


def _require_digest_tuple(values: tuple[str, str, str]) -> None:
    if len(values) != 3 or any(_SHA256_RE.fullmatch(value) is None for value in values):
        raise ProviderResultsError("all three input SHA-256 digests are required")


def _require_sha256(value: Any, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ProviderResultsError(f"{label} must be a SHA-256 digest")


def _require_commit(value: Any, label: str) -> None:
    if not isinstance(value, str) or _COMMIT_RE.fullmatch(value) is None:
        raise ProviderResultsError(f"{label} must be a full Git commit")


def _normalize_key(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower().replace("-", "_")


def _validate_safe_value(value: Any, *, key: str | None = None) -> None:
    if key is not None:
        normalized = _normalize_key(key)
        if normalized in _SECRET_KEYS or normalized.endswith(
            ("_token", "_secret", "_password", "_resource_id", "_run_id")
        ):
            raise ProviderResultsError(f"combined artifact contains unsafe key: {key}")
    if isinstance(value, dict):
        for item_key, item in value.items():
            _validate_safe_value(item, key=str(item_key))
    elif isinstance(value, list):
        for item in value:
            _validate_safe_value(item)
    elif isinstance(value, str) and re.search(r"https?://", value, flags=re.IGNORECASE):
        parsed = urlsplit(value[value.lower().find("http") :])
        if parsed.netloc:
            raise ProviderResultsError("combined artifact contains a URL")


def _format_value(value: dict[str, Any]) -> str:
    if value["sample_count"] < REPORTING_POLICY["small_sample_threshold"]:
        return (
            f"{value['p50_ms']:.2f} "
            f"[{value['min_ms']:.2f}–{value['max_ms']:.2f}]"  # noqa: RUF001
        )
    return f"{value['p50_ms']:.2f} / {value['p95_ms']:.2f}"
