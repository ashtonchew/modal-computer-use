from __future__ import annotations

import math
import os
import re
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..client import DaemonClient
from ..config import (
    ActionConfig,
    BrowserConfig,
    ComputerConfig,
    ImageConfig,
    ResourceConfig,
    RuntimeConfig,
)
from ..latency import validate_first_frame
from ..sandbox import (
    ComputerSandbox,
    cleanup_modal_benchmark_run,
    modal_daemon_endpoint,
    run_modal_benchmark_function_once,
)
from ..state import new_run_id
from .constants import COORDINATE_CLICK_SEQUENCE_ACTIONS, MOVE_CLICK_ACTIONS

ModalOptimizedIngress = Literal["connect", "attested-tunnel"]

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_ARMS: tuple[ModalOptimizedIngress, ...] = ("connect", "attested-tunnel")
_CASE_NAMES = (
    "transport_floor_0b",
    "screenshot_full_png",
    "move_and_click",
    "ordered_four_click_batch",
)
_RECURRING_CASES = (
    "screenshot_full_png",
    "move_and_click",
    "ordered_four_click_batch",
)
_SCHEDULE = "alternating paired rounds: connect/tunnel, tunnel/connect"
_WINNER_GATE = {
    "minimum_recurring_score_improvement_percent": 10.0,
    "minimum_recurring_case_wins": 2,
    "maximum_losing_case_regression_percent": 5.0,
    "transport_floor_decides_selection": False,
}
_UNSAFE_KEYS = {
    "access_key",
    "api_key",
    "authorization",
    "base_url",
    "bearer",
    "artifact_bytes",
    "clipboard_text",
    "credential",
    "credentials",
    "endpoint",
    "headers",
    "password",
    "private_key",
    "resource_id",
    "run_id",
    "sandbox_id",
    "screenshot",
    "screenshot_bytes",
    "secret",
    "secret_key",
    "stderr",
    "stdout",
    "token",
    "typed_text",
}
_UNSAFE_KEY_SUFFIXES = tuple(f"_{key}" for key in _UNSAFE_KEYS)


@dataclass(frozen=True, slots=True)
class ModalOptimizedIngressABConfig:
    region: str
    image_revision: str
    cpu: float = 4.0
    memory_mib: int = 8192
    browser: Literal["chromium"] = "chromium"
    iterations: int = 30
    warmup_iterations: int = 2
    pilot: bool = False
    app_name: str = "modal-computer-use"
    readiness_timeout_seconds: int = 120
    sandbox_timeout_seconds: int = 900

    def __post_init__(self) -> None:
        if not self.region.strip():
            raise ValueError("region must be explicit")
        if _COMMIT_RE.fullmatch(self.image_revision) is None:
            raise ValueError("image_revision must be a full Git commit")
        if self.cpu <= 0 or self.memory_mib < 128:
            raise ValueError("resource configuration is invalid")
        if self.iterations < 1 or self.warmup_iterations < 0:
            raise ValueError("iteration counts are invalid")
        if not self.pilot and (self.iterations != 30 or self.warmup_iterations != 2):
            raise ValueError("nonpublishable counts require pilot=True")


