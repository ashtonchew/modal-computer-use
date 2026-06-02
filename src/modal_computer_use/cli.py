from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .benchmarks import (
    DEFAULT_SDK_BENCHMARK_SURFACES,
    BenchmarkSurface,
    run_action_batch_benchmark,
    run_action_batch_benchmark_mock_local,
    run_benchmark_report,
    run_benchmark_report_mock_local,
)
from .benchmarks.billing import modal_billing_reconciliation_request
from .benchmarks.modal_colocated_client import (
    DEFAULT_MODAL_COLOCATED_RUNNER_PATHS,
    DEFAULT_MODAL_COLOCATED_SURFACES,
    MODAL_COLOCATED_ALLOWED_RUNNER_PATHS,
    MODAL_COLOCATED_ALLOWED_SURFACES,
    ModalColocatedClientBenchmarkConfig,
    run_modal_colocated_client_benchmark,
)
from .benchmarks.modal_region_ab import (
    DEFAULT_MODAL_REGION_AB_REGIONS,
    modal_region_ab_comparison,
    modal_region_ab_markdown_summary,
)
from .benchmarks.observation_surface import CAUSAL_ACTION_OBSERVE_DIAGNOSTIC_CASES
from .benchmarks.surfaces import (
    run_sdk_surface_benchmark,
    run_sdk_surface_benchmark_mock_local,
)
from .client import DaemonClient
from .config import BrowserConfig, ComputerConfig, ModalIngress
from .errors import ModalNotInstalledError, SandboxUnavailableError
from .sandbox import (
    ComputerSandbox,
    _connect_token_parts,
    modal_sandbox_exec_runner_from_id,
)
from .state import new_run_id
from .tracing import ComputerTrace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="computer-use")
    subparsers = parser.add_subparsers(dest="command", required=True)

    trace_parser = subparsers.add_parser("trace")
    trace_subparsers = trace_parser.add_subparsers(dest="trace_command", required=True)

    validate_parser = trace_subparsers.add_parser("validate")
    validate_parser.add_argument("path", type=Path)

    replay_parser = trace_subparsers.add_parser("replay")
    replay_parser.add_argument("path", type=Path)
    replay_parser.add_argument("--dry-run", action="store_true")
    replay_target = replay_parser.add_mutually_exclusive_group()
    replay_target.add_argument("--base-url")
    replay_target.add_argument("--sandbox-id")
    replay_target.add_argument("--target-run-id", "--target", dest="target_run_id")
    replay_parser.add_argument("--token")
    replay_parser.add_argument("--app-name", default="modal-computer-use")
    replay_parser.add_argument("--continue-on-error", action="store_true")

    benchmark_parser = subparsers.add_parser("benchmark")
    benchmark_subparsers = benchmark_parser.add_subparsers(dest="benchmark_command", required=True)
    action_batch_parser = benchmark_subparsers.add_parser("action-batch")
    action_batch_mode = action_batch_parser.add_mutually_exclusive_group(required=True)
    action_batch_mode.add_argument("--base-url")
    action_batch_mode.add_argument("--mock-local", action="store_true")
    action_batch_parser.add_argument("--token")
    action_batch_parser.add_argument("--iterations", type=_positive_int, default=5)
    action_batch_parser.add_argument("--json", action="store_true", default=True)

    report_parser = benchmark_subparsers.add_parser("report")
    report_mode = report_parser.add_mutually_exclusive_group(required=True)
    report_mode.add_argument("--base-url")
    report_mode.add_argument("--mock-local", action="store_true")
    report_parser.add_argument("--token")
    report_parser.add_argument(
        "--include-sandbox-exec",
        action="store_true",
        help="also compare the daemon move+click hot path with Modal Sandbox.exec",
    )
    report_parser.add_argument("--sandbox-id")
    report_parser.add_argument("--modal-region")
    report_parser.add_argument("--resource-profile")
    report_parser.add_argument("--browser")
    report_parser.add_argument("--gpu")
    report_parser.add_argument("--image-profile", dest="image_profile")
    report_parser.add_argument("--image-variant", dest="image_profile")
    report_parser.add_argument("--iterations", type=_positive_int, default=5)
    report_parser.add_argument("--output", type=Path)
    report_parser.add_argument("--json", action="store_true", default=True)

    sdk_parser = benchmark_subparsers.add_parser("sdk")
    sdk_mode = sdk_parser.add_mutually_exclusive_group()
    sdk_mode.add_argument("--base-url")
    sdk_mode.add_argument("--mock-local", action="store_true")
    sdk_parser.add_argument("--token")
    sdk_parser.add_argument(
        "--surface",
        action="append",
        choices=[
            "daemon-http",
            "daemon-hot-session",
            "daemon-transport-floor",
            "daemon-observation-stream",
            "sandbox-exec",
            "openai-adapter",
            "anthropic-adapter",
            "action-executor",
        ],
        help="SDK-owned benchmark surface to measure; may be passed more than once",
    )
    sdk_parser.add_argument(
        "--surfaces",
        help=(
            "comma-separated surface list; defaults to "
            "daemon-http,openai-adapter,anthropic-adapter,action-executor"
        ),
    )
    sdk_parser.add_argument("--sandbox-id")
    sdk_parser.add_argument(
        "--create-modal-sandbox",
        action="store_true",
        help=(
            "create a fresh Modal-backed CUA sandbox, run daemon-http through "
            "the selected Modal daemon ingress, then terminate it"
        ),
    )
    sdk_parser.add_argument("--app-name", default="modal-computer-use")
    sdk_parser.add_argument("--name")
    sdk_parser.add_argument("--modal-region")
    sdk_parser.add_argument(
        "--modal-ingress",
        choices=["attested-tunnel", "connect", "tunnel"],
        default="attested-tunnel",
        help="Modal daemon ingress for created sandboxes; defaults to attested-tunnel",
    )
    sdk_parser.add_argument(
        "--daemon-http-version",
        choices=["1.1", "2"],
        default="1.1",
        help="daemon transport HTTP version for created Modal sandboxes; defaults to 1.1",
    )
    sdk_parser.add_argument("--resource-profile")
    sdk_parser.add_argument("--browser")
    sdk_parser.add_argument("--gpu")
    sdk_parser.add_argument("--modal-cpu", type=float)
    sdk_parser.add_argument("--modal-memory-mib", type=int)
    sdk_parser.add_argument(
        "--input-rate-limit-per-sec",
        type=int,
        default=0,
        help=(
            "daemon input action rate limit for created benchmark sandboxes; "
            "defaults to 0 so primitive latency runs do not measure throttling"
        ),
    )
    sdk_parser.add_argument(
        "--input-backend",
        choices=["auto", "xtest", "xdotool"],
        default="auto",
        help="daemon pointer input backend for created benchmark sandboxes; defaults to auto",
    )
    sdk_parser.add_argument("--image-profile", dest="image_profile")
    sdk_parser.add_argument("--image-variant", dest="image_profile")
    sdk_parser.add_argument("--iterations", type=_positive_int, default=5)
    sdk_parser.add_argument("--output", type=Path)
    sdk_parser.add_argument(
        "--modal-billing-reconcile",
        action="store_true",
        help="query Modal billing report data for a tagged daemon-http benchmark run",
    )
    sdk_parser.add_argument(
        "--modal-billing-start",
        help="UTC/ISO start timestamp for Modal billing reconciliation",
    )
    sdk_parser.add_argument(
        "--modal-billing-end",
        help="UTC/ISO end timestamp for Modal billing reconciliation; defaults to now",
    )
    sdk_parser.add_argument(
        "--modal-billing-resolution",
        default="h",
        help="Modal billing report resolution; defaults to h",
    )
    sdk_parser.add_argument(
        "--modal-billing-buffer-seconds",
        type=int,
        default=0,
        help="subtract this many seconds from the reconciliation end time",
    )
    sdk_parser.add_argument(
        "--modal-billing-tag",
        action="append",
        default=[],
        help="required billing tag as key=value; repeat for each attribution tag",
    )
    sdk_parser.add_argument(
        "--modal-billing-tag-name",
        action="append",
        default=[],
        help="tag name to request from Modal billing reports; repeatable",
    )
    sdk_parser.add_argument("--json", action="store_true", default=True)

    ingress_ab_parser = benchmark_subparsers.add_parser("modal-ingress-ab")
    ingress_ab_parser.add_argument("--app-name", default="modal-computer-use")
    ingress_ab_parser.add_argument("--name")
    ingress_ab_parser.add_argument("--modal-region")
    ingress_ab_parser.add_argument(
        "--daemon-http-version",
        choices=["1.1", "2"],
        default="1.1",
        help="daemon transport HTTP version for the created Modal sandbox; defaults to 1.1",
    )
    ingress_ab_parser.add_argument("--resource-profile")
    ingress_ab_parser.add_argument("--browser")
    ingress_ab_parser.add_argument("--gpu")
    ingress_ab_parser.add_argument("--modal-cpu", type=float)
    ingress_ab_parser.add_argument("--modal-memory-mib", type=int)
    ingress_ab_parser.add_argument(
        "--input-rate-limit-per-sec",
        type=int,
        default=0,
        help=(
            "daemon input action rate limit for the created benchmark sandbox; "
            "defaults to 0 so primitive latency runs do not measure throttling"
        ),
    )
    ingress_ab_parser.add_argument(
        "--input-backend",
        choices=["auto", "xtest", "xdotool"],
        default="auto",
        help="daemon pointer input backend for the created benchmark sandbox; defaults to auto",
    )
    ingress_ab_parser.add_argument("--image-profile", dest="image_profile")
    ingress_ab_parser.add_argument("--image-variant", dest="image_profile")
    ingress_ab_parser.add_argument("--iterations", type=_positive_int, default=5)
    ingress_ab_parser.add_argument("--output", type=Path)
    ingress_ab_parser.add_argument("--json", action="store_true", default=True)

    region_ab_parser = benchmark_subparsers.add_parser("modal-region-ab")
    region_ab_parser.set_defaults(modal_region=None)
    region_ab_parser.add_argument("--app-name", default="modal-computer-use")
    region_ab_parser.add_argument("--name")
    region_ab_parser.add_argument(
        "--modal-region",
        dest="modal_regions",
        action="append",
        help=(
            "Modal region to test; repeatable. Use 'default' for Modal's default "
            "placement. Defaults to default, us-west, us-east."
        ),
    )
    region_ab_parser.add_argument(
        "--modal-ingress",
        choices=["attested-tunnel", "connect", "tunnel"],
        default="attested-tunnel",
        help="Modal daemon ingress to compare across regions; defaults to attested-tunnel",
    )
    region_ab_parser.add_argument(
        "--daemon-http-version",
        choices=["1.1", "2"],
        default="1.1",
        help="daemon transport HTTP version for created Modal sandboxes; defaults to 1.1",
    )
    region_ab_parser.add_argument(
        "--caller-region-label",
        help=(
            "free-form label for where the benchmark caller or model loop ran; "
            "recorded as metadata only"
        ),
    )
    region_ab_parser.add_argument("--resource-profile")
    region_ab_parser.add_argument("--browser")
    region_ab_parser.add_argument("--gpu")
    region_ab_parser.add_argument("--modal-cpu", type=float)
    region_ab_parser.add_argument("--modal-memory-mib", type=int)
    region_ab_parser.add_argument(
        "--input-rate-limit-per-sec",
        type=int,
        default=0,
        help=(
            "daemon input action rate limit for created benchmark sandboxes; "
            "defaults to 0 so primitive latency runs do not measure throttling"
        ),
    )
    region_ab_parser.add_argument(
        "--input-backend",
        choices=["auto", "xtest", "xdotool"],
        default="auto",
        help="daemon pointer input backend for created benchmark sandboxes; defaults to auto",
    )
    region_ab_parser.add_argument("--image-profile", dest="image_profile")
    region_ab_parser.add_argument("--image-variant", dest="image_profile")
    region_ab_parser.add_argument("--iterations", type=_positive_int, default=5)
    region_ab_parser.add_argument("--output", type=Path)
    region_ab_parser.add_argument("--json", action="store_true", default=True)

    region_summary_parser = benchmark_subparsers.add_parser("modal-region-summary")
    region_summary_parser.add_argument("path", type=Path)
    region_summary_parser.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="summary output format; defaults to markdown",
    )
    region_summary_parser.add_argument("--output", type=Path)

    colocated_parser = benchmark_subparsers.add_parser("modal-colocated-client")
    colocated_parser.add_argument("--app-name", default="modal-computer-use")
    colocated_parser.add_argument("--name")
    colocated_parser.add_argument("--modal-region", required=True)
    colocated_parser.add_argument(
        "--caller-region-label",
        help="free-form label for where the external benchmark caller ran",
    )
    colocated_parser.add_argument(
        "--modal-ingress",
        choices=["attested-tunnel", "connect", "tunnel"],
        default="attested-tunnel",
        help="Modal daemon ingress for the target sandbox; defaults to attested-tunnel",
    )
    colocated_parser.add_argument(
        "--daemon-http-version",
        choices=["1.1", "2"],
        default="1.1",
        help="daemon transport HTTP version for the target Modal sandbox; defaults to 1.1",
    )
    colocated_parser.add_argument("--resource-profile")
    colocated_parser.add_argument("--browser")
    colocated_parser.add_argument("--gpu")
    colocated_parser.add_argument("--modal-cpu", type=float)
    colocated_parser.add_argument("--modal-memory-mib", type=int)
    colocated_parser.add_argument("--runner-cpu", type=float)
    colocated_parser.add_argument("--runner-memory-mib", type=int)
    colocated_parser.add_argument(
        "--surface",
        action="append",
        choices=list(MODAL_COLOCATED_ALLOWED_SURFACES),
        help=(
            "co-located benchmark surface to measure; may be passed more than once; "
            "defaults to daemon-transport-floor"
        ),
    )
    colocated_parser.add_argument(
        "--surfaces",
        help=(
            "comma-separated co-located surface list; defaults to "
            + ",".join(DEFAULT_MODAL_COLOCATED_SURFACES)
        ),
    )
    colocated_parser.add_argument(
        "--runner-path",
        action="append",
        choices=list(MODAL_COLOCATED_ALLOWED_RUNNER_PATHS),
        help=(
            "target daemon path used by the Modal runner; may be passed more than once; "
            "defaults to inherited"
        ),
    )
    colocated_parser.add_argument(
        "--runner-paths",
        help=(
            "comma-separated Modal runner path list; defaults to "
            + ",".join(DEFAULT_MODAL_COLOCATED_RUNNER_PATHS)
        ),
    )
    colocated_parser.add_argument(
        "--observation-case",
        action="append",
        help=(
            "daemon-observation-stream case to measure; may be passed more than once; "
            "defaults to every observation case"
        ),
    )
    colocated_parser.add_argument(
        "--observation-cases",
        help="comma-separated daemon-observation-stream case list",
    )
    colocated_parser.add_argument(
        "--observation-profile",
        choices=["causal-action-observe-diagnostic"],
        help=(
            "named daemon-observation-stream case profile; "
            "causal-action-observe-diagnostic measures transport probes plus json/binary-envelope "
            "causal action-observe production cases"
        ),
    )
    colocated_parser.add_argument(
        "--input-rate-limit-per-sec",
        type=int,
        default=0,
        help=(
            "daemon input action rate limit for the target benchmark sandbox; "
            "defaults to 0 so primitive latency runs do not measure throttling"
        ),
    )
    colocated_parser.add_argument(
        "--input-backend",
        choices=["auto", "xtest", "xdotool"],
        default="auto",
        help="daemon pointer input backend for the target benchmark sandbox; defaults to auto",
    )
    colocated_parser.add_argument("--image-profile", dest="image_profile")
    colocated_parser.add_argument("--image-variant", dest="image_profile")
    colocated_parser.add_argument("--iterations", type=_positive_int, default=5)
    colocated_parser.add_argument("--output", type=Path)
    colocated_parser.add_argument("--json", action="store_true", default=True)

    args = parser.parse_args(argv)
    if args.command == "benchmark" and args.benchmark_command == "report":
        if args.mock_local and args.include_sandbox_exec:
            report_parser.error(
                "--include-sandbox-exec requires --base-url and an existing --sandbox-id"
            )
        if args.include_sandbox_exec and not args.sandbox_id:
            report_parser.error("--include-sandbox-exec requires --sandbox-id")
    if args.command == "benchmark" and args.benchmark_command == "sdk":
        surfaces = _sdk_surfaces(args, parser=sdk_parser)
        if "daemon-http" in surfaces and not (
            args.mock_local or args.base_url or args.create_modal_sandbox
        ):
            sdk_parser.error(
                "daemon-http surface benchmark requires --mock-local, --base-url, "
                "or --create-modal-sandbox"
            )
        if "daemon-hot-session" in surfaces and not (args.base_url or args.create_modal_sandbox):
            sdk_parser.error(
                "daemon-hot-session surface benchmark requires --base-url "
                "or --create-modal-sandbox"
            )
        if "daemon-observation-stream" in surfaces and not (
            args.base_url or args.create_modal_sandbox
        ):
            sdk_parser.error(
                "daemon-observation-stream surface benchmark requires --base-url "
                "or --create-modal-sandbox"
            )
        if "daemon-transport-floor" in surfaces and not (
            args.base_url or args.create_modal_sandbox
        ):
            sdk_parser.error(
                "daemon-transport-floor surface benchmark requires --base-url "
                "or --create-modal-sandbox"
            )
        if args.create_modal_sandbox:
            if args.mock_local or args.base_url:
                sdk_parser.error(
                    "--create-modal-sandbox cannot be combined with --mock-local or --base-url"
                )
            if not {
                "daemon-http",
                "daemon-hot-session",
                "daemon-transport-floor",
                "daemon-observation-stream",
            }.intersection(surfaces):
                sdk_parser.error(
                    "--create-modal-sandbox requires surface daemon-http, "
                    "daemon-hot-session, daemon-transport-floor, "
                    "or daemon-observation-stream"
                )
            _validate_modal_create_args(args, parser=sdk_parser)
        if "sandbox-exec" in surfaces and not args.sandbox_id:
            sdk_parser.error("sandbox-exec surface benchmark requires --sandbox-id")
        if args.modal_billing_reconcile:
            if "daemon-http" not in surfaces:
                sdk_parser.error("--modal-billing-reconcile requires surface daemon-http")
            if not args.modal_billing_start:
                sdk_parser.error("--modal-billing-reconcile requires --modal-billing-start")
            if not args.modal_billing_tag:
                sdk_parser.error("--modal-billing-reconcile requires --modal-billing-tag")
            _parse_cli_datetime(args.modal_billing_start, parser=sdk_parser)
            if args.modal_billing_end:
                _parse_cli_datetime(args.modal_billing_end, parser=sdk_parser)
            _parse_key_value_pairs(args.modal_billing_tag, parser=sdk_parser)
    if args.command == "trace" and args.trace_command == "validate":
        return _trace_validate(args.path)
    if args.command == "trace" and args.trace_command == "replay":
        if not args.dry_run and not (args.base_url or args.sandbox_id or args.target_run_id):
            replay_parser.error("real replay requires --base-url, --sandbox-id, or --target-run-id")
        return _trace_replay(args)
    if args.benchmark_command == "action-batch":
        return _benchmark_action_batch(args)
    if args.benchmark_command == "sdk":
        return _benchmark_sdk(args)
    if args.benchmark_command == "modal-ingress-ab":
        _validate_modal_create_args(args, parser=ingress_ab_parser)
        return _benchmark_modal_ingress_ab(args)
    if args.benchmark_command == "modal-region-ab":
        _validate_modal_create_args(args, parser=region_ab_parser)
        return _benchmark_modal_region_ab(args, parser=region_ab_parser)
    if args.benchmark_command == "modal-region-summary":
        return _benchmark_modal_region_summary(args)
    if args.benchmark_command == "modal-colocated-client":
        colocated_surfaces = _modal_colocated_surfaces(args, parser=colocated_parser)
        _validate_modal_create_args(args, parser=colocated_parser)
        if "daemon-observation-stream" in colocated_surfaces and _modal_benchmark_resource_profile(
            args
        ) not in {"browser", "browser-gpu"}:
            colocated_parser.error(
                "daemon-observation-stream requires a browser-capable target; "
                "pass --browser chromium or --browser firefox"
            )
        return _benchmark_modal_colocated_client(args)
    return _benchmark_report(args)


