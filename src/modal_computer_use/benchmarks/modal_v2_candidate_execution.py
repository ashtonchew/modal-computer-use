from __future__ import annotations

import asyncio
import json
import time
from textwrap import dedent
from typing import Any

from ..config import (
    ActionConfig,
    BrowserConfig,
    ComputerConfig,
    ImageConfig,
    ResourceConfig,
    RuntimeConfig,
)
from ..latency import SessionStartupTiming
from ..sandbox import (
    ModalCandidateRunner,
    create_modal_candidate_allocation_context,
    create_modal_candidate_computer,
    create_modal_candidate_runner,
)
from ..state import new_run_id
from .modal_v2_candidate import (
    ARM_V1_CONNECT,
    ARM_V1_TUNNEL,
    ARM_V2_I6PN,
    ARM_V2_TUNNEL,
    ModalV2CandidateConfig,
    arm_definitions,
)

CANDIDATE_RESULT_START = "__MODAL_V2_CANDIDATE_RESULT_START__"
CANDIDATE_RESULT_END = "__MODAL_V2_CANDIDATE_RESULT_END__"


def run_candidate_phase(
    config: ModalV2CandidateConfig,
    *,
    schedule: list[dict[str, Any]],
    app_name: str = "modal-computer-use-v2-candidate",
    runner_factory: Any = create_modal_candidate_runner,
    progress: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    run_id = new_run_id()
    runner: ModalCandidateRunner | None = None
    trials: list[dict[str, Any]] = []
    runner_cleanup = False
    try:
        runner = runner_factory(
            app_name=app_name,
            cloud=config.cloud,
            region=config.region,
            image_revision=config.image_revision,
            app_tags={"benchmark": "modal-v2-candidate"},
            tags={"benchmark_run": run_id[:16]},
        )
        for item in schedule:
            trial = run_candidate_trial(
                config,
                schedule_item=item,
                runner=runner,
                app_name=app_name,
                run_id=run_id,
            )
            trials.append(trial)
            if progress is not None:
                progress(item["phase"], len(trials), len(schedule), item["arm"])
    finally:
        if runner is not None:
            runner_cleanup = runner.terminate()
        for trial in trials:
            cleanup = trial.get("cleanup")
            if isinstance(cleanup, dict):
                cleanup["runner_terminated"] = runner_cleanup
    return trials, {
        "runner_backend": "v2",
        "runner_i6pn_enabled": True,
        "runner_resources": {"cpu": 1.0, "memory_mib": 1024},
        "runner_image_identity": f"modal-computer-use-{config.browser}:{config.image_revision}",
        "runner_placement": {} if runner is None else dict(runner.placement),
        "runner_cleanup_succeeded": runner_cleanup,
        "runner_reused_across_interleaved_trials": True,
        "broker_on_action_or_frame_path": False,
    }


def run_candidate_throughput(
    config: ModalV2CandidateConfig,
    *,
    app_name: str = "modal-computer-use-v2-candidate-throughput",
) -> list[dict[str, Any]]:
    concurrency = list(config.throughput_concurrency)
    if config.enable_concurrency_50:
        concurrency.append(50)
    total_allocations = 2 * sum(concurrency)
    per_second = (config.cpu * 0.00003942 + (config.memory_mib / 1024) * 0.00000667) * 1.75
    estimated_cost_ceiling = total_allocations * config.sandbox_timeout_seconds * per_second
    if estimated_cost_ceiling > config.max_estimated_cost_usd:
        raise RuntimeError("throughput cost ceiling exceeds the preregistered capacity/cost gate")
    context = create_modal_candidate_allocation_context(
        app_name=app_name,
        image_revision=config.image_revision,
        cloud=config.cloud,
        region=config.region,
        cpu=config.cpu,
        memory_mib=config.memory_mib,
    )

    async def execute() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for count in concurrency:
            for backend in ("v1", "v2"):
                row = await context.run_batch(
                    backend=backend,
                    concurrency=count,
                    timeout_seconds=config.sandbox_timeout_seconds,
                )
                row["classification"] = "minimal-container-allocation-throughput"
                row["image_identity"] = (
                    f"modal-computer-use-{config.browser}:{config.image_revision}"
                )
                row["requested_cloud"] = config.cloud
                row["requested_region"] = config.region
                row["requested_cpu"] = config.cpu
                row["requested_memory_mib"] = config.memory_mib
                rows.append(row)
        return rows

    return asyncio.run(execute())


def run_candidate_trial(
    config: ModalV2CandidateConfig,
    *,
    schedule_item: dict[str, Any],
    runner: ModalCandidateRunner,
    app_name: str,
    run_id: str,
    clock: Any = time.perf_counter,
) -> dict[str, Any]:
    arm = str(schedule_item["arm"])
    definitions = arm_definitions()
    if arm not in definitions:
        raise ValueError("schedule contains an unsupported arm")
    definition = definitions[arm]
    backend = definition["backend"]
    transport = definition["ingress"]
    target: Any | None = None
    timing = SessionStartupTiming(clock=clock)
    started = clock()
    failure: dict[str, Any] | None = None
    status = "failed"
    metrics: dict[str, float | None] = {
        key: None
        for key in (
            "allocation_ms",
            "daemon_ready_ms",
            "browser_ready_ms",
            "first_valid_frame_ms",
            "warm_action_to_frame_ms",
        )
    }
    target_terminated = False
    target_detached = False
    actual = {
        "target_cloud": None,
        "target_region": None,
        "runner_cloud": runner.placement.get("cloud"),
        "runner_region": runner.placement.get("region"),
        "i6pn_reachability": None,
    }
    verification = _empty_verification()
    try:
        target_config = _target_config(config, arm=arm, run_id=run_id, schedule_item=schedule_item)
        target = create_modal_candidate_computer(
            config=target_config,
            backend=backend,
            transport=transport,
            cloud=config.cloud,
            app_name=app_name,
            app_tags={"benchmark": "modal-v2-candidate"},
            tags={"benchmark_arm": arm},
            wait=True,
            timing=timing,
        )
        stages = timing.as_dict()["stages"]
        metrics["allocation_ms"] = _observed_elapsed(stages, "sandbox_registered")
        placement = target.runtime_placement()
        actual["target_cloud"] = placement["cloud"]
        actual["target_region"] = placement["region"]
        runner_dispatch_offset_ms = (clock() - started) * 1000.0
        runner_result = runner.execute(
            target,
            ("python", "-c", modal_candidate_runner_code()),
            transport=transport,
            timeout_seconds=config.readiness_timeout_seconds + 120,
        )
        payload = extract_candidate_runner_result(runner_result.stdout)
        if payload.get("status") != "valid":
            raise RuntimeError(str(payload.get("error_type") or "candidate runner failed"))
        runner_stages = payload["stages_ms"]
        metrics.update(
            {
                "daemon_ready_ms": runner_dispatch_offset_ms + float(runner_stages["daemon_ready"]),
                "browser_ready_ms": runner_dispatch_offset_ms
                + float(runner_stages["browser_ready"]),
                "first_valid_frame_ms": (
                    runner_dispatch_offset_ms + float(runner_stages["first_valid_frame"])
                ),
                "warm_action_to_frame_ms": float(payload["warm_action_to_frame_ms"]),
            }
        )
        verification = dict(payload["verification"])
        actual["runner_cloud"] = payload["placement"].get("cloud")
        actual["runner_region"] = payload["placement"].get("region")
        actual["i6pn_reachability"] = (
            "verified-workspace-private-direct" if arm == ARM_V2_I6PN else "not-applicable"
        )
        status = "valid"
    except Exception as exc:
        failure = {"phase": "trial", "error_type": type(exc).__name__}
        status = "timeout" if isinstance(exc, TimeoutError) else "failed"
    finally:
        resource_duration_seconds = max(0.0, clock() - started)
        if target is not None:
            try:
                target.terminate(wait=True)
            except Exception:
                target_terminated = False
            else:
                target_terminated = True
            try:
                target.detach()
            except Exception:
                target_detached = False
            else:
                target_detached = True
    requested = _requested_controls(config, arm=arm)
    return {
        "sequence": schedule_item["sequence"],
        "phase": schedule_item["phase"],
        "arm": arm,
        "lifecycle_index": schedule_item["lifecycle_index"],
        "status": status,
        "metrics": metrics,
        "requested": requested,
        "actual": actual,
        "verification": verification,
        "retry_count": 0,
        "failure": failure,
        "cleanup": {
            "target_terminated": target_terminated,
            "target_detached": target_detached,
            "runner_terminated": None,
            "runner_cleanup_scope": "phase",
        },
        "resource_duration_seconds": resource_duration_seconds,
        "estimated_billed_cost": _estimated_target_cost(config, resource_duration_seconds),
        "optimizations": list(definition["optimizations"]),
    }


def modal_candidate_runner_code() -> str:
    return dedent(f"""\
import json
import os
import time

from modal_computer_use import ComputerSandbox
from modal_computer_use.benchmarks.observation_surface import (
    CLICK_TOGGLE_ACTION,
    _open_click_toggle_page,
)
from modal_computer_use.latency import validate_first_frame


def elapsed(started):
    return (time.perf_counter() - started) * 1000.0


started = time.perf_counter()
result = {{"status": "failed"}}
computer = None
try:
    computer = ComputerSandbox.local(
        base_url=os.environ["COMPUTER_USE_DAEMON_BASE_URL"],
        token=os.environ.get("COMPUTER_USE_DAEMON_TOKEN") or None,
        timeout=180.0,
    )
    probes = {{
        "healthz": computer.client.get_json("/healthz"),
        "readyz": computer.client.get_json("/readyz"),
        "version": computer.client.get_json("/v1/version"),
        "capabilities": computer.client.get_json("/v1/capabilities"),
    }}
    daemon_ready_ms = elapsed(started)
    browser = computer.browser.status()
    browser_ready_ms = elapsed(started)
    first_frame = computer.screenshots.full_bytes(format="png", processing="daemon")
    validate_first_frame(first_frame, expected_width=1024, expected_height=768, image_format="png")
    first_valid_frame_ms = elapsed(started)
    _open_click_toggle_page(computer.client)
    with computer.observation_stream(
        fps=0.01,
        frame_encoding="binary-envelope",
        timeout=180.0,
    ) as stream:
        warmup = stream.act_and_observe(
            actions=[CLICK_TOGGLE_ACTION],
            source="modal-v2-candidate-warmup",
            change_timeout_ms=200,
            poll_strategy="adaptive",
            change_detection="auto_region",
            change_signal="auto",
            frame_encoding="binary-envelope",
        )
        warmup.require_valid_frame(require_change=True)
        measured = stream.act_and_observe(
            actions=[CLICK_TOGGLE_ACTION],
            source="modal-v2-candidate-measured",
            change_timeout_ms=200,
            poll_strategy="adaptive",
            change_detection="auto_region",
            change_signal="auto",
            frame_encoding="binary-envelope",
        )
        measured.require_valid_frame(require_change=True)
    metadata = measured.frame.metadata
    action_result = metadata.get("action_result") or {{}}
    result = {{
        "status": "valid",
        "stages_ms": {{
            "daemon_ready": daemon_ready_ms,
            "browser_ready": browser_ready_ms,
            "first_valid_frame": first_valid_frame_ms,
        }},
        "warm_action_to_frame_ms": measured.elapsed_ms,
        "placement": {{
            "cloud": os.environ.get("MODAL_CLOUD_PROVIDER") or None,
            "region": os.environ.get("MODAL_REGION") or None,
        }},
        "verification": {{
            "healthz": probes["healthz"].get("ok") is True,
            "readyz": probes["readyz"].get("ready") is True,
            "version": isinstance(probes["version"].get("version"), str),
            "capabilities": (
                isinstance(probes["capabilities"].get("action_types"), list)
                and isinstance(probes["capabilities"].get("screenshot_formats"), list)
                and probes["capabilities"].get("image_profile") == "browser"
            ),
            "browser": (
                browser.get("configured_browser") == "chromium"
                and (browser.get("prewarm_result") or {{}}).get("ok") is True
                and isinstance(browser.get("windows"), int)
                and browser.get("windows") >= 1
            ),
            "frame": True,
            "action": action_result.get("ok") is True,
            "causal_frame": metadata.get("causal_frame") is True,
            "changed_frame": (
                metadata.get("change_detected") is True
                and metadata.get("change_timeout_reached") is not True
            ),
            "binary_envelope": metadata.get("frame_encoding") == "binary-envelope",
        }},
    }}
except Exception as exc:
    result = {{"status": "failed", "error_type": type(exc).__name__}}
finally:
    if computer is not None:
        computer.client.close()
print("{CANDIDATE_RESULT_START}")
print(json.dumps(result, sort_keys=True))
print("{CANDIDATE_RESULT_END}")
""")


def extract_candidate_runner_result(stdout: str) -> dict[str, Any]:
    start = stdout.find(CANDIDATE_RESULT_START)
    end = stdout.find(CANDIDATE_RESULT_END)
    if start < 0 or end < 0 or end <= start:
        raise ValueError("candidate runner did not emit a bounded result")
    raw = stdout[start + len(CANDIDATE_RESULT_START) : end].strip()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("candidate runner result must be an object")
    return payload


def _target_config(
    config: ModalV2CandidateConfig,
    *,
    arm: str,
    run_id: str,
    schedule_item: dict[str, Any],
) -> ComputerConfig:
    ingress = "connect" if arm == ARM_V1_CONNECT else "tunnel"
    return ComputerConfig(
        runtime=RuntimeConfig(
            timeout_seconds=config.sandbox_timeout_seconds,
            readiness_timeout_seconds=config.readiness_timeout_seconds,
            modal_region=config.region,
        ),
        resources=ResourceConfig(
            profile="browser",
            cpu=config.cpu,
            memory_mib=config.memory_mib,
        ),
        image=ImageConfig(source="named", revision=config.image_revision),
        browser=BrowserConfig(kind="chromium", prewarm=True),
        actions=ActionConfig(input_rate_limit_per_sec=0),
        run_id=(f"{run_id}-{schedule_item['phase']}-{schedule_item['sequence']:03d}-{arm[:12]}"),
        ingress=ingress,
    )


def _requested_controls(config: ModalV2CandidateConfig, *, arm: str) -> dict[str, Any]:
    definition = arm_definitions()[arm]
    return {
        "backend": definition["backend"],
        "caller_path": definition["caller_path"],
        "ingress": definition["ingress"],
        "action_transport": definition["action_transport"],
        "observation_transport": definition["observation_transport"],
        "cloud": config.cloud,
        "region": config.region,
        "cpu": config.cpu,
        "memory_mib": config.memory_mib,
        "image_identity": f"modal-computer-use-{config.browser}:{config.image_revision}",
        "browser": config.browser,
        "browser_prewarm": config.browser_prewarm,
        "width": config.width,
        "height": config.height,
        "readiness_boundary": "runner-direct-authenticated-daemon-browser-frame",
        "action_semantics": "click-512-512-left",
        "observation_semantics": "changed-causal-png-binary-envelope",
        "cleanup_policy": "terminate-target-runner-and-detach-target",
        "pool_policy": "none",
        "snapshot_policy": "none",
    }


def _empty_verification() -> dict[str, bool]:
    return {
        key: False
        for key in (
            "healthz",
            "readyz",
            "version",
            "capabilities",
            "browser",
            "frame",
            "action",
            "causal_frame",
            "changed_frame",
            "binary_envelope",
        )
    }


def _observed_elapsed(stages: dict[str, Any], name: str) -> float | None:
    stage = stages.get(name)
    if not isinstance(stage, dict) or stage.get("status") != "observed":
        return None
    value = stage.get("elapsed_ms")
    return float(value) if isinstance(value, int | float) else None


def _estimated_target_cost(
    config: ModalV2CandidateConfig,
    duration_seconds: float,
) -> dict[str, Any]:
    base = duration_seconds * (config.cpu * 0.00003942 + (config.memory_mib / 1024) * 0.00000667)
    multiplier = 1.75 if config.region == "us-west" else None
    return {
        "status": "partial" if multiplier is not None else "unknown",
        "estimated_usd": None if multiplier is None else base * multiplier,
        "duration_seconds": duration_seconds,
        "included": ["target_cpu", "target_memory"],
        "excluded": ["runner_compute", "control_plane", "billing_adjustments"],
    }


def candidate_backend_for_arm(arm: str) -> str:
    if arm in {ARM_V1_CONNECT, ARM_V1_TUNNEL}:
        return "v1"
    if arm in {ARM_V2_TUNNEL, ARM_V2_I6PN}:
        return "v2"
    raise ValueError("unknown candidate arm")
