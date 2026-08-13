"""Validate and render a sanitized external action-to-frame report.

This module reads an offline JSON-shaped artifact. It does not import provider SDKs,
create infrastructure, or call a live service. The artifact is a promotion input for
one logical action-to-immediate-frame case. Each arm records a complete measured path.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from datetime import date
from importlib.metadata import PackageNotFoundError, version
from typing import Any

ACTION_FRAME_SCHEMA_VERSION = 1
ACTION_FRAME_BENCHMARK = "external-provider-action-frame"
ACTION_FRAME_CASE_ID = "ordered-actions-to-immediate-frame-v1"
ACTION_FRAME_TIMER_BOUNDARY = (
    "caller_before_ordered_action_dispatch_to_validated_immediate_full_frame_bytes"
)
ACTION_FRAME_ACTION_SEMANTICS = "one-left-click-at-512-384-then-immediate-full-frame"
ACTION_FRAME_ACTION_PAYLOAD_SHA256 = (
    "83599900ae670680c7d84271000b03114940c492d935c26b5f0999a281958296"
)

_SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._/-]{0,127}")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}")

_TOP_LEVEL_KEYS = {
    "schema_version",
    "benchmark",
    "status",
    "evidence_date",
    "source_sha",
    "comparison",
    "workload",
    "arms",
}
_COMPARISON_KEYS = {"case_id", "scope", "topology", "claim"}
_WORKLOAD_KEYS = {
    "case_id",
    "action_semantics",
    "action_payload_sha256",
    "timer_boundary",
    "warmup_iterations",
    "measured_iterations",
    "screenshot_policy",
}
_ARM_KEYS = {
    "case_id",
    "provider",
    "path",
    "source_sha",
    "sdk",
    "topology",
    "resources",
    "screenshot",
    "request_shape",
    "timer_boundary",
    "warmup_iterations",
    "measured_iterations",
    "samples_ms",
    "summary_ms",
    "failures",
    "cleanup",
    "harness_retries",
    "replacement_samples",
    "input_artifact_digests",
    "status",
}
_SDK_KEYS = {"package", "version", "retry_policy"}
_TOPOLOGY_KEYS = {"caller", "requested_region", "observed_region", "placement"}
_RESOURCE_KEYS = {"cpu", "memory_mib", "availability", "source"}
_SCREENSHOT_KEYS = {"format", "width", "height", "show_cursor"}
_REQUEST_SHAPE_KEYS = {"sdk_calls", "transport_requests", "batching"}
_SUMMARY_KEYS = {"p50", "p95"}
_CLEANUP_KEYS = {"status", "survivors"}
_FAILURE_KEYS = {"phase", "category"}
_DIGEST_KEYS = {"role", "sha256"}
_CLEANUP_VERIFICATION_KEYS = {"source_sha", "providers"}

_SECRET_KEY_NAMES = {
    "access_key",
    "api_key",
    "artifact_bytes",
    "authorization",
    "base_url",
    "bearer",
    "clipboard",
    "credential",
    "credentials",
    "endpoint",
    "password",
    "private_key",
    "raw_error",
    "screenshot_bytes",
    "secret",
    "secret_key",
    "token",
    "typed_content",
    "typed_text",
    "url",
}
_RAW_ERROR_KEY_NAMES = {"error", "exception", "message", "raw_error", "traceback"}


class ActionFrameReportError(ValueError):
    """Raised when an action-to-frame artifact cannot support publication."""


def validate_action_frame_report(payload: dict[str, Any]) -> None:
    """Validate a complete, sanitized action-to-frame benchmark artifact.

    Validation requires one complete measured arm per provider. Every arm uses the
    same logical case, timer boundary, screenshot policy, warmup count, and measured
    count. Summary values are recalculated from the retained numeric samples.
    """

    if not isinstance(payload, dict):
        raise ActionFrameReportError("report must be a JSON object")
    _require_exact_keys(payload, _TOP_LEVEL_KEYS, "report")
    _reject_unsafe_values(payload)

    if payload.get("schema_version") != ACTION_FRAME_SCHEMA_VERSION:
        raise ActionFrameReportError("unsupported action-to-frame schema version")
    if payload.get("benchmark") != ACTION_FRAME_BENCHMARK:
        raise ActionFrameReportError("benchmark name must identify action-to-frame evidence")
    if payload.get("status") != "eligible":
        raise ActionFrameReportError("report status must be eligible")
    evidence_date = payload.get("evidence_date")
    if not isinstance(evidence_date, str) or _DATE_RE.fullmatch(evidence_date) is None:
        raise ActionFrameReportError("evidence_date must use YYYY-MM-DD")
    try:
        date.fromisoformat(evidence_date)
    except ValueError as exc:
        raise ActionFrameReportError("evidence_date is not a calendar date") from exc
    source_sha = _require_source_sha(payload.get("source_sha"), "report source_sha")

    comparison = _mapping(payload.get("comparison"), "comparison")
    _require_exact_keys(comparison, _COMPARISON_KEYS, "comparison")
    if comparison != {
        "case_id": ACTION_FRAME_CASE_ID,
        "scope": "complete-measured-paths",
        "topology": "disclosed-per-arm",
        "claim": "path-level-comparison-only",
    }:
        raise ActionFrameReportError("comparison scope must describe complete measured paths")

    workload = _mapping(payload.get("workload"), "workload")
    _require_exact_keys(workload, _WORKLOAD_KEYS, "workload")
    _validate_workload(workload)

    arms = payload.get("arms")
    if not isinstance(arms, list) or len(arms) < 2:
        raise ActionFrameReportError("report requires at least two complete arms")
    providers: set[str] = set()
    for index, arm_value in enumerate(arms):
        arm = _mapping(arm_value, f"arm {index}")
        _validate_arm(
            arm,
            index=index,
            source_sha=source_sha,
            workload=workload,
            providers=providers,
        )


def render_action_frame_report_json(payload: dict[str, Any]) -> str:
    """Return deterministic JSON after validation."""

    validate_action_frame_report(payload)
    return f"{json.dumps(payload, indent=2, sort_keys=True)}\n"


def assemble_action_frame_report(
    *,
    step_artifact: dict[str, Any],
    provider_artifact: dict[str, Any],
    cleanup_verification: dict[str, Any],
    source_sha: str,
    evidence_date: str,
    input_artifact_digests: dict[str, str],
) -> dict[str, Any]:
    """Assemble a publishable report from fresh, sanitized benchmark inputs.

    The function accepts JSON-shaped values only. It copies measured timings and
    safe metadata into the report. It omits provider observations and raw cleanup
    details. Every source, case, timer, action digest, and cleanup result must match.
    """

    report_source_sha = _require_source_sha(source_sha, "assembly source_sha")
    _validate_report_date(evidence_date)
    digests = _validated_input_digest_map(input_artifact_digests)
    _validate_step_input_source_sha(step_artifact, report_source_sha)
    _validate_input_source_sha(provider_artifact, report_source_sha, "provider artifact")
    _validate_cleanup_verification(cleanup_verification, report_source_sha)

    step_arm = _assemble_step_arm(
        step_artifact,
        source_sha=report_source_sha,
        input_digests=digests,
        cleanup_verification=cleanup_verification,
    )
    provider_arms = _assemble_provider_arms(
        provider_artifact,
        source_sha=report_source_sha,
        input_digests=digests,
        cleanup_verification=cleanup_verification,
    )
    arms = [step_arm, *provider_arms]
    if len(arms) < 2:
        raise ActionFrameReportError("assembly requires a Modal arm and a provider arm")
    result = {
        "schema_version": ACTION_FRAME_SCHEMA_VERSION,
        "benchmark": ACTION_FRAME_BENCHMARK,
        "status": "eligible",
        "evidence_date": evidence_date,
        "source_sha": report_source_sha,
        "comparison": {
            "case_id": ACTION_FRAME_CASE_ID,
            "scope": "complete-measured-paths",
            "topology": "disclosed-per-arm",
            "claim": "path-level-comparison-only",
        },
        "workload": {
            "case_id": ACTION_FRAME_CASE_ID,
            "action_semantics": ACTION_FRAME_ACTION_SEMANTICS,
            "action_payload_sha256": ACTION_FRAME_ACTION_PAYLOAD_SHA256,
            "timer_boundary": ACTION_FRAME_TIMER_BOUNDARY,
            "warmup_iterations": step_arm["warmup_iterations"],
            "measured_iterations": step_arm["measured_iterations"],
            "screenshot_policy": "provider-native-full-frame",
        },
        "arms": arms,
    }
    validate_action_frame_report(result)
    return result


def render_action_frame_report_markdown(payload: dict[str, Any]) -> str:
    """Return a compact, long-form Markdown report for the benchmark page."""

    validate_action_frame_report(payload)
    workload = payload["workload"]
    arms = payload["arms"]

    lines = [
        f"# External provider action-to-frame benchmark, {payload['evidence_date']}",
        "",
        "**Evidence status:** eligible",
        "",
        "This report measures one click followed by the next full screenshot through "
        f"{len(arms)} public SDK paths. The paths use different caller topologies, request "
        "counts, resources, and screenshot formats. The results describe each complete path "
        "under its recorded configuration.",
        "",
        "## Results",
        "",
        "All arms use the same action case and timer boundary. Each arm reports its screenshot "
        "representation, warmup count, and measured count.",
        "",
        "| Case | Path | p50 (ms) | p95 (ms) | n | Status |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for arm in arms:
        summary = arm["summary_ms"]
        lines.append(
            f"| {arm['case_id']} | {arm['provider']} / {arm['path']} | "
            f"{summary['p50']:.2f} | {summary['p95']:.2f} | "
            f"{arm['measured_iterations']} | {arm['status']} |"
        )

    lines.extend(
        [
            "",
            "## Method",
            "",
            f"Case: `{workload['case_id']}`.",
            f"Action semantics: `{workload['action_semantics']}`.",
            f"Timer boundary: `{workload['timer_boundary']}`.",
            f"Warmup iterations: {workload['warmup_iterations']}. Measured iterations: "
            f"{workload['measured_iterations']}.",
            f"Screenshot policy: `{workload['screenshot_policy']}`.",
            f"Action payload SHA-256: `{workload['action_payload_sha256']}`.",
            "",
            "## Configuration",
            "",
            "Target CPU and memory describe the desktop target. Modal used the same resource "
            "shape for its placed caller and target. The benchmark did not measure the external "
            "callers' resources.",
            "",
            "| Provider | SDK | SDK retry policy | Caller | Requested region | Observed region | "
            "Screenshot | Target CPU (physical cores) | Target memory (MiB) | SDK calls | "
            "Transport requests |",
            "| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for arm in arms:
        topology = arm["topology"]
        sdk = arm["sdk"]
        resources = arm["resources"]
        shape = arm["request_shape"]
        screenshot = arm["screenshot"]
        lines.append(
            f"| {arm['provider']} | {sdk['package']} {sdk['version']} | "
            f"{sdk['retry_policy']} | {topology['caller']} | "
            f"{topology['requested_region']} | "
            f"{topology['observed_region']} | {_format_screenshot(screenshot)} | "
            f"{_format_cpu(resources['cpu'])} | "
            f"{_format_memory(resources['memory_mib'])} | {shape['sdk_calls']} | "
            f"{shape['transport_requests']} |"
        )

    lines.extend(
        [
            "",
            "## Evidence",
            "",
            f"Measurement source SHA: [`{payload['source_sha']}`](https://github.com/"
            f"ashtonchew/modal-computer-use/commit/{payload['source_sha']}).",
            f"Sanitized artifact: [external-provider-action-frame-{payload['evidence_date']}.json]"
            f"(../benchmark-data/external-provider-action-frame-{payload['evidence_date']}.json).",
            "",
            "Input artifact digests:",
            "",
        ]
    )
    for arm in arms:
        for digest in arm["input_artifact_digests"]:
            lines.append(f"- {arm['provider']} / {digest['role']}: `{digest['sha256']}`")

    lines.extend(
        [
            "",
            "All arms completed with zero failures, zero harness retries, zero replacement "
            "samples, and clean resource cleanup. Provider SDK retry policy is shown per arm.",
            "",
        ]
    )
    return "\n".join(lines)


def _assemble_step_arm(
    artifact: dict[str, Any],
    *,
    source_sha: str,
    input_digests: list[dict[str, str]],
    cleanup_verification: dict[str, Any],
) -> dict[str, Any]:
    if artifact.get("benchmark") != "computer-step-promotion":
        raise ActionFrameReportError("step artifact benchmark is unsupported")
    if artifact.get("status") != "complete":
        raise ActionFrameReportError("step artifact must be complete")
    if artifact.get("failures") != []:
        raise ActionFrameReportError("step artifact contains failures")
    if artifact.get("retries") != 0 or artifact.get("replacement_samples") != 0:
        raise ActionFrameReportError("step artifact contains retries or replacements")
    _require_exact_cleanup(artifact.get("cleanup"), "step artifact cleanup")
    preregistration = _mapping(artifact.get("preregistration"), "step preregistration")
    warmup = _require_nonnegative_int(
        preregistration.get("warmup_iterations"), "step warmup iterations"
    )
    measured = _require_positive_int(
        preregistration.get("samples_per_arm"), "step measured iterations"
    )
    if measured < 30:
        raise ActionFrameReportError("step artifact requires at least 30 measured samples")
    observations = artifact.get("observations")
    if not isinstance(observations, list) or len(observations) != measured:
        raise ActionFrameReportError("step artifact observations are incomplete")
    samples: list[float] = []
    for index, observation_value in enumerate(observations):
        observation = _mapping(observation_value, f"step observation {index}")
        if observation.get("status") != "ok":
            raise ActionFrameReportError(f"step observation {index} failed")
        if observation.get("borrow_count") != 1 or observation.get("connection_reused") is not True:
            raise ActionFrameReportError("step observation does not prove one borrow and reuse")
        if observation.get("frame_valid") is not True:
            raise ActionFrameReportError("step observation frame was not validated")
        timings = _mapping(observation.get("timings_ms"), f"step observation {index} timings")
        samples.append(
            _require_finite_nonnegative(
                timings.get("action_to_frame_ms"),
                f"step observation {index} action_to_frame_ms",
            )
        )
    configuration = _mapping(artifact.get("configuration"), "step configuration")
    if configuration.get("action_scenario") not in {
        None,
        "reset-pointer-then-click-unique-coordinate-v1",
    }:
        raise ActionFrameReportError("step action scenario is unsupported")
    _require_common_action_configuration(configuration, "step configuration")
    screenshot = _screenshot_from_configuration(configuration, "step screenshot")
    topology = _topology_from_configuration(configuration, "step topology")
    resources = _resources_from_configuration(configuration, "step resources")
    sdk = _sdk_from_configuration(configuration, "step SDK")
    cleanup = _cleanup_for_provider(cleanup_verification, "modal-daemon")
    return {
        "case_id": ACTION_FRAME_CASE_ID,
        "provider": "modal-daemon",
        "path": "computer.step",
        "source_sha": source_sha,
        "sdk": sdk,
        "topology": topology,
        "resources": resources,
        "screenshot": screenshot,
        "request_shape": {"sdk_calls": 1, "transport_requests": 1, "batching": "single-request"},
        "timer_boundary": ACTION_FRAME_TIMER_BOUNDARY,
        "warmup_iterations": warmup,
        "measured_iterations": measured,
        "samples_ms": samples,
        "summary_ms": _summary(samples),
        "failures": [],
        "cleanup": cleanup,
        "harness_retries": 0,
        "replacement_samples": 0,
        "input_artifact_digests": input_digests,
        "status": "measured",
    }


def _assemble_provider_arms(
    artifact: dict[str, Any],
    *,
    source_sha: str,
    input_digests: list[dict[str, str]],
    cleanup_verification: dict[str, Any],
) -> list[dict[str, Any]]:
    benchmark = artifact.get("benchmark")
    tracked_runner = benchmark == "external-provider-action-frame-run"
    if benchmark not in {"provider-compare", "external-provider-action-frame-run"}:
        raise ActionFrameReportError("provider artifact benchmark is unsupported")
    if tracked_runner:
        if artifact.get("status") != "eligible" or artifact.get("failures") != []:
            raise ActionFrameReportError("provider runner artifact is incomplete")
    elif artifact.get("status") not in {None, "ok", "eligible"} and artifact.get("ok") is not True:
        raise ActionFrameReportError("provider artifact is incomplete")
    providers = _mapping(artifact.get("providers"), "provider artifact providers")
    arms: list[dict[str, Any]] = []
    for provider, provider_value in providers.items():
        provider_result = _mapping(provider_value, f"provider {provider}")
        if tracked_runner:
            case_value = provider_result.get("case")
        else:
            cases = _mapping(provider_result.get("cases"), f"provider {provider} cases")
            case_value = cases.get("action_to_immediate_frame")
            if case_value is None:
                case_value = cases.get("action-to-immediate-frame")
        case = _mapping(case_value, f"provider {provider} action-to-frame case")
        if case.get("status") != "ok" or provider_result.get("status") != "ok":
            raise ActionFrameReportError(f"provider {provider} action-to-frame arm is incomplete")
        case_failures = case.get("failures", [] if tracked_runner else None)
        if provider_result.get("failures") != [] or case_failures != []:
            raise ActionFrameReportError(
                f"provider {provider} action-to-frame arm contains failures"
            )
        if case.get("case_id") != ACTION_FRAME_CASE_ID:
            raise ActionFrameReportError(f"provider {provider} case ID differs from workload")
        if case.get("action_semantics") != ACTION_FRAME_ACTION_SEMANTICS:
            raise ActionFrameReportError(
                f"provider {provider} action semantics differ from workload"
            )
        if case.get("action_payload_sha256") != ACTION_FRAME_ACTION_PAYLOAD_SHA256:
            raise ActionFrameReportError(
                f"provider {provider} action payload differs from workload"
            )
        if case.get("timer_boundary") != ACTION_FRAME_TIMER_BOUNDARY:
            raise ActionFrameReportError(
                f"provider {provider} timer boundary differs from workload"
            )
        _require_source_match(provider_result, case, source_sha, provider)
        samples = case.get("samples_ms")
        measured = _require_positive_int(
            case.get("iterations"), f"provider {provider} measured iterations"
        )
        warmup = _require_nonnegative_int(
            artifact.get("warmup_iterations"), f"provider {provider} warmup iterations"
        )
        if measured < 30 or not isinstance(samples, list) or len(samples) != measured:
            raise ActionFrameReportError(f"provider {provider} samples are incomplete")
        _validate_samples(samples, provider)
        metadata = _mapping(provider_result.get("metadata"), f"provider {provider} metadata")
        sdk = _sdk_from_provider(metadata, case, provider)
        topology = _topology_from_provider(metadata, case, provider)
        resources = _resources_from_provider(metadata, provider)
        screenshot = _mapping(case.get("screenshot"), f"provider {provider} screenshot")
        _validate_screenshot(screenshot)
        request_shape_value = _mapping(
            case.get("request_shape"), f"provider {provider} request shape"
        )
        request_shape = {
            key: request_shape_value[key]
            for key in ("sdk_calls", "transport_requests", "batching")
            if key in request_shape_value
        }
        _validate_request_shape(request_shape, provider)
        cleanup = _cleanup_for_provider(cleanup_verification, provider)
        harness_retries = case.get("harness_retries")
        if harness_retries != 0 or case.get("replacement_samples") != 0:
            raise ActionFrameReportError(f"provider {provider} has retries or replacements")
        arms.append(
            {
                "case_id": ACTION_FRAME_CASE_ID,
                "provider": provider,
                "path": _require_name(case.get("path"), f"provider {provider} path"),
                "source_sha": source_sha,
                "sdk": sdk,
                "topology": topology,
                "resources": resources,
                "screenshot": screenshot,
                "request_shape": request_shape,
                "timer_boundary": ACTION_FRAME_TIMER_BOUNDARY,
                "warmup_iterations": warmup,
                "measured_iterations": measured,
                "samples_ms": samples,
                "summary_ms": _summary(samples),
                "failures": [],
                "cleanup": cleanup,
                "harness_retries": 0,
                "replacement_samples": 0,
                "input_artifact_digests": input_digests,
                "status": "measured",
            }
        )
    if not arms:
        raise ActionFrameReportError("provider artifact has no complete action-to-frame arms")
    return arms


def _validate_workload(workload: dict[str, Any]) -> None:
    if workload.get("case_id") != ACTION_FRAME_CASE_ID:
        raise ActionFrameReportError("workload case_id is unsupported")
    if workload.get("action_semantics") != ACTION_FRAME_ACTION_SEMANTICS:
        raise ActionFrameReportError("workload action semantics are unsupported")
    _require_digest(workload.get("action_payload_sha256"), "workload action payload SHA-256")
    if workload.get("timer_boundary") != ACTION_FRAME_TIMER_BOUNDARY:
        raise ActionFrameReportError("workload timer boundary is unsupported")
    warmup = _require_nonnegative_int(workload.get("warmup_iterations"), "workload warmup")
    measured = _require_positive_int(workload.get("measured_iterations"), "workload measured")
    if measured < 30:
        raise ActionFrameReportError("workload requires at least 30 measured samples")
    if warmup > 1000:
        raise ActionFrameReportError("workload warmup count is too large")
    if workload.get("screenshot_policy") != "provider-native-full-frame":
        raise ActionFrameReportError("workload screenshot policy is unsupported")


def _validate_arm(
    arm: dict[str, Any],
    *,
    index: int,
    source_sha: str,
    workload: dict[str, Any],
    providers: set[str],
) -> None:
    _require_exact_keys(arm, _ARM_KEYS, f"arm {index}")
    case_id = arm.get("case_id")
    if case_id != ACTION_FRAME_CASE_ID:
        raise ActionFrameReportError(f"arm {index} case_id does not match workload")
    provider = _require_name(arm.get("provider"), f"arm {index} provider")
    if provider in providers:
        raise ActionFrameReportError(f"duplicate provider arm: {provider}")
    providers.add(provider)
    path = _require_name(arm.get("path"), f"arm {index} path")
    if path.lower().find("rank") >= 0 or path.lower().find("winner") >= 0:
        raise ActionFrameReportError(f"arm {index} path contains a comparison claim")
    if _require_source_sha(arm.get("source_sha"), f"arm {index} source_sha") != source_sha:
        raise ActionFrameReportError(f"arm {index} source SHA differs from report")
    _validate_sdk(_mapping(arm.get("sdk"), f"arm {index} sdk"), index)
    _validate_topology(_mapping(arm.get("topology"), f"arm {index} topology"), index)
    _validate_resources(_mapping(arm.get("resources"), f"arm {index} resources"), index)

    screenshot = _mapping(arm.get("screenshot"), f"arm {index} screenshot")
    _validate_screenshot(screenshot)

    _validate_request_shape(_mapping(arm.get("request_shape"), f"arm {index} request shape"), index)
    if arm.get("timer_boundary") != workload["timer_boundary"]:
        raise ActionFrameReportError(f"arm {index} timer boundary differs from workload")
    if arm.get("warmup_iterations") != workload["warmup_iterations"]:
        raise ActionFrameReportError(f"arm {index} warmup count differs from workload")
    if arm.get("measured_iterations") != workload["measured_iterations"]:
        raise ActionFrameReportError(f"arm {index} measured count differs from workload")

    if arm.get("status") != "measured":
        raise ActionFrameReportError(f"arm {index} status must be measured")
    samples = arm.get("samples_ms")
    if not isinstance(samples, list) or len(samples) != workload["measured_iterations"]:
        raise ActionFrameReportError(f"arm {index} sample count is incomplete")
    _validate_samples(samples, index)
    summary = _mapping(arm.get("summary_ms"), f"arm {index} summary")
    _require_exact_keys(summary, _SUMMARY_KEYS, f"arm {index} summary")
    expected = _summary(samples)
    for key in _SUMMARY_KEYS:
        value = _require_finite_nonnegative(summary.get(key), f"arm {index} {key}")
        if not math.isclose(value, expected[key], rel_tol=1e-9, abs_tol=1e-9):
            raise ActionFrameReportError(f"arm {index} {key} is not recomputed from samples")

    failures = arm.get("failures")
    if failures != []:
        raise ActionFrameReportError(f"arm {index} failures must be empty for eligible evidence")
    cleanup = _mapping(arm.get("cleanup"), f"arm {index} cleanup")
    _require_exact_keys(cleanup, _CLEANUP_KEYS, f"arm {index} cleanup")
    if cleanup != {"status": "clean", "survivors": 0}:
        raise ActionFrameReportError(f"arm {index} cleanup is incomplete")
    if arm.get("harness_retries") != 0:
        raise ActionFrameReportError(f"arm {index} harness retries must be zero")
    if arm.get("replacement_samples") != 0:
        raise ActionFrameReportError(f"arm {index} replacement samples must be zero")
    _validate_input_digests(arm.get("input_artifact_digests"), index)


def _validate_sdk(sdk: dict[str, Any], index: int | str) -> None:
    _require_exact_keys(sdk, _SDK_KEYS, f"arm {index} sdk")
    _require_version(sdk.get("package"), f"arm {index} SDK package")
    _require_version(sdk.get("version"), f"arm {index} SDK version")
    _require_name(sdk.get("retry_policy"), f"arm {index} SDK retry policy")


def _validate_topology(topology: dict[str, Any], index: int | str) -> None:
    _require_exact_keys(topology, _TOPOLOGY_KEYS, f"arm {index} topology")
    _require_name(topology.get("caller"), f"arm {index} caller")
    requested = _require_name(topology.get("requested_region"), f"arm {index} requested region")
    observed = _require_name(topology.get("observed_region"), f"arm {index} observed region")
    placement = topology.get("placement")
    if placement not in {"match", "provider-default"}:
        raise ActionFrameReportError(f"arm {index} placement must be match or provider-default")
    if placement == "match" and requested != observed:
        raise ActionFrameReportError(f"arm {index} requested and observed regions differ")
    if placement == "provider-default" and requested != "provider-default":
        raise ActionFrameReportError(f"arm {index} provider-default region is inconsistent")


def _validate_resources(resources: dict[str, Any], index: int | str) -> None:
    _require_exact_keys(resources, _RESOURCE_KEYS, f"arm {index} resources")
    availability = resources.get("availability")
    source = _require_name(resources.get("source"), f"arm {index} resource source")
    if availability not in {"recorded", "unavailable"}:
        raise ActionFrameReportError(f"arm {index} resource availability is unsupported")
    cpu = resources.get("cpu")
    memory = resources.get("memory_mib")
    if availability == "recorded":
        cpu_value = _require_finite_nonnegative(cpu, f"arm {index} CPU")
        if cpu_value <= 0:
            raise ActionFrameReportError(f"arm {index} CPU must be positive")
        if isinstance(memory, bool) or not isinstance(memory, int) or memory <= 0:
            raise ActionFrameReportError(f"arm {index} memory_mib must be a positive integer")
    elif cpu is not None or memory is not None:
        raise ActionFrameReportError(
            f"arm {index} unavailable resources must use null CPU and memory_mib"
        )
    if availability == "unavailable" and source != "provider-not-disclosed":
        raise ActionFrameReportError(
            f"arm {index} unavailable resources require provider-not-disclosed source"
        )


def _validate_screenshot(screenshot: dict[str, Any]) -> None:
    _require_exact_keys(screenshot, _SCREENSHOT_KEYS, "screenshot")
    if screenshot.get("format") not in {"png", "jpeg"}:
        raise ActionFrameReportError("screenshot format must be png or jpeg")
    width = screenshot.get("width")
    height = screenshot.get("height")
    if (width is None) != (height is None):
        raise ActionFrameReportError("screenshot dimensions must both be recorded or unknown")
    if width is not None and (
        isinstance(width, bool) or not isinstance(width, int) or width <= 0
    ):
        raise ActionFrameReportError("screenshot width must be positive or unknown")
    if height is not None and (
        isinstance(height, bool) or not isinstance(height, int) or height <= 0
    ):
        raise ActionFrameReportError("screenshot height must be positive or unknown")
    if screenshot.get("show_cursor") is not None and not isinstance(
        screenshot.get("show_cursor"), bool
    ):
        raise ActionFrameReportError("screenshot show_cursor must be boolean or unknown")


def _validate_request_shape(request_shape: dict[str, Any], index: int | str) -> None:
    _require_exact_keys(request_shape, _REQUEST_SHAPE_KEYS, f"arm {index} request shape")
    for key in ("sdk_calls", "transport_requests"):
        value = request_shape.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ActionFrameReportError(f"arm {index} {key} must be a positive integer")
    if request_shape.get("batching") not in {"single-request", "sequential-requests"}:
        raise ActionFrameReportError(f"arm {index} batching shape is unsupported")


def _validate_samples(samples: list[Any], index: int) -> None:
    for sample in samples:
        _require_finite_nonnegative(sample, f"arm {index} sample")


def _validate_input_digests(value: Any, index: int) -> None:
    if not isinstance(value, list) or not value:
        raise ActionFrameReportError(f"arm {index} input artifact digests are required")
    roles: set[str] = set()
    for digest_index, item_value in enumerate(value):
        item = _mapping(item_value, f"arm {index} input digest {digest_index}")
        _require_exact_keys(item, _DIGEST_KEYS, f"arm {index} input digest {digest_index}")
        role = _require_name(item.get("role"), f"arm {index} input digest role")
        if role in roles:
            raise ActionFrameReportError(f"arm {index} input digest roles must be unique")
        roles.add(role)
        _require_digest(item.get("sha256"), f"arm {index} input artifact SHA-256")


def _validated_input_digest_map(value: dict[str, str]) -> list[dict[str, str]]:
    if not isinstance(value, dict):
        raise ActionFrameReportError("assembly input artifact digests must be an object")
    expected_roles = {"step_candidate", "provider_compare", "cleanup_verification"}
    if set(value) != expected_roles:
        raise ActionFrameReportError("assembly input artifact digest roles are incomplete")
    result: list[dict[str, str]] = []
    for role in sorted(expected_roles):
        digest = _require_digest(value.get(role), f"assembly {role} SHA-256")
        result.append({"role": role, "sha256": digest})
    return result


def _validate_input_source_sha(value: dict[str, Any], source_sha: str, label: str) -> None:
    if _require_source_sha(value.get("source_sha"), f"{label} source_sha") != source_sha:
        raise ActionFrameReportError(f"{label} source SHA differs from assembly source")


def _validate_step_input_source_sha(value: dict[str, Any], source_sha: str) -> None:
    direct = value.get("source_sha")
    if direct is not None:
        _validate_input_source_sha(value, source_sha, "step artifact")
        return
    configuration = _mapping(value.get("configuration"), "step configuration")
    image_identity = configuration.get("image_identity")
    expected_prefix = f"inline-source-{source_sha}-config-"
    if not isinstance(image_identity, str) or not image_identity.startswith(expected_prefix):
        raise ActionFrameReportError("step artifact image identity differs from assembly source")


def _validate_cleanup_verification(value: dict[str, Any], source_sha: str) -> None:
    _require_exact_keys(value, _CLEANUP_VERIFICATION_KEYS, "cleanup verification")
    if (
        _require_source_sha(value.get("source_sha"), "cleanup verification source_sha")
        != source_sha
    ):
        raise ActionFrameReportError("cleanup verification source SHA differs from assembly source")
    providers = _mapping(value.get("providers"), "cleanup verification providers")
    if not providers:
        raise ActionFrameReportError("cleanup verification has no providers")
    for provider, result_value in providers.items():
        result = _mapping(result_value, f"cleanup verification {provider}")
        _require_verification_cleanup(result, f"cleanup verification {provider}")


def _require_exact_cleanup(value: Any, label: str) -> None:
    cleanup = _mapping(value, label)
    _require_exact_keys(cleanup, {"attempted", "succeeded", "survivors"}, label)
    if cleanup != {"attempted": True, "succeeded": True, "survivors": 0}:
        raise ActionFrameReportError(f"{label} is incomplete")


def _cleanup_for_provider(value: dict[str, Any], provider: str) -> dict[str, Any]:
    providers = _mapping(value.get("providers"), "cleanup verification providers")
    aliases = (provider, "modal-daemon") if provider == "modal" else (provider,)
    selected: Any = None
    for alias in aliases:
        if alias in providers:
            selected = providers[alias]
            break
    if selected is None:
        raise ActionFrameReportError(f"cleanup verification is missing provider {provider}")
    result = _mapping(selected, f"cleanup verification {provider}")
    _require_verification_cleanup(result, f"cleanup verification {provider}")
    return {"status": "clean", "survivors": 0}


def _require_verification_cleanup(value: dict[str, Any], label: str) -> None:
    if set(value) == {"status", "survivors"}:
        if value != {"status": "clean", "survivors": 0}:
            raise ActionFrameReportError(f"{label} is incomplete")
        return
    _require_exact_cleanup(value, label)


def _validate_report_date(value: str) -> None:
    if not isinstance(value, str) or _DATE_RE.fullmatch(value) is None:
        raise ActionFrameReportError("assembly evidence_date must use YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ActionFrameReportError("assembly evidence_date is not a calendar date") from exc


def _require_common_action_configuration(configuration: dict[str, Any], label: str) -> None:
    if configuration.get("action_payload_sha256") != ACTION_FRAME_ACTION_PAYLOAD_SHA256:
        raise ActionFrameReportError(f"{label} action payload differs from workload")
    if configuration.get("operation_transport") != "computer-step-envelope-v1":
        raise ActionFrameReportError(f"{label} operation transport is unsupported")


def _screenshot_from_configuration(configuration: dict[str, Any], label: str) -> dict[str, Any]:
    raw = _mapping(configuration.get("screenshot"), label)
    for key in ("format", "show_cursor"):
        if key not in raw:
            raise ActionFrameReportError(f"{label} is missing {key}")
    screenshot = {
        "format": raw["format"],
        "width": raw.get("width"),
        "height": raw.get("height"),
        "show_cursor": raw["show_cursor"],
    }
    _validate_screenshot(screenshot)
    return screenshot


def _topology_from_configuration(configuration: dict[str, Any], label: str) -> dict[str, str]:
    caller = configuration.get("caller_topology")
    requested = _mapping(configuration.get("requested_placement"), f"{label} requested placement")
    observed = _mapping(configuration.get("observed_placement"), f"{label} observed placement")
    requested_region = _require_name(requested.get("region"), f"{label} requested region")
    observed_target = _mapping(observed.get("target"), f"{label} observed target placement")
    observed_region = _require_name(observed_target.get("region"), f"{label} observed region")
    if requested_region != observed_region:
        raise ActionFrameReportError(f"{label} requested and observed regions differ")
    if caller != "one-application-owned-modal-function":
        raise ActionFrameReportError(f"{label} caller topology is unsupported")
    topology = {
        "caller": "application-owned-modal-function",
        "requested_region": requested_region,
        "observed_region": observed_region,
        "placement": "match",
    }
    _validate_topology(topology, label)
    return topology


def _resources_from_configuration(configuration: dict[str, Any], label: str) -> dict[str, Any]:
    raw = _mapping(configuration.get("resources"), f"{label} resources")
    function = _mapping(raw.get("function"), f"{label} function resources")
    sandbox = _mapping(raw.get("sandbox"), f"{label} sandbox resources")
    cpu = _require_finite_nonnegative(function.get("cpu"), f"{label} CPU")
    memory = sandbox.get("memory_mib")
    if function.get("cpu") != sandbox.get("cpu") or function.get("memory_mib") != memory:
        raise ActionFrameReportError(f"{label} function and target resources differ")
    result = {
        "cpu": cpu,
        "memory_mib": memory,
        "source": "benchmark-config",
        "availability": "recorded",
    }
    _validate_resources(result, 0)
    return result


def _sdk_from_configuration(configuration: dict[str, Any], label: str) -> dict[str, str]:
    sdk_value = configuration.get("sdk")
    if sdk_value is None:
        try:
            package_version = version("modal-computer-use")
        except PackageNotFoundError as exc:
            raise ActionFrameReportError(
                "modal-computer-use package version is unavailable"
            ) from exc
        sdk_value = {
            "package": "modal-computer-use",
            "version": package_version,
            "retry_policy": "no-mutation-retry",
        }
    sdk = _mapping(sdk_value, f"{label} SDK")
    result = {
        "package": _require_version(sdk.get("package"), f"{label} SDK package"),
        "version": _require_version(sdk.get("version"), f"{label} SDK version"),
        "retry_policy": _require_name(sdk.get("retry_policy"), f"{label} SDK retry policy"),
    }
    _validate_sdk(result, 0)
    return result


def _require_source_match(
    provider_result: dict[str, Any], case: dict[str, Any], source_sha: str, provider: str
) -> None:
    values = []
    for value, label in (
        (provider_result.get("source_sha"), "provider"),
        (case.get("source_sha"), "case"),
    ):
        if value is not None:
            values.append(_require_source_sha(value, f"provider {provider} {label} source_sha"))
    if not values or any(value != source_sha for value in values):
        raise ActionFrameReportError(f"provider {provider} source SHA is missing or differs")


def _sdk_from_provider(
    metadata: dict[str, Any], case: dict[str, Any], provider: str
) -> dict[str, str]:
    raw = case.get("sdk")
    if not isinstance(raw, dict):
        raw = {
            "package": metadata.get("sdk_package"),
            "version": metadata.get("sdk_version"),
            "retry_policy": metadata.get("sdk_retry_policy", "provider-default"),
        }
    result = {
        "package": _require_version(raw.get("package"), f"provider {provider} SDK package"),
        "version": _require_version(raw.get("version"), f"provider {provider} SDK version"),
        "retry_policy": str(raw.get("retry_policy", "provider-default")).replace("_", "-"),
    }
    result["retry_policy"] = _require_name(
        result["retry_policy"], f"provider {provider} SDK retry policy"
    )
    _validate_sdk(result, provider)
    return result


def _topology_from_provider(
    metadata: dict[str, Any], case: dict[str, Any], provider: str
) -> dict[str, str]:
    raw = case.get("topology")
    if not isinstance(raw, dict):
        raw = metadata.get("topology")
    if not isinstance(raw, dict):
        raise ActionFrameReportError(f"provider {provider} topology is missing")
    result = {
        "caller": _require_name(raw.get("caller"), f"provider {provider} caller"),
        "requested_region": _require_name(
            raw.get("requested_region"), f"provider {provider} requested region"
        ),
        "observed_region": _require_name(
            raw.get("observed_region"), f"provider {provider} observed region"
        ),
        "placement": _require_name(raw.get("placement"), f"provider {provider} placement"),
    }
    _validate_topology(result, provider)
    return result


def _resources_from_provider(metadata: dict[str, Any], provider: str) -> dict[str, Any]:
    raw = metadata.get("resources")
    if isinstance(raw, dict):
        result = {
            "cpu": raw.get("cpu"),
            "memory_mib": raw.get("memory_mib"),
            "source": raw.get("source", "provider-metadata"),
            "availability": raw.get("availability"),
        }
    else:
        cpu_count = metadata.get("cpu_count")
        memory_gib = metadata.get("memory_gib")
        if isinstance(cpu_count, int | float) and not isinstance(cpu_count, bool) and isinstance(
            memory_gib, int | float
        ) and not isinstance(memory_gib, bool):
            result = {
                "cpu": float(cpu_count),
                "memory_mib": int(float(memory_gib) * 1024),
                "source": "provider-metadata",
                "availability": "recorded",
            }
        else:
            result = {
                "cpu": None,
                "memory_mib": None,
                "source": "provider-not-disclosed",
                "availability": "unavailable",
            }
    _validate_resources(result, provider)
    return result


def _summary(samples: list[Any]) -> dict[str, float]:
    ordered = sorted(float(value) for value in samples)
    rank = 0.95 * (len(ordered) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    p95 = ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)
    return {"p50": float(statistics.median(ordered)), "p95": float(p95)}


def _format_cpu(value: Any) -> str:
    return "Not disclosed" if value is None else f"{float(value):.1f}"


def _format_memory(value: Any) -> str:
    return "Not disclosed" if value is None else str(value)


def _format_screenshot(value: dict[str, Any]) -> str:
    width = value["width"]
    height = value["height"]
    dimensions = (
        f"{width} x {height}"
        if isinstance(width, int) and isinstance(height, int)
        else "dimensions not recorded"
    )
    cursor = value["show_cursor"]
    cursor_label = (
        "cursor shown"
        if cursor is True
        else "cursor hidden"
        if cursor is False
        else "cursor setting not reported"
    )
    return f"{value['format'].upper()}; {dimensions}; {cursor_label}"


def _reject_unsafe_values(value: Any, *, key: str | None = None) -> None:
    if key is not None:
        normalized = _normalise_key(key)
        if normalized in _SECRET_KEY_NAMES or normalized in _RAW_ERROR_KEY_NAMES:
            raise ActionFrameReportError(f"secret or raw error field is forbidden: {key}")
        if normalized.endswith("_id") and normalized != "case_id":
            raise ActionFrameReportError(f"ephemeral or resource ID field is forbidden: {key}")
    if isinstance(value, dict):
        for item_key, item in value.items():
            if not isinstance(item_key, str):
                raise ActionFrameReportError("artifact keys must be strings")
            _reject_unsafe_values(item, key=item_key)
    elif isinstance(value, list):
        for item in value:
            _reject_unsafe_values(item)
    elif isinstance(value, str):
        lowered = value.lower()
        if lowered.startswith(("http://", "https://", "artifact://")):
            raise ActionFrameReportError("URLs are forbidden in sanitized evidence")
        if "authorization: bearer " in lowered or "bearer " in lowered:
            raise ActionFrameReportError("bearer credentials are forbidden in sanitized evidence")


def _require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    unknown = actual - expected
    missing = expected - actual
    if unknown:
        raise ActionFrameReportError(f"{label} has unknown field(s): {', '.join(sorted(unknown))}")
    if missing:
        raise ActionFrameReportError(f"{label} is missing field(s): {', '.join(sorted(missing))}")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ActionFrameReportError(f"{label} must be an object")
    return value


def _require_source_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SOURCE_SHA_RE.fullmatch(value) is None:
        raise ActionFrameReportError(f"{label} must be a full 40-character Git SHA")
    return value


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ActionFrameReportError(f"{label} must be a SHA-256 digest")
    return value


def _require_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or _NAME_RE.fullmatch(value) is None:
        raise ActionFrameReportError(f"{label} must be a safe name")
    return value


def _require_version(value: Any, label: str) -> str:
    if not isinstance(value, str) or _VERSION_RE.fullmatch(value) is None:
        raise ActionFrameReportError(f"{label} must be a safe version")
    return value


def _require_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ActionFrameReportError(f"{label} must be a nonnegative integer")
    return value


def _require_positive_int(value: Any, label: str) -> int:
    result = _require_nonnegative_int(value, label)
    if result == 0:
        raise ActionFrameReportError(f"{label} must be positive")
    return result


def _require_finite_nonnegative(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ActionFrameReportError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ActionFrameReportError(f"{label} must be finite and nonnegative")
    return result


def _normalise_key(value: str) -> str:
    return value.strip().lower().replace("-", "_")