def _trace_validate(path: Path) -> int:
    result = ComputerTrace.load(path).validate()
    _print_json(result.to_dict())
    return 0 if result.ok else 1


def _trace_replay(args: argparse.Namespace) -> int:
    trace = ComputerTrace.load(args.path)
    if args.dry_run:
        plan = trace.replay(dry_run=True)
        _print_json(plan.to_dict())
        return 0 if plan.ok else 1

    target = _trace_replay_target(args)
    try:
        plan = trace.replay(
            dry_run=False,
            target=target,
            stop_on_error=not args.continue_on_error,
        )
    finally:
        target.detach()
    _print_json(plan.to_dict())
    return 0 if plan.ok else 1


def _trace_replay_target(args: argparse.Namespace) -> ComputerSandbox:
    if args.base_url:
        return ComputerSandbox.local(base_url=args.base_url, token=args.token)
    if args.sandbox_id:
        return ComputerSandbox.attach(
            sandbox_id=args.sandbox_id,
            app_name=args.app_name,
            token=args.token,
            wait=True,
        )
    return ComputerSandbox.attach(run_id=args.target_run_id, app_name=args.app_name, wait=True)


def _benchmark_action_batch(args: argparse.Namespace) -> int:
    if args.mock_local:
        result = run_action_batch_benchmark_mock_local(iterations=args.iterations)
    else:
        client = DaemonClient(args.base_url, token=args.token)
        try:
            result = run_action_batch_benchmark(
                client=client,
                mode="http",
                iterations=args.iterations,
                base_url=args.base_url,
            )
        finally:
            client.close()
    _print_json(result)
    return 0 if result["ok"] else 1