def run_modal_optimized_ingress_ab(
    config: ModalOptimizedIngressABConfig,
    *,
    function_launcher: Any = run_modal_benchmark_function_once,
    cleanup_sweep: Any = cleanup_modal_benchmark_run,
    run_id_factory: Callable[[], str] = new_run_id,
) -> dict[str, Any]:
    run_id = run_id_factory()
    failures: list[dict[str, Any]] = []
    runner_result: dict[str, Any] | None = None
    try:
        launched = function_launcher(
            run_modal_optimized_ingress_ab_in_runner,
            config=config,
            run_tag=run_id,
            app_name=config.app_name,
            region=config.region,
            image_revision=config.image_revision,
            cpu=config.cpu,
            memory_mib=config.memory_mib,
            timeout_seconds=max(900, (config.iterations + config.warmup_iterations) * 30),
            retries=0,
        )
        if isinstance(launched, dict):
            runner_result = launched
        else:
            failures.append(_failure("runner_dispatch", -1, "InvalidRunnerResult"))
    except Exception as exc:
        failures.append(_failure("runner_dispatch", -1, type(exc).__name__))

    try:
        cleanup = cleanup_sweep(
            app_name=config.app_name,
            run_id=run_id,
            include_inventory=True,
        )
    except Exception as exc:
        cleanup = {}
        failures.append(_failure("final_cleanup", -1, type(exc).__name__))
    cleanup_ok = (
        isinstance(cleanup, dict)
        and cleanup.get("cleanup_succeeded") is True
        and cleanup.get("remaining_sandboxes") == 0
    )
    if not cleanup_ok and not any(item["phase"] == "final_cleanup" for item in failures):
        failures.append(_failure("final_cleanup", -1, "CleanupFailed"))
    if runner_result is not None:
        failures.extend(_safe_failures(runner_result.get("failures"), "runner"))
    runner_ok = runner_result is not None and runner_result.get("ok") is True
    selection = _select_optimized_ingress(
        runner_result.get("cases", {}) if isinstance(runner_result, dict) else {}
    )
    publishable = not config.pilot and runner_ok and cleanup_ok and not failures
    result = {
        "schema_version": 1,
        "benchmark": "modal-optimized-ingress-ab",
        "ok": runner_ok and cleanup_ok and not failures,
        "eligibility": "publishable" if publishable else "pilot_ineligible",
        "iterations": config.iterations,
        "warmup_iterations": config.warmup_iterations,
        "replacement_samples": 0,
        "metadata": {
            "caller_topology": "one Modal Function and one target with matching observed placement",
            "runner_kind": "modal-function",
            "runner_invocations": 1,
            "target_count": 1,
            "modal_region": config.region,
            "daemon_http_version": "1.1",
            "image_revision": config.image_revision,
            "runner_cpu": config.cpu,
            "runner_memory_mib": config.memory_mib,
            "target_cpu": config.cpu,
            "target_memory_mib": config.memory_mib,
            "schedule": _SCHEDULE,
            "connection_reuse": "one persistent client per arm",
            "authorization_boundary": "completed before attested-tunnel warmup",
        },
        "run": runner_result or {},
        "selection": selection,
        "final_cleanup": {
            "cleanup_succeeded": cleanup_ok,
            "remaining_sandboxes": (
                cleanup.get("remaining_sandboxes") if isinstance(cleanup, dict) else None
            ),
        },
        "failures": failures,
    }
    validate_modal_optimized_ingress_ab_artifact(result, require_publishable=False)
    return result


