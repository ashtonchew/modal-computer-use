from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
MINIMUM_ELIGIBLE_SOURCE_SHA = "c6afa9d0384e664f5ccd7426014dcf5d20266e0e"
OPAQUE_TZAFON_SETTLE_SENTENCE = (
    "Tzafon settle semantics are opaque at this API boundary, so its action "
    "acknowledgement is not treated as equivalent to Modal’s hash-confirmed first visual change."  # noqa: RUF001
)
NATIVE_SCREENSHOT_CAVEAT = (
    "Full screenshots use each provider's native/default format and are not "
    "pixel- or codec-normalized."
)
CLEANUP_BOUNDARY = (
    "Eligibility requires successful command and top-level outcomes. Cleanup errors "
    "are terminal in the producer, but this combined artifact does not independently "
    "prove cleanup beyond those recorded outcomes."
)

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
    "base_url",
    "bearer",
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
    source_sha: str,
    expected_harness_commit: str,
) -> dict[str, Any]:
    """Validate three source artifacts and return a secret-safe combined record."""
    _require_digest_tuple(input_sha256)
    _require_commit(source_sha, "source_sha")
    _require_commit(expected_harness_commit, "expected_harness_commit")
    if source_sha != expected_harness_commit:
        raise ProviderResultsError("source SHA must match the expected harness commit")

    provider_cases = _validated_provider_cases(provider_artifact)
    provider_harness_sha = _mapping(provider_artifact.get("provenance"), "provider provenance").get(
        "harness_commit"
    )
    _require_commit(provider_harness_sha, "provider harness commit")
    if provider_harness_sha != expected_harness_commit:
        raise ProviderResultsError("provider source does not match the expected harness commit")
    optimized_cases, optimized_config = _validated_optimized_cases(
        modal_optimized_artifact, expected_harness_commit=expected_harness_commit
    )
    experiment = _validated_experiment(
        modal_observation_artifact,
        expected_harness_commit=expected_harness_commit,
    )

    rows: list[dict[str, Any]] = []
    for case_name, label in _ROWS:
        values: dict[str, Any] = {}
        for key, _column_label in _COLUMNS:
            if key == "modal_optimized" and case_name == "product_create_to_first_screenshot":
                values[key] = {"status": "not_measured"}
                continue
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
        "provenance": {
            "source_sha": source_sha,
            "expected_harness_commit": expected_harness_commit,
            "provider_harness_sha": provider_harness_sha,
            "minimum_eligible_source_sha": MINIMUM_ELIGIBLE_SOURCE_SHA,
            "safe_configuration_sha256": configuration_sha256,
            "inputs": [
                {"role": "sanitized_provider_defaults", "sha256": input_sha256[0]},
                {"role": "raw_modal_optimized", "sha256": input_sha256[1]},
                {"role": "raw_modal_observation", "sha256": input_sha256[2]},
            ],
        },
        "configuration": configuration,
        "headline": {
            "columns": [label for _, label in _COLUMNS],
            "rows": rows,
        },
        "experiment": experiment,
        "boundaries": {
            "native_screenshot_caveat": NATIVE_SCREENSHOT_CAVEAT,
            "cleanup": CLEANUP_BOUNDARY,
            "tzafon_settle": OPAQUE_TZAFON_SETTLE_SENTENCE,
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
            "provenance",
            "configuration",
            "headline",
            "experiment",
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
            if (
                case_name == "product_create_to_first_screenshot"
                and provider_key == "modal_optimized"
            ):
                if value != {"status": "not_measured"}:
                    raise ProviderResultsError("only Modal optimized create may be not_measured")
                continue
            _require_exact_keys(
                value,
                {"status", "p50_ms", "p95_ms"},
                f"headline {case_name} {provider_key}",
            )
            if value.get("status") != "measured":
                raise ProviderResultsError("headline measured values require measured status")
            _require_summary_order(value.get("p50_ms"), value.get("p95_ms"), "headline summary")
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
    boundaries = _mapping(payload.get("boundaries"), "boundaries")
    expected_boundaries = {
        "native_screenshot_caveat": NATIVE_SCREENSHOT_CAVEAT,
        "cleanup": CLEANUP_BOUNDARY,
        "tzafon_settle": OPAQUE_TZAFON_SETTLE_SENTENCE,
    }
    if boundaries != expected_boundaries:
        raise ProviderResultsError("combined artifact boundaries are not exact")
    sample_counts = _mapping(payload.get("sample_counts"), "sample counts")
    if sample_counts != {"provider_defaults": 3, "modal_optimized": 30, "experiment": 30}:
        raise ProviderResultsError("combined artifact sample counts are not exact")
    provenance = _mapping(payload.get("provenance"), "provenance")
    _require_exact_keys(
        provenance,
        {
            "source_sha",
            "expected_harness_commit",
            "provider_harness_sha",
            "minimum_eligible_source_sha",
            "safe_configuration_sha256",
            "inputs",
        },
        "provenance",
    )
    _require_commit(provenance.get("source_sha"), "provenance source_sha")
    _require_commit(provenance.get("expected_harness_commit"), "expected harness commit")
    _require_commit(provenance.get("provider_harness_sha"), "provider harness SHA")
    if not (
        provenance.get("source_sha")
        == provenance.get("expected_harness_commit")
        == provenance.get("provider_harness_sha")
    ):
        raise ProviderResultsError("combined provenance source revisions must be equal")
    if provenance.get("minimum_eligible_source_sha") != MINIMUM_ELIGIBLE_SOURCE_SHA:
        raise ProviderResultsError("combined artifact has the wrong minimum source commit")
    configuration = _mapping(payload.get("configuration"), "configuration")
    expected_configuration = {
        "modal_optimized": {
            "modal_region": "us-west-2",
            "modal_runner_path": "connect",
            "modal_ingress": "connect",
            "daemon_http_version": "1.1",
            "browser": "chromium",
            "input_rate_limit_per_sec": 0,
            "subprocess_backend": "isolated-asyncio",
            "measured_iterations": 30,
            "caller_topology": "separate same-region Modal runner",
        },
        "provider_defaults": {
            "measured_iterations": 3,
            "caller_topology": "external provider SDK caller",
        },
    }
    if configuration != expected_configuration:
        raise ProviderResultsError("combined artifact configuration is not exact")
    configuration_digest = hashlib.sha256(
        json.dumps(configuration, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    if provenance.get("safe_configuration_sha256") != configuration_digest:
        raise ProviderResultsError("safe configuration digest does not match configuration")
    inputs = provenance.get("inputs")
    expected_roles = [
        "sanitized_provider_defaults",
        "raw_modal_optimized",
        "raw_modal_observation",
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
    lines = [
        "# Provider benchmark results",
        "",
        "Values are p50 / p95 milliseconds.",
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
            "## Modal-only experimental result",
            "",
            "Action click to first hash-confirmed visual change: "
            f"{experiment['p50_ms']:.2f} / {experiment['p95_ms']:.2f} ms p50 / p95 "
            f"({experiment['successful_iterations']}/{experiment['iterations']}, "
            "no replacement samples).",
            "",
            payload["boundaries"]["tzafon_settle"],
            "",
            payload["boundaries"]["native_screenshot_caveat"],
            "",
            f"Sample counts: provider defaults {counts['provider_defaults']}/"
            f"{counts['provider_defaults']}; Modal optimized {counts['modal_optimized']}/"
            f"{counts['modal_optimized']}; Modal experiment {counts['experiment']}/"
            f"{counts['experiment']}. The default and optimized Modal columns use explicitly "
            "different caller topologies.",
            "",
            payload["boundaries"]["cleanup"],
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
        "browser": "chromium",
        "modal_ingress": "attested-tunnel",
        "daemon_http_version": "1.1",
        "resource_profile": "browser",
        "input_rate_limit_per_sec": 0,
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
        result[provider] = cases
    return result


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


def _validated_optimized_cases(
    payload: dict[str, Any], *, expected_harness_commit: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    _require_ok(payload, "Modal optimized artifact")
    if payload.get("iterations") != 30:
        raise ProviderResultsError("Modal optimized artifact must use exactly 30 iterations")
    metadata = _mapping(payload.get("metadata"), "Modal optimized metadata")
    if metadata.get("primary_runner_path") != "connect" or metadata.get("runner_paths") != [
        "connect"
    ]:
        raise ProviderResultsError("Modal optimized artifact must select only the Connect runner")
    if metadata.get("surfaces") != ["daemon-http"]:
        raise ProviderResultsError("Modal optimized artifact must select only daemon-http")
    run = _mapping(
        _mapping(payload.get("runs"), "runs").get("modal_colocated_runner"), "optimized run"
    )
    _require_ok(run, "Modal optimized runner")
    run_metadata = _mapping(run.get("metadata"), "optimized run metadata")
    environment = _mapping(run_metadata.get("environment"), "optimized environment")
    expected = {
        "modal_region": "us-west-2",
        "modal_runner_path": "connect",
        "modal_ingress": "connect",
        "daemon_http_version": "1.1",
        "browser": "chromium",
        "input_rate_limit_per_sec": 0,
        "subprocess_backend": "isolated-asyncio",
    }
    for key, value in expected.items():
        if environment.get(key) != value:
            raise ProviderResultsError(f"Modal optimized configuration requires {key}={value!r}")
    provenance = _mapping(environment.get("provenance"), "optimized provenance")
    if provenance.get("git_revision") != expected_harness_commit:
        raise ProviderResultsError("Modal optimized source does not match expected harness commit")
    if provenance.get("git_worktree_clean") is not True:
        raise ProviderResultsError("Modal optimized source must have a clean worktree")
    preflight = run_metadata.get("runner_preflight")
    if (
        isinstance(preflight, dict)
        and preflight.get("status") not in {None, "ok"}
        and preflight.get("ok") is not True
    ):
        raise ProviderResultsError("Modal optimized runner preflight failed")
    surfaces = _mapping(run.get("surfaces"), "optimized surfaces")
    surface = _mapping(surfaces.get("daemon-http"), "optimized daemon-http surface")
    _require_ok(surface, "optimized daemon-http surface", status_key=True)
    cases = _mapping(surface.get("cases"), "optimized cases")
    for case_name, _ in _ROWS[1:]:
        case = _mapping(cases.get(case_name), f"optimized case {case_name}")
        _require_case(case, expected=30, label=f"optimized {case_name}")
        _require_case_semantics(case_name, case)
        if case_name in {
            "coordinate_click",
            "coordinate_click_sequence",
            "type_100_chars",
            "type_1000_chars",
        } and case.get("input_backends") != ["xtest"]:
            raise ProviderResultsError(f"optimized {case_name} must use XTest")
    verification = _mapping(surface.get("verification"), "optimized verification")
    for name in ("cursor_position", "type_text"):
        if _mapping(verification.get(name), f"optimized {name}").get("status") != "ok":
            raise ProviderResultsError("Modal optimized cursor/type readback failed")
    return cases, {
        **expected,
        "measured_iterations": 30,
        "caller_topology": "separate same-region Modal runner",
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
        _mapping(payload.get("runs"), "runs").get("modal_colocated_runner"), "observation run"
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
    summary = _mapping(case.get("summary_ms"), "observation summary")
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
        "p50_ms": _number(summary.get("p50"), "experiment p50"),
        "p95_ms": _number(summary.get("p95"), "experiment p95"),
    }


def _summary_value(case: dict[str, Any]) -> dict[str, Any]:
    summary = _mapping(case.get("summary_ms"), "case summary")
    p50, p95 = _require_summary_order(summary.get("p50"), summary.get("p95"), "case summary")
    return {
        "status": "measured",
        "p50_ms": p50,
        "p95_ms": p95,
    }


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
    if value.get("status") == "not_measured":
        return "not measured"
    return f"{value['p50_ms']:.2f} / {value['p95_ms']:.2f}"