def _benchmark_report(args: argparse.Namespace) -> int:
    if args.mock_local:
        result = run_benchmark_report_mock_local(iterations=args.iterations)
    else:
        sandbox_exec_runner = None
        sandbox_exec_setup_failure = None
        if args.include_sandbox_exec:
            try:
                sandbox_exec_runner = modal_sandbox_exec_runner_from_id(args.sandbox_id)
            except Exception as exc:
                sandbox_exec_setup_failure = _sandbox_exec_setup_failure(exc)
        client = DaemonClient(args.base_url, token=args.token)
        try:
            result = run_benchmark_report(
                client=client,
                mode="http",
                iterations=args.iterations,
                base_url=args.base_url,
                include_sandbox_exec=args.include_sandbox_exec,
                sandbox_exec_runner=sandbox_exec_runner,
                sandbox_exec_setup_failure=sandbox_exec_setup_failure,
                environment_metadata=_benchmark_environment_metadata(args),
            )
        finally:
            client.close()
    output = _json_string(result)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{output}\n", encoding="utf-8")
    print(output)
    return 0 if result["ok"] else 1


def _benchmark_sdk(args: argparse.Namespace) -> int:
    surfaces = _sdk_surfaces(args)
    sandbox_exec_runner = None
    sandbox_exec_setup_failure = None
    if "sandbox-exec" in surfaces:
        try:
            sandbox_exec_runner = modal_sandbox_exec_runner_from_id(args.sandbox_id)
        except Exception as exc:
            sandbox_exec_setup_failure = _sandbox_exec_setup_failure(exc)

    if args.mock_local:
        result = run_sdk_surface_benchmark_mock_local(
            surfaces=surfaces,
            iterations=args.iterations,
            sandbox_exec_runner=sandbox_exec_runner,
            sandbox_exec_setup_failure=sandbox_exec_setup_failure,
            environment_metadata=_benchmark_environment_metadata(args),
        )
    elif args.base_url:
        client = DaemonClient(args.base_url, token=args.token)
        try:
            result = run_sdk_surface_benchmark(
                surfaces=surfaces,
                client=client,
                mode="http",
                iterations=args.iterations,
                base_url=args.base_url,
                sandbox_exec_runner=sandbox_exec_runner,
                sandbox_exec_setup_failure=sandbox_exec_setup_failure,
                environment_metadata=_benchmark_environment_metadata(args),
            )
        finally:
            client.close()
    elif args.create_modal_sandbox:
        result = _benchmark_sdk_created_modal_sandbox(
            args,
            surfaces=surfaces,
            sandbox_exec_runner=sandbox_exec_runner,
            sandbox_exec_setup_failure=sandbox_exec_setup_failure,
        )
    else:
        raise RuntimeError("unreachable SDK benchmark mode")
    output = _json_string(result)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{output}\n", encoding="utf-8")
    print(output)
    return 0 if result["ok"] else 1