def run_modal_optimized_ingress_ab_in_runner(
    config: ModalOptimizedIngressABConfig,
    *,
    run_tag: str = "remote-run",
    runner_placement: dict[str, str | None] | None = None,
    create_computer: Any = ComputerSandbox.create,
    connect_endpoint_factory: Any = modal_daemon_endpoint,
    client_factory: Any = DaemonClient,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    placement = runner_placement or {
        "cloud": os.environ.get("MODAL_CLOUD_PROVIDER"),
        "region": os.environ.get("MODAL_REGION"),
    }
    computer: Any | None = None
    connect_client: Any | None = None
    tunnel_client: Any | None = None
    failures: list[dict[str, Any]] = []
    cases: dict[str, Any] = {}
    placement_verified = False
    connect_authorization_setup = {"completed": False, "elapsed_ms": None}
    observed_placement: dict[str, Any] = {
        "requested_region": config.region,
        "runner": _safe_observed_placement(placement),
        "target": None,
    }
    cleanup = {"attempted": False, "succeeded": None, "error_type": None}
    try:
        computer_config = _computer_config(config, run_id=f"{run_tag}-target")
        computer = create_computer(
            config=computer_config,
            app_name=config.app_name,
            app_tags={"benchmark": "modal-optimized-ingress-ab"},
            tags={"benchmark": "modal-optimized-ingress-ab", "role": "target"},
            wait=True,
        )
        _validate_initial_frame(computer, computer_config)
        target_placement = computer.runtime_placement()
        observed_placement["target"] = _safe_observed_placement(target_placement)
        _require_matching_placement(placement, target_placement, requested_region=config.region)
        placement_verified = True

        endpoint = connect_endpoint_factory(computer, "connect")
        connect_client = client_factory(endpoint.base_url, token=endpoint.token, http2=False)
        authorization_started = clock()
        try:
            payload = connect_client.post_json("/v1/session/tunnel-authorize")
            tunnel_token = payload.get("token") if isinstance(payload, dict) else None
            if not isinstance(tunnel_token, str) or not tunnel_token:
                raise RuntimeError("daemon did not return an attested tunnel token")
        except Exception:
            connect_authorization_setup["elapsed_ms"] = max(
                0.0, (clock() - authorization_started) * 1000.0
            )
            raise
        connect_authorization_setup["elapsed_ms"] = max(
            0.0, (clock() - authorization_started) * 1000.0
        )
        connect_authorization_setup["completed"] = True
        tunnel_client = client_factory(
            computer.client.base_url,
            token=tunnel_token,
            http2=False,
        )
        clients: dict[ModalOptimizedIngress, Any] = {
            "connect": connect_client,
            "attested-tunnel": tunnel_client,
        }
        _preflight_clients(clients)
        cases = _run_interleaved_cases(
            clients,
            iterations=config.iterations,
            warmup_iterations=config.warmup_iterations,
            clock=clock,
        )
        failures.extend(_case_failures(cases))
    except Exception as exc:
        phase = (
            "authorization"
            if placement_verified and not connect_authorization_setup["completed"]
            else "runner"
        )
        failures.append(_failure(phase, -1, type(exc).__name__))
    finally:
        for client in (tunnel_client, connect_client):
            if client is not None:
                try:
                    client.close()
                except Exception as exc:
                    failures.append(_failure("client_cleanup", -1, type(exc).__name__))
        cleanup = _cleanup_target(computer)
        if cleanup.get("succeeded") is False:
            failures.append(_failure("target_cleanup", -1, str(cleanup["error_type"])))
    return {
        "ok": (
            placement_verified
            and connect_authorization_setup["completed"] is True
            and set(cases) == set(_CASE_NAMES)
            and cleanup.get("succeeded") is True
            and not failures
        ),
        "placement_verified": placement_verified,
        "placement": observed_placement,
        "connect_authorization_setup": connect_authorization_setup,
        "cases": cases,
        "target_cleanup": cleanup,
        "failures": failures,
    }


def _run_interleaved_cases(
    clients: dict[ModalOptimizedIngress, Any],
    *,
    iterations: int,
    warmup_iterations: int,
    clock: Callable[[], float],
) -> dict[str, Any]:
    factories: dict[str, Callable[[Any], Callable[[], dict[str, Any]]]] = {
        "transport_floor_0b": lambda client: lambda: _transport_floor_operation(client),
        "screenshot_full_png": lambda client: lambda: _screenshot_operation(client),
        "move_and_click": lambda client: lambda: _action_operation(
            client,
            actions=MOVE_CLICK_ACTIONS,
        ),
        "ordered_four_click_batch": lambda client: lambda: _action_operation(
            client,
            actions=COORDINATE_CLICK_SEQUENCE_ACTIONS,
        ),
    }
    cases: dict[str, Any] = {}
    for case_name, factory in factories.items():
        operations = {arm: factory(clients[arm]) for arm in _ARMS}
        samples: dict[ModalOptimizedIngress, list[float]] = {arm: [] for arm in _ARMS}
        failures: dict[ModalOptimizedIngress, list[dict[str, str | int]]] = {
            arm: [] for arm in _ARMS
        }
        observed_http_versions: dict[ModalOptimizedIngress, set[str]] = {
            arm: set() for arm in _ARMS
        }
        observed_input_backends: dict[ModalOptimizedIngress, set[str]] = {
            arm: set() for arm in _ARMS
        }
        for phase, rounds in (("warmup", warmup_iterations), ("measure", iterations)):
            for iteration in range(rounds):
                order = _ARMS if iteration % 2 == 0 else tuple(reversed(_ARMS))
                for arm in order:
                    started = clock()
                    try:
                        observation = operations[arm]()
                    except Exception as exc:
                        failures[arm].append(
                            {
                                "phase": phase,
                                "iteration": iteration,
                                "exception_type": type(exc).__name__,
                            }
                        )
                        continue
                    elapsed_ms = max(0.0, (clock() - started) * 1000.0)
                    http_version = observation.get("http_version")
                    if isinstance(http_version, str):
                        observed_http_versions[arm].add(http_version)
                    input_backend = observation.get("input_backend")
                    if isinstance(input_backend, str):
                        observed_input_backends[arm].add(input_backend)
                    if phase == "measure":
                        samples[arm].append(elapsed_ms)
        arms: dict[str, Any] = {}
        for arm in _ARMS:
            arm_failures = failures[arm]
            arms[arm] = {
                "status": (
                    "ok" if not arm_failures and len(samples[arm]) == iterations else "failed"
                ),
                "iterations": iterations,
                "successful_iterations": len(samples[arm]),
                "summary_ms": _summary(samples[arm]),
                "http_versions": sorted(observed_http_versions[arm]),
                "input_backends": sorted(observed_input_backends[arm]),
                "failures": arm_failures,
            }
        cases[case_name] = {
            "semantic": _case_semantic(case_name),
            "iterations_per_arm": iterations,
            "warmup_iterations_per_arm": warmup_iterations,
            "schedule": _SCHEDULE,
            "arms": arms,
            "comparison": _case_comparison(arms),
        }
    return cases


def _preflight_clients(clients: dict[ModalOptimizedIngress, Any]) -> None:
    for arm in _ARMS:
        result = clients[arm].get_json("/healthz")
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError(f"{arm} health preflight failed")


def _transport_floor_operation(client: Any) -> dict[str, Any]:
    payload, _headers = client.post_bytes_with_headers(
        "/v1/observations/transport-probe",
        json={"size_bytes": 0},
    )
    if payload != b"":
        raise RuntimeError("zero-byte transport probe returned a payload")
    return {"http_version": _require_http_11(client)}


def _screenshot_operation(client: Any) -> dict[str, Any]:
    payload, headers = client.post_bytes_with_headers(
        "/v1/screenshots/full/raw",
        json={"format": "png", "show_cursor": False},
    )
    validate_first_frame(
        payload,
        expected_width=1024,
        expected_height=768,
        image_format="png",
    )
    if headers.get("x-computer-use-width") != "1024":
        raise RuntimeError("screenshot width header did not match the validated frame")
    if headers.get("x-computer-use-height") != "768":
        raise RuntimeError("screenshot height header did not match the validated frame")
    return {"http_version": _require_http_11(client)}


def _action_operation(client: Any, *, actions: list[dict[str, Any]]) -> dict[str, Any]:
    result = client.post_json(
        "/v1/actions/run",
        json={"actions": actions, "source": "benchmark"},
    )
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("daemon action response was not successful")
    action_results = result.get("results")
    if not isinstance(action_results, list) or len(action_results) != len(actions):
        raise RuntimeError("daemon action response count did not match the request")
    backends: list[str] = []
    for item in action_results:
        if not isinstance(item, dict) or item.get("ok") is not True:
            raise RuntimeError("daemon action response contained a failed action")
        output = item.get("output")
        backend = output.get("input_backend") if isinstance(output, dict) else None
        if not isinstance(backend, str) or not backend:
            raise RuntimeError("daemon action response omitted its input backend")
        backends.append(backend)
    if set(backends) != {"xtest"}:
        raise RuntimeError("optimized ingress A/B requires the XTest backend")
    return {"http_version": _require_http_11(client), "input_backend": "xtest"}


def _require_http_11(client: Any) -> str:
    version = getattr(getattr(client, "transport", None), "last_http_version", None)
    if version != "HTTP/1.1":
        raise RuntimeError("optimized ingress A/B requires HTTP/1.1 on both arms")
    return version


def _select_optimized_ingress(cases: dict[str, Any]) -> dict[str, Any]:
    p50: dict[ModalOptimizedIngress, list[float]] = {arm: [] for arm in _ARMS}
    case_wins = {arm: 0 for arm in _ARMS}
    eligible = True
    for case_name in _RECURRING_CASES:
        case = cases.get(case_name)
        arms = case.get("arms") if isinstance(case, dict) else None
        if not isinstance(arms, dict):
            eligible = False
            continue
        values: dict[ModalOptimizedIngress, float] = {}
        for arm in _ARMS:
            arm_result = arms.get(arm)
            summary = arm_result.get("summary_ms") if isinstance(arm_result, dict) else None
            value = summary.get("p50") if isinstance(summary, dict) else None
            if (
                not isinstance(arm_result, dict)
                or arm_result.get("status") != "ok"
                or arm_result.get("failures") != []
                or not isinstance(value, int | float)
                or isinstance(value, bool)
                or value <= 0
            ):
                eligible = False
                continue
            values[arm] = float(value)
            p50[arm].append(float(value))
        if set(values) != set(_ARMS):
            continue
        faster = min(_ARMS, key=lambda arm: values[arm])
        case_wins[faster] += 1

    scores = {
        arm: _geometric_mean(p50[arm]) if len(p50[arm]) == len(_RECURRING_CASES) else None
        for arm in _ARMS
    }
    selected: ModalOptimizedIngress | None = None
    score_improvement_percent: float | None = None
    if eligible and all(isinstance(scores[arm], float) for arm in _ARMS):
        winner = min(_ARMS, key=lambda arm: float(scores[arm]))
        loser: ModalOptimizedIngress = (
            "attested-tunnel" if winner == "connect" else "connect"
        )
        winner_score = float(scores[winner])
        loser_score = float(scores[loser])
        score_improvement_percent = (1.0 - winner_score / loser_score) * 100.0
        losing_case_regressions = []
        for case_name in _RECURRING_CASES:
            arms = cases[case_name]["arms"]
            winner_p50 = float(arms[winner]["summary_ms"]["p50"])
            loser_p50 = float(arms[loser]["summary_ms"]["p50"])
            if winner_p50 > loser_p50:
                losing_case_regressions.append((winner_p50 / loser_p50 - 1.0) * 100.0)
        maximum_regression = max(losing_case_regressions, default=0.0)
        if (
            score_improvement_percent
            >= _WINNER_GATE["minimum_recurring_score_improvement_percent"]
            and case_wins[winner] >= _WINNER_GATE["minimum_recurring_case_wins"]
            and maximum_regression
            <= _WINNER_GATE["maximum_losing_case_regression_percent"]
        ):
            selected = winner
    return {
        "selected_ingress": selected,
        "requires_confirmation": selected is None,
        "recurring_cases": list(_RECURRING_CASES),
        "recurring_score": scores,
        "recurring_score_improvement_percent": score_improvement_percent,
        "case_wins": case_wins,
        "gate": dict(_WINNER_GATE),
        "zero_byte_floor_role": "descriptive only",
    }


def validate_modal_optimized_ingress_ab_artifact(
    payload: dict[str, Any], *, require_publishable: bool = True
) -> None:
    _validate_safe_value(payload)
    if (
        payload.get("schema_version") != 1
        or payload.get("benchmark") != "modal-optimized-ingress-ab"
    ):
        raise ValueError("Modal optimized ingress A/B artifact schema is unsupported")
    if payload.get("replacement_samples") not in (None, 0):
        raise ValueError("replacement samples are forbidden")
    if require_publishable and (
        payload.get("ok") is not True
        or payload.get("eligibility") != "publishable"
        or payload.get("iterations") != 30
        or payload.get("warmup_iterations") != 2
    ):
        raise ValueError("artifact is not publishable")
    if payload.get("ok") is True:
        run = payload.get("run")
        if not isinstance(run, dict) or run.get("ok") is not True:
            raise ValueError("successful artifact requires a successful runner")
        if run.get("placement_verified") is not True:
            raise ValueError("successful artifact requires observed placement verification")
        if run.get("connect_authorization_setup", {}).get("completed") is not True:
            raise ValueError("successful artifact requires Connect authorization")
        if run.get("failures") != [] or payload.get("failures") != []:
            raise ValueError("successful artifact cannot contain failures")
        cases = run.get("cases")
        if not isinstance(cases, dict) or set(cases) != set(_CASE_NAMES):
            raise ValueError("successful artifact requires the exact A/B cases")
        iterations = payload.get("iterations")
        for case_name in _CASE_NAMES:
            _validate_case(cases[case_name], iterations)
        selection = payload.get("selection")
        expected = _select_optimized_ingress(cases)
        if selection != expected:
            raise ValueError("selection does not match the predeclared gate")
    cleanup = payload.get("final_cleanup")
    if require_publishable and cleanup != {
        "cleanup_succeeded": True,
        "remaining_sandboxes": 0,
    }:
        raise ValueError("publishable artifact requires terminal cleanup")


def validate_modal_optimized_ingress_ab_output_path(path: Path) -> None:
    if path.is_absolute() or len(path.parts) < 2 or path.parts[0] != "benchmark-results":
        raise ValueError("output must be repository-relative under benchmark-results")
    if ".." in path.parts:
        raise ValueError("output must not traverse directories")
    benchmark_root = Path("benchmark-results")
    if benchmark_root.is_symlink():
        raise ValueError("output root cannot be a symlink")
    current = Path(path.parts[0])
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("output cannot traverse a symlink")
    resolved_root = benchmark_root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
        raise ValueError("output escapes benchmark-results")


def _validate_case(case: Any, iterations: Any) -> None:
    if (
        not isinstance(case, dict)
        or case.get("schedule") != _SCHEDULE
        or case.get("iterations_per_arm") != iterations
    ):
        raise ValueError("A/B case metadata is invalid")
    arms = case.get("arms")
    if not isinstance(arms, dict) or set(arms) != set(_ARMS):
        raise ValueError("A/B case arms are invalid")
    for arm in _ARMS:
        result = arms[arm]
        if (
            not isinstance(result, dict)
            or result.get("status") != "ok"
            or result.get("iterations") != iterations
            or result.get("successful_iterations") != iterations
            or result.get("failures") != []
        ):
            raise ValueError("A/B arm success contract is invalid")
        summary = result.get("summary_ms")
        if not isinstance(summary, dict):
            raise ValueError("A/B arm summary is missing")
        _finite_nonnegative(summary.get("p50"), "p50")
        _finite_nonnegative(summary.get("p95"), "p95")


def _computer_config(
    config: ModalOptimizedIngressABConfig,
    *,
    run_id: str,
) -> ComputerConfig:
    return ComputerConfig(
        runtime=RuntimeConfig(
            modal_region=config.region,
            timeout_seconds=config.sandbox_timeout_seconds,
            readiness_timeout_seconds=config.readiness_timeout_seconds,
        ),
        resources=ResourceConfig(
            profile="browser",
            cpu=config.cpu,
            memory_mib=config.memory_mib,
        ),
        image=ImageConfig(source="named", revision=config.image_revision),
        browser=BrowserConfig(kind=config.browser, prewarm=False),
        actions=ActionConfig(input_rate_limit_per_sec=0),
        run_id=run_id,
        ingress="tunnel",
    )


def _validate_initial_frame(computer: Any, config: ComputerConfig) -> None:
    payload = computer.screenshots.full_bytes(format="png", processing="daemon")
    validate_first_frame(
        payload,
        expected_width=config.desktop.resolution[0],
        expected_height=config.desktop.resolution[1],
        image_format="png",
    )


def _require_matching_placement(
    runner: dict[str, str | None],
    target: dict[str, str | None],
    *,
    requested_region: str,
) -> None:
    if not runner.get("cloud") or runner.get("region") != requested_region or target != runner:
        raise RuntimeError("runner and target placement differ")


def _safe_observed_placement(value: dict[str, str | None]) -> dict[str, str | None]:
    return {"cloud": value.get("cloud"), "region": value.get("region")}


def _case_semantic(case_name: str) -> str:
    return {
        "transport_floor_0b": "zero-byte HTTP response over a reused client",
        "screenshot_full_png": "validated 1024x768 PNG returned in memory",
        "move_and_click": "one ordered move and click request",
        "ordered_four_click_batch": "four clicks in one ordered action request",
    }[case_name]


def _case_comparison(arms: dict[str, Any]) -> dict[str, Any]:
    connect = arms["connect"].get("summary_ms", {}).get("p50")
    tunnel = arms["attested-tunnel"].get("summary_ms", {}).get("p50")
    if not isinstance(connect, int | float) or not isinstance(tunnel, int | float):
        return {
            "connect_p50_ms": None,
            "attested_tunnel_p50_ms": None,
            "attested_minus_connect_ms": None,
            "attested_vs_connect_percent": None,
            "faster_arm": None,
        }
    delta = float(tunnel) - float(connect)
    return {
        "connect_p50_ms": float(connect),
        "attested_tunnel_p50_ms": float(tunnel),
        "attested_minus_connect_ms": delta,
        "attested_vs_connect_percent": None if connect == 0 else (delta / float(connect)) * 100.0,
        "faster_arm": "connect" if connect < tunnel else "attested-tunnel",
    }


def _summary(samples: list[float]) -> dict[str, float | None]:
    if not samples:
        return {"p50": None, "p95": None}
    return {
        "p50": float(statistics.median(samples)),
        "p95": _percentile(samples, 95),
    }


def _percentile(samples: list[float], percentile: int) -> float:
    ordered = sorted(samples)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (percentile / 100.0) * (len(ordered) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return float(ordered[lower])
    weight = rank - lower
    return float(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)


def _geometric_mean(values: list[float]) -> float:
    return math.exp(sum(math.log(value) for value in values) / len(values))


def _case_failures(cases: dict[str, Any]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for case_name, case in cases.items():
        arms = case.get("arms") if isinstance(case, dict) else None
        if not isinstance(arms, dict):
            failures.append(_failure(case_name, -1, "InvalidCaseResult"))
            continue
        for arm in _ARMS:
            for item in _safe_failures(arms.get(arm, {}).get("failures"), case_name):
                failures.append({**item, "phase": f"{case_name}:{arm}:{item['phase']}"})
    return failures


def _cleanup_target(computer: Any | None) -> dict[str, Any]:
    if computer is None:
        return {"attempted": False, "succeeded": None, "error_type": None}
    error_type: str | None = None
    try:
        computer.terminate(wait=True)
    except Exception as exc:
        error_type = type(exc).__name__
    try:
        computer.detach()
    except Exception as exc:
        error_type = error_type or type(exc).__name__
    return {"attempted": True, "succeeded": error_type is None, "error_type": error_type}


def _failure(phase: str, iteration: int, exception_type: str) -> dict[str, Any]:
    return {"phase": phase, "iteration": iteration, "exception_type": exception_type}


def _safe_failures(value: Any, default_phase: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    safe: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            safe.append(_failure(default_phase, index, "BenchmarkFailure"))
            continue
        phase = item.get("phase")
        iteration = item.get("iteration")
        exception_type = item.get("exception_type")
        safe.append(
            _failure(
                phase if isinstance(phase, str) else default_phase,
                iteration if isinstance(iteration, int) else index,
                exception_type if isinstance(exception_type, str) else "BenchmarkFailure",
            )
        )
    return safe


def _validate_safe_value(value: Any, *, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", str(key)).lower().replace("-", "_")
            if normalized in _UNSAFE_KEYS or normalized.endswith(_UNSAFE_KEY_SUFFIXES):
                field_path = ".".join((*path, str(key)))
                raise ValueError(f"unsafe field in benchmark artifact: {field_path}")
            _validate_safe_value(item, path=(*path, str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_safe_value(item, path=(*path, str(index)))
    elif isinstance(value, str):
        lowered = value.lower()
        if "http://" in lowered or "https://" in lowered or "bearer " in lowered:
            raise ValueError(f"unsafe value in benchmark artifact: {'.'.join(path)}")


def _finite_nonnegative(value: Any, label: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"{label} must be finite and nonnegative")