def _benchmark_sdk_created_modal_sandbox(
    args: argparse.Namespace,
    *,
    surfaces: list[BenchmarkSurface],
    sandbox_exec_runner: Any,
    sandbox_exec_setup_failure: dict[str, Any] | None,
) -> dict[str, Any]:
    import time

    run_id = new_run_id()
    config = _modal_benchmark_config(args, run_id=run_id)
    app_tags = {"benchmark": "sdk-surfaces", "benchmark_run_id": run_id}
    tags = {"benchmark": "sdk-surfaces", "benchmark_run_id": run_id, "surface": "daemon-http"}
    started = time.perf_counter()
    computer = ComputerSandbox.create(
        config=config,
        app_name=args.app_name,
        name=args.name,
        app_tags=app_tags,
        tags=tags,
        wait=True,
    )
    cold_create_to_ready_ms = (time.perf_counter() - started) * 1000
    metadata = {
        **_benchmark_environment_metadata(args),
        "modal_cold_create_to_ready_ms": cold_create_to_ready_ms,
        "modal_run_id": run_id,
        "modal_app_name": args.app_name,
        "modal_sandbox_id": computer.metadata().sandbox_id if computer.metadata() else None,
        "modal_cpu_count": args.modal_cpu,
        "modal_memory_gib": (
            args.modal_memory_mib / 1024 if args.modal_memory_mib is not None else None
        ),
    }
    try:
        return run_sdk_surface_benchmark(
            surfaces=surfaces,
            client=computer.client,
            mode="http",
            iterations=args.iterations,
            base_url=computer.client.base_url,
            sandbox_exec_runner=sandbox_exec_runner,
            sandbox_exec_setup_failure=sandbox_exec_setup_failure,
            environment_metadata=metadata,
        )
    finally:
        computer.terminate()
        computer.detach()


def _benchmark_modal_ingress_ab(args: argparse.Namespace) -> int:
    import time

    run_id = new_run_id()
    config = _modal_benchmark_config(args, run_id=run_id, ingress="tunnel")
    app_tags = {"benchmark": "modal-ingress-ab", "benchmark_run_id": run_id}
    tags = {"benchmark": "modal-ingress-ab", "benchmark_run_id": run_id, "surface": "daemon-http"}
    started = time.perf_counter()
    computer = ComputerSandbox.create(
        config=config,
        app_name=args.app_name,
        name=args.name,
        app_tags=app_tags,
        tags=tags,
        wait=True,
    )
    cold_create_to_ready_ms = (time.perf_counter() - started) * 1000
    attested_client: DaemonClient | None = None
    try:
        base_metadata = {
            **_benchmark_environment_metadata(args),
            "modal_cold_create_to_ready_ms": cold_create_to_ready_ms,
            "modal_run_id": run_id,
            "modal_app_name": args.app_name,
            "modal_sandbox_id": computer.metadata().sandbox_id if computer.metadata() else None,
            "modal_cpu_count": args.modal_cpu,
            "modal_memory_gib": (
                args.modal_memory_mib / 1024 if args.modal_memory_mib is not None else None
            ),
        }
        raw_result = run_sdk_surface_benchmark(
            surfaces=["daemon-http"],
            client=computer.client,
            mode="http",
            iterations=args.iterations,
            base_url=computer.client.base_url,
            environment_metadata={
                **base_metadata,
                "modal_ingress": "tunnel",
                "modal_ingress_ab_role": "raw-static-token",
            },
        )
        attested_token = _mint_tunnel_token_for_sandbox(computer)
        attested_client = DaemonClient(base_url=computer.client.base_url, token=attested_token)
        attested_result = run_sdk_surface_benchmark(
            surfaces=["daemon-http"],
            client=attested_client,
            mode="http",
            iterations=args.iterations,
            base_url=computer.client.base_url,
            environment_metadata={
                **base_metadata,
                "modal_ingress": "attested-tunnel",
                "modal_ingress_ab_role": "attested-minted-token",
            },
        )
        result = {
            "ok": raw_result["ok"] and attested_result["ok"],
            "benchmark": "modal-ingress-ab",
            "generated_at": datetime.now(UTC).isoformat(),
            "iterations": args.iterations,
            "metadata": {
                "environment": {
                    key: value for key, value in base_metadata.items() if value is not None
                },
                "base_url": computer.client.base_url,
                "comparison": "same sandbox, same encrypted tunnel URL, different bearer tokens",
            },
            "runs": {
                "raw_static_token": raw_result,
                "attested_minted_token": attested_result,
            },
            "comparison": _modal_ingress_ab_comparison(raw_result, attested_result),
            "failures": [
                *raw_result.get("failures", []),
                *attested_result.get("failures", []),
            ],
        }
    finally:
        if attested_client is not None:
            attested_client.close()
        computer.terminate()
        computer.detach()
    output = _json_string(result)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{output}\n", encoding="utf-8")
    print(output)
    return 0 if result["ok"] else 1


def _benchmark_modal_region_ab(
    args: argparse.Namespace,
    *,
    parser: argparse.ArgumentParser,
) -> int:
    import time

    regions = _modal_region_ab_regions(args, parser=parser)
    run_id = new_run_id()
    runs: dict[str, Any] = {}
    failures: list[dict[str, Any]] = []
    for region_label in regions:
        region = None if region_label == "default" else region_label
        region_run_id = f"{run_id}-{_modal_region_slug(region_label)}"
        config = _modal_benchmark_config(args, run_id=region_run_id, modal_region=region)
        app_tags = {"benchmark": "modal-region-ab", "benchmark_run_id": run_id}
        tags = {
            "benchmark": "modal-region-ab",
            "benchmark_run_id": run_id,
            "surface": "daemon-transport-floor",
        }
        computer_name = _modal_region_ab_name(args.name, region_label, region_count=len(regions))
        started = time.perf_counter()
        computer = ComputerSandbox.create(
            config=config,
            app_name=args.app_name,
            name=computer_name,
            app_tags=app_tags,
            tags=tags,
            wait=True,
        )
        cold_create_to_ready_ms = (time.perf_counter() - started) * 1000
        try:
            metadata = {
                **_modal_region_ab_environment_metadata(args),
                "modal_region": region,
                "modal_region_label": region_label,
                "modal_cold_create_to_ready_ms": cold_create_to_ready_ms,
                "modal_run_id": region_run_id,
                "modal_ab_run_id": run_id,
                "modal_app_name": args.app_name,
                "modal_sandbox_id": (
                    computer.metadata().sandbox_id if computer.metadata() else None
                ),
                "modal_cpu_count": args.modal_cpu,
                "modal_memory_gib": (
                    args.modal_memory_mib / 1024 if args.modal_memory_mib is not None else None
                ),
            }
            result = run_sdk_surface_benchmark(
                surfaces=["daemon-transport-floor"],
                client=computer.client,
                mode="http",
                iterations=args.iterations,
                base_url=computer.client.base_url,
                environment_metadata=metadata,
            )
            runs[region_label] = result
            failures.extend(result.get("failures", []))
        finally:
            computer.terminate()
            computer.detach()

    result = {
        "ok": all(run.get("ok") for run in runs.values()),
        "benchmark": "modal-region-ab",
        "generated_at": datetime.now(UTC).isoformat(),
        "iterations": args.iterations,
        "metadata": {
            "regions": regions,
            "surface": "daemon-transport-floor",
            "caller_region_label": args.caller_region_label,
            "modal_ingress": args.modal_ingress,
            "daemon_http_version": args.daemon_http_version,
            "comparison": "fresh Modal sandbox per region, same daemon transport-floor surface",
        },
        "runs": runs,
        "comparison": modal_region_ab_comparison(runs),
        "failures": failures,
    }
    output = _json_string(result)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{output}\n", encoding="utf-8")
    print(output)
    return 0 if result["ok"] else 1


def _modal_region_ab_regions(
    args: argparse.Namespace,
    *,
    parser: argparse.ArgumentParser,
) -> list[str]:
    values = args.modal_regions or list(DEFAULT_MODAL_REGION_AB_REGIONS)
    regions: list[str] = []
    seen: set[str] = set()
    for value in values:
        region = value.strip()
        if not region:
            parser.error("--modal-region must not be empty")
        if region in seen:
            continue
        seen.add(region)
        regions.append(region)
    return regions


def _modal_region_ab_name(
    name: str | None,
    region_label: str,
    *,
    region_count: int,
) -> str | None:
    if not name:
        return None
    if region_count == 1:
        return name
    return f"{name}-{_modal_region_slug(region_label)}"


def _modal_region_slug(region_label: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in region_label).strip("-") or "region"


def _modal_region_ab_environment_metadata(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "caller_region_label": args.caller_region_label,
        "modal_ingress": args.modal_ingress,
        "daemon_http_version": args.daemon_http_version,
        "resource_profile": args.resource_profile,
        "browser": args.browser,
        "gpu": args.gpu,
        "input_rate_limit_per_sec": args.input_rate_limit_per_sec,
        "image_profile": args.image_profile,
    }


def _benchmark_modal_region_summary(args: argparse.Namespace) -> int:
    payload = json.loads(args.path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("modal region benchmark artifact must be a JSON object")
    if args.format == "json":
        output = _json_string(
            {
                "benchmark": payload.get("benchmark"),
                "generated_at": payload.get("generated_at"),
                "metadata": payload.get("metadata"),
                "comparison": modal_region_ab_comparison(_dict_value(payload.get("runs"))),
            }
        )
    else:
        output = modal_region_ab_markdown_summary(payload)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{output}\n", encoding="utf-8")
    print(output)
    return 0


def _benchmark_modal_colocated_client(args: argparse.Namespace) -> int:
    try:
        result = run_modal_colocated_client_benchmark(
            ModalColocatedClientBenchmarkConfig(
                app_name=args.app_name,
                name=args.name,
                target_config_factory=lambda run_id: _modal_benchmark_config(
                    args, run_id=run_id
                ),
                modal_region=args.modal_region,
                caller_region_label=args.caller_region_label,
                modal_ingress=args.modal_ingress,
                daemon_http_version=args.daemon_http_version,
                resource_profile=args.resource_profile,
                browser=args.browser,
                gpu=args.gpu,
                modal_cpu=args.modal_cpu,
                modal_memory_mib=args.modal_memory_mib,
                runner_cpu=args.runner_cpu,
                runner_memory_mib=args.runner_memory_mib,
                input_rate_limit_per_sec=args.input_rate_limit_per_sec,
                image_profile=args.image_profile,
                surfaces=_modal_colocated_surfaces(args),
                observation_cases=_modal_colocated_observation_cases(args),
                runner_paths=_modal_colocated_runner_paths(args),
                iterations=args.iterations,
            )
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    output = _json_string(result)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{output}\n", encoding="utf-8")
    print(output)
    return 0 if result["ok"] else 1


def _modal_colocated_surfaces(
    args: argparse.Namespace,
    *,
    parser: argparse.ArgumentParser | None = None,
) -> list[BenchmarkSurface]:
    values: list[str] = []
    if args.surfaces:
        values.extend(surface.strip() for surface in args.surfaces.split(","))
    if args.surface:
        values.extend(args.surface)
    if not values:
        return list(DEFAULT_MODAL_COLOCATED_SURFACES)
    invalid = [surface for surface in values if surface not in MODAL_COLOCATED_ALLOWED_SURFACES]
    if invalid:
        if parser is not None:
            parser.error(f"invalid co-located benchmark surface: {', '.join(invalid)}")
        raise SystemExit(f"invalid co-located benchmark surface: {', '.join(invalid)}")
    return values  # type: ignore[return-value]


def _modal_colocated_runner_paths(args: argparse.Namespace) -> list[str]:
    values: list[str] = []
    if getattr(args, "runner_paths", None):
        values.extend(path.strip() for path in args.runner_paths.split(",") if path.strip())
    values.extend(getattr(args, "runner_path", None) or [])
    if not values:
        return list(DEFAULT_MODAL_COLOCATED_RUNNER_PATHS)
    invalid = [path for path in values if path not in MODAL_COLOCATED_ALLOWED_RUNNER_PATHS]
    if invalid:
        raise SystemExit(f"unsupported co-located runner path: {', '.join(invalid)}")
    return values


def _mint_tunnel_token_for_sandbox(computer: ComputerSandbox) -> str:
    sandbox = computer._sandbox
    if sandbox is None:
        raise SandboxUnavailableError("modal ingress A/B benchmark requires a Modal sandbox")
    token_info = sandbox.create_connect_token(
        user_metadata={"sdk": "modal-computer-use", "benchmark": "modal-ingress-ab"}
    )
    connect_base_url, connect_token = _connect_token_parts(token_info)
    connect_client = DaemonClient(base_url=connect_base_url, token=connect_token)
    try:
        payload = connect_client.post_json("/v1/session/tunnel-authorize")
    finally:
        connect_client.close()
    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise SandboxUnavailableError("daemon did not return an attested tunnel token")
    return token


def _modal_ingress_ab_comparison(
    raw_result: dict[str, Any],
    attested_result: dict[str, Any],
) -> dict[str, Any]:
    cases = [
        ("batch_5_actions", ("action_batch", "cases", "batch_5_actions")),
        ("separate_5_actions", ("action_batch", "cases", "separate_5_actions")),
        ("move_click", ("move_click",)),
        ("move_click_sequence", ("move_click_sequence",)),
        ("screenshot_full", ("screenshot_full",)),
        ("command_echo", ("command_echo",)),
        ("type_100_chars", ("type_100_chars",)),
        ("type_1000_chars", ("type_1000_chars",)),
    ]
    rows: dict[str, Any] = {}
    for name, path in cases:
        raw_case = _daemon_case(raw_result, path)
        attested_case = _daemon_case(attested_result, path)
        raw_mean = _case_mean(raw_case)
        attested_mean = _case_mean(attested_case)
        delta_ms = None if raw_mean is None or attested_mean is None else attested_mean - raw_mean
        delta_percent = (
            None
            if delta_ms is None or raw_mean is None or raw_mean == 0
            else (delta_ms / raw_mean) * 100
        )
        rows[name] = {
            "raw_static_token_mean_ms": raw_mean,
            "attested_minted_token_mean_ms": attested_mean,
            "delta_ms": delta_ms,
            "delta_percent": delta_percent,
            "raw_daemon_mean_ms": _case_daemon_mean(raw_case),
            "attested_daemon_mean_ms": _case_daemon_mean(attested_case),
            "raw_overhead_mean_ms": _case_overhead_mean(raw_case),
            "attested_overhead_mean_ms": _case_overhead_mean(attested_case),
        }
    return rows


def _daemon_case(result: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    value: Any = result.get("surfaces", {}).get("daemon-http", {}).get("cases", {})
    for key in path:
        value = value.get(key, {}) if isinstance(value, dict) else {}
    return value if isinstance(value, dict) else {}


def _case_mean(case: dict[str, Any]) -> float | None:
    summary = case.get("summary_ms")
    value = summary.get("mean") if isinstance(summary, dict) else None
    return float(value) if isinstance(value, int | float) else None


def _case_daemon_mean(case: dict[str, Any]) -> float | None:
    summary = case.get("daemon_summary_ms")
    value = summary.get("mean") if isinstance(summary, dict) else None
    return float(value) if isinstance(value, int | float) else None


def _case_overhead_mean(case: dict[str, Any]) -> float | None:
    summary = case.get("overhead_summary_ms")
    value = summary.get("mean") if isinstance(summary, dict) else None
    return float(value) if isinstance(value, int | float) else None


def _modal_benchmark_config(
    args: argparse.Namespace,
    *,
    run_id: str,
    ingress: ModalIngress | None = None,
    modal_region: str | None = None,
) -> ComputerConfig:
    config = ComputerConfig(run_id=run_id)
    config.ingress = ingress or args.modal_ingress
    config.network.daemon_http_version = getattr(args, "daemon_http_version", "1.1")
    config.runtime.modal_region = modal_region if modal_region is not None else args.modal_region
    config.resources.profile = _modal_benchmark_resource_profile(args)
    config.resources.gpu = args.gpu
    config.resources.cpu = args.modal_cpu
    config.resources.memory_mib = args.modal_memory_mib
    config.actions.input_rate_limit_per_sec = args.input_rate_limit_per_sec
    config.actions.input_backend = args.input_backend
    if args.browser:
        config.browser = BrowserConfig(kind=args.browser)
    return config


def _modal_benchmark_resource_profile(args: argparse.Namespace) -> str:
    if args.resource_profile:
        return args.resource_profile
    if args.gpu:
        return "browser-gpu"
    if args.browser:
        return "browser"
    return "standard"


def _validate_modal_create_args(
    args: argparse.Namespace,
    *,
    parser: argparse.ArgumentParser,
) -> None:
    profile = _modal_benchmark_resource_profile(args)
    if profile not in {"standard", "browser", "browser-gpu", "custom"}:
        parser.error("--resource-profile must be one of standard, browser, browser-gpu, custom")
    if args.browser and args.browser not in {"firefox", "chromium"}:
        parser.error("--browser must be firefox or chromium when creating a Modal sandbox")
    if args.modal_cpu is not None and args.modal_cpu <= 0:
        parser.error("--modal-cpu must be greater than 0")
    if args.modal_memory_mib is not None and args.modal_memory_mib < 128:
        parser.error("--modal-memory-mib must be at least 128")
    if args.input_rate_limit_per_sec < 0:
        parser.error("--input-rate-limit-per-sec must be non-negative")


def _sdk_surfaces(
    args: argparse.Namespace,
    *,
    parser: argparse.ArgumentParser | None = None,
) -> list[BenchmarkSurface]:
    values: list[str] = []
    if args.surfaces:
        values.extend(surface.strip() for surface in args.surfaces.split(","))
    if args.surface:
        values.extend(args.surface)
    if not values:
        return list(DEFAULT_SDK_BENCHMARK_SURFACES)
    allowed = set(DEFAULT_SDK_BENCHMARK_SURFACES) | {
        "daemon-hot-session",
        "daemon-transport-floor",
        "daemon-observation-stream",
        "sandbox-exec",
    }
    invalid = [surface for surface in values if surface not in allowed]
    if invalid:
        if parser is not None:
            parser.error(f"invalid benchmark surface: {', '.join(invalid)}")
        raise SystemExit(f"invalid benchmark surface: {', '.join(invalid)}")
    return values  # type: ignore[return-value]


def _modal_colocated_observation_cases(args: argparse.Namespace) -> list[str] | None:
    values: list[str] = []
    if args.observation_profile == "causal-action-observe-diagnostic":
        values.extend(CAUSAL_ACTION_OBSERVE_DIAGNOSTIC_CASES)
    if args.observation_cases:
        values.extend(case.strip() for case in args.observation_cases.split(","))
    if args.observation_case:
        values.extend(args.observation_case)
    values = [value for value in values if value]
    return values or None


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def _print_json(data: dict[str, Any]) -> None:
    print(_json_string(data))


def _json_string(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def _dict_value(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _sandbox_exec_setup_failure(exc: Exception) -> dict[str, Any]:
    code = "sandbox_exec_attach_failed"
    message = "could not attach to the requested Modal Sandbox"
    if isinstance(exc, ModalNotInstalledError):
        code = "modal_not_installed"
        message = str(exc)
    elif isinstance(exc, SandboxUnavailableError):
        code = "sandbox_unavailable"
        message = str(exc)
    elif "NotFound" in type(exc).__name__:
        code = "sandbox_not_found"
        message = "could not find the requested Modal Sandbox"
    return {
        "case": "sandbox_exec_move_click",
        "phase": "setup",
        "iteration": 0,
        "type": type(exc).__name__,
        "message": message,
        "code": code,
    }


def _benchmark_environment_metadata(args: argparse.Namespace) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "modal_region": args.modal_region,
        "modal_ingress": getattr(args, "modal_ingress", None),
        "daemon_http_version": getattr(args, "daemon_http_version", None),
        "resource_profile": args.resource_profile,
        "browser": args.browser,
        "gpu": args.gpu,
        "input_rate_limit_per_sec": getattr(args, "input_rate_limit_per_sec", None),
        "image_profile": args.image_profile,
    }
    if getattr(args, "modal_billing_reconcile", False):
        metadata["modal_billing_reconciliation"] = modal_billing_reconciliation_request(
            start=_parse_cli_datetime(args.modal_billing_start),
            end=_parse_cli_datetime(args.modal_billing_end) if args.modal_billing_end else None,
            resolution=args.modal_billing_resolution,
            buffer_seconds=args.modal_billing_buffer_seconds,
            required_tags=_parse_key_value_pairs(args.modal_billing_tag),
            tag_names=args.modal_billing_tag_name or None,
        )
    return metadata


def _parse_cli_datetime(
    value: str,
    *,
    parser: argparse.ArgumentParser | None = None,
) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        if parser is not None:
            parser.error("Modal billing timestamps must be ISO datetimes")
        raise
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_key_value_pairs(
    values: list[str],
    *,
    parser: argparse.ArgumentParser | None = None,
) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        key, separator, raw_value = value.partition("=")
        if not separator:
            if parser is not None:
                parser.error("--modal-billing-tag must be key=value")
            raise SystemExit(f"invalid key=value pair: {value}")
        key = key.strip()
        raw_value = raw_value.strip()
        if not key or not raw_value:
            if parser is not None:
                parser.error("--modal-billing-tag must include non-empty key and value")
            raise SystemExit(f"invalid key=value pair: {value}")
        parsed[key] = raw_value
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
