from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .benchmark_comparison import (
    add_provider_cleanup_errors,
    finalize_provider_runtime,
    run_provider_comparison,
)
from .benchmarks import (
    DEFAULT_COMPARE_PROVIDERS,
    DEFAULT_SDK_BENCHMARK_SURFACES,
    BenchmarkSurface,
    ComparisonProvider,
    run_action_batch_benchmark,
    run_action_batch_benchmark_mock_local,
    run_benchmark_report,
    run_benchmark_report_mock_local,
)
from .benchmarks.billing import modal_billing_reconciliation_request
from .benchmarks.costs import estimate_surface_cost
from .benchmarks.lifecycle import CleanupError, measure_create_to_first_observation
from .benchmarks.mock_local import _with_mock_local_client
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
from .benchmarks.provenance import benchmark_provenance
from .benchmarks.provider_results import (
    ProviderResultsError,
    render_provider_results_json,
    render_provider_results_markdown,
    validate_provider_results,
)
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


def _add_subprocess_backend_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--subprocess-backend",
        choices=["asyncio", "threaded", "isolated-asyncio"],
        default="isolated-asyncio",
        help=(
            "daemon subprocess execution backend for created sandboxes; "
            "defaults to isolated-asyncio"
        ),
    )


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
    provider_results_parser = benchmark_subparsers.add_parser("provider-results")
    provider_results_parser.add_argument("combined", type=Path)
    provider_results_parser.add_argument(
        "--format", choices=["markdown", "json"], default="markdown"
    )
    provider_results_parser.add_argument("--output", type=Path)
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
        help=(
            "daemon mouse and keyboard input backend for created benchmark sandboxes; "
            "defaults to auto"
        ),
    )
    _add_subprocess_backend_argument(sdk_parser)
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
    sdk_parser.add_argument(
        "--modal-billing-environment",
        help="scope Modal billing reconciliation to this Environment; defaults to Workspace",
    )
    sdk_parser.add_argument("--json", action="store_true", default=True)

    compare_parser = benchmark_subparsers.add_parser("compare")
    compare_mode = compare_parser.add_mutually_exclusive_group()
    compare_mode.add_argument("--base-url")
    compare_mode.add_argument("--mock-local", action="store_true")
    compare_parser.add_argument("--token")
    compare_parser.add_argument(
        "--provider",
        action="append",
        choices=[
            "modal-daemon",
            "modal-exec",
            "openai",
            "anthropic",
            "generic",
            "daytona",
            "e2b",
            "tzafon",
        ],
        help="provider to benchmark; may be passed more than once",
    )
    compare_parser.add_argument(
        "--providers",
        help="comma-separated provider list; defaults to modal-daemon,openai,anthropic,generic",
    )
    compare_parser.add_argument("--sandbox-id")
    compare_parser.add_argument(
        "--create-modal-sandbox",
        action="store_true",
        help="create a fresh Modal-backed CUA sandbox, run the comparison, then terminate it",
    )
    compare_parser.add_argument("--app-name", default="modal-computer-use")
    compare_parser.add_argument("--name")
    compare_parser.add_argument("--modal-region")
    compare_parser.add_argument(
        "--modal-ingress",
        choices=["attested-tunnel", "connect", "tunnel"],
        default="attested-tunnel",
        help="Modal daemon ingress for created sandboxes; defaults to attested-tunnel",
    )
    compare_parser.add_argument(
        "--daemon-http-version",
        choices=["1.1", "2"],
        default="1.1",
        help="daemon transport HTTP version for created Modal sandboxes; defaults to 1.1",
    )
    compare_parser.add_argument("--resource-profile")
    compare_parser.add_argument("--browser")
    compare_parser.add_argument("--gpu")
    compare_parser.add_argument("--modal-cpu", type=float)
    compare_parser.add_argument("--modal-memory-mib", type=int)
    compare_parser.add_argument(
        "--input-rate-limit-per-sec",
        type=int,
        default=0,
        help=(
            "daemon input action rate limit for created benchmark sandboxes; "
            "defaults to 0 so primitive latency runs do not measure throttling"
        ),
    )
    compare_parser.add_argument(
        "--input-backend",
        choices=["auto", "xtest", "xdotool"],
        default="auto",
        help="daemon pointer input backend for created benchmark sandboxes; defaults to auto",
    )
    _add_subprocess_backend_argument(compare_parser)
    compare_parser.add_argument("--image-profile", dest="image_profile")
    compare_parser.add_argument("--image-variant", dest="image_profile")
    compare_parser.add_argument("--iterations", type=_positive_int, default=5)
    compare_parser.add_argument("--output", type=Path)
    compare_parser.add_argument(
        "--modal-billing-reconcile",
        action="store_true",
        help="query Modal billing report data for a tagged modal-daemon benchmark run",
    )
    compare_parser.add_argument(
        "--modal-billing-start",
        help="UTC/ISO start timestamp for Modal billing reconciliation",
    )
    compare_parser.add_argument(
        "--modal-billing-end",
        help="UTC/ISO end timestamp for Modal billing reconciliation; defaults to now",
    )
    compare_parser.add_argument(
        "--modal-billing-resolution",
        default="h",
        help="Modal billing report resolution; defaults to h",
    )
    compare_parser.add_argument(
        "--modal-billing-buffer-seconds",
        type=int,
        default=0,
        help="subtract this many seconds from the reconciliation end time",
    )
    compare_parser.add_argument(
        "--modal-billing-tag",
        action="append",
        default=[],
        help="required billing tag as key=value; repeat for each attribution tag",
    )
    compare_parser.add_argument(
        "--modal-billing-tag-name",
        action="append",
        default=[],
        help="tag name to request from Modal billing reports; repeatable",
    )
    compare_parser.add_argument(
        "--env-file",
        type=Path,
        help="load provider benchmark credentials from a dotenv file; existing env vars win",
    )
    compare_parser.add_argument("--json", action="store_true", default=True)

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
    _add_subprocess_backend_argument(ingress_ab_parser)
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
    _add_subprocess_backend_argument(region_ab_parser)
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
    _add_subprocess_backend_argument(colocated_parser)
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
    if args.command == "benchmark" and args.benchmark_command == "compare":
        providers = _compare_providers(args, parser=compare_parser)
        if "modal-daemon" in providers and not (
            args.mock_local or args.base_url or args.create_modal_sandbox
        ):
            compare_parser.error(
                "modal-daemon comparison requires --mock-local, --base-url, "
                "or --create-modal-sandbox"
            )
        if args.create_modal_sandbox:
            if args.mock_local or args.base_url:
                compare_parser.error(
                    "--create-modal-sandbox cannot be combined with --mock-local or --base-url"
                )
            if "modal-daemon" not in providers:
                compare_parser.error("--create-modal-sandbox requires provider modal-daemon")
            _validate_modal_create_args(args, parser=compare_parser)
        if "modal-exec" in providers and not args.sandbox_id:
            compare_parser.error("modal-exec comparison requires --sandbox-id")
        if args.modal_billing_reconcile:
            if "modal-daemon" not in providers:
                compare_parser.error("--modal-billing-reconcile requires provider modal-daemon")
            if not args.modal_billing_start:
                compare_parser.error("--modal-billing-reconcile requires --modal-billing-start")
            if not args.modal_billing_tag:
                compare_parser.error("--modal-billing-reconcile requires --modal-billing-tag")
            _parse_cli_datetime(args.modal_billing_start, parser=compare_parser)
            if args.modal_billing_end:
                _parse_cli_datetime(args.modal_billing_end, parser=compare_parser)
            _parse_key_value_pairs(args.modal_billing_tag, parser=compare_parser)
        if args.env_file is not None and not args.env_file.is_file():
            compare_parser.error("--env-file must point to an existing file")
    if args.command == "trace" and args.trace_command == "validate":
        return _trace_validate(args.path)
    if args.command == "trace" and args.trace_command == "replay":
        if not args.dry_run and not (args.base_url or args.sandbox_id or args.target_run_id):
            replay_parser.error("real replay requires --base-url, --sandbox-id, or --target-run-id")
        return _trace_replay(args)
    if args.benchmark_command == "action-batch":
        return _benchmark_action_batch(args)
    if args.benchmark_command == "provider-results":
        return _benchmark_provider_results(args)
    if args.benchmark_command == "sdk":
        return _benchmark_sdk(args)
    if args.benchmark_command == "compare":
        return _benchmark_compare(args)
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


def _benchmark_provider_results(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(args.combined.read_bytes())
        if not isinstance(payload, dict):
            raise ProviderResultsError("combined provider results artifact must be a JSON object")
        validate_provider_results(payload)
        output = (
            render_provider_results_markdown(payload)
            if args.format == "markdown"
            else render_provider_results_json(payload)
        )
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
    except (OSError, json.JSONDecodeError, ProviderResultsError) as exc:
        raise SystemExit(str(exc)) from exc
    return 0


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

    started = time.perf_counter()
    run_id = new_run_id()
    config = _modal_benchmark_config(args, run_id=run_id)
    app_tags = {"benchmark": "sdk-surfaces", "benchmark_run_id": run_id}
    tags = {"benchmark": "sdk-surfaces", "benchmark_run_id": run_id, "surface": "daemon-http"}
    computer, metadata = _create_modal_benchmark_computer(
        args,
        config=config,
        app_name=args.app_name,
        name=args.name,
        app_tags=app_tags,
        tags=tags,
    )
    try:
        result = run_sdk_surface_benchmark(
            surfaces=surfaces,
            client=computer.client,
            mode="http",
            iterations=args.iterations,
            base_url=computer.client.base_url,
            sandbox_exec_runner=sandbox_exec_runner,
            sandbox_exec_setup_failure=sandbox_exec_setup_failure,
            environment_metadata=metadata,
        )
    except Exception as exc:
        cleanup_errors = _cleanup_modal_benchmark_computer(computer)
        _add_cleanup_note(exc, cleanup_errors)
        raise
    cleanup_errors = _cleanup_modal_benchmark_computer(computer)
    resource_lifetime_ms = (time.perf_counter() - started) * 1000
    _record_modal_resource_lifetime(result, metadata, resource_lifetime_ms)
    _raise_modal_cleanup_errors(cleanup_errors)
    return result


def _benchmark_compare(args: argparse.Namespace) -> int:
    providers = _compare_providers(args)
    if not args.mock_local and _has_live_external_provider(providers):
        _load_benchmark_env_file(args.env_file)
    sandbox_exec_runner = None
    sandbox_exec_setup_failure = None
    if "modal-exec" in providers:
        try:
            sandbox_exec_runner = modal_sandbox_exec_runner_from_id(args.sandbox_id)
        except Exception as exc:
            sandbox_exec_setup_failure = _sandbox_exec_setup_failure(exc)

    if args.mock_local:
        result = _with_mock_local_client(
            lambda client: run_provider_comparison(
                providers=providers,
                client=client,
                mode="mock-local",
                iterations=args.iterations,
                base_url="http://testserver",
                sandbox_exec_runner=sandbox_exec_runner,
                sandbox_exec_setup_failure=sandbox_exec_setup_failure,
                environment_metadata=_benchmark_environment_metadata(args),
            )
        )
    elif args.base_url:
        client = DaemonClient(args.base_url, token=args.token)
        try:
            result = run_provider_comparison(
                providers=providers,
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
        result = _benchmark_compare_created_modal_sandbox(
            args,
            providers=providers,
            sandbox_exec_runner=sandbox_exec_runner,
            sandbox_exec_setup_failure=sandbox_exec_setup_failure,
        )
    else:
        result = run_provider_comparison(
            providers=providers,
            mode="provider-live",
            iterations=args.iterations,
            sandbox_exec_runner=sandbox_exec_runner,
            sandbox_exec_setup_failure=sandbox_exec_setup_failure,
            environment_metadata=_benchmark_environment_metadata(args),
        )
    output = _json_string(result)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{output}\n", encoding="utf-8")
    print(output)
    return 0 if result["ok"] else 1


def _benchmark_compare_created_modal_sandbox(
    args: argparse.Namespace,
    *,
    providers: list[ComparisonProvider],
    sandbox_exec_runner: Any,
    sandbox_exec_setup_failure: dict[str, Any] | None,
) -> dict[str, Any]:
    import time

    other_providers = [provider for provider in providers if provider != "modal-daemon"]
    precomputed_results: dict[str, dict[str, Any]] = {}
    if other_providers:
        precomputed = run_provider_comparison(
            providers=other_providers,
            mode="provider-live",
            iterations=args.iterations,
            sandbox_exec_runner=sandbox_exec_runner,
            sandbox_exec_setup_failure=sandbox_exec_setup_failure,
            environment_metadata=_benchmark_environment_metadata(args),
        )
        precomputed_results = _dict_value(precomputed.get("providers"))

    def create_lifecycle() -> tuple[ComputerSandbox, dict[str, Any]]:
        run_id = new_run_id()
        config = _modal_benchmark_config(args, run_id=run_id)
        app_tags = {"benchmark": "provider-compare", "benchmark_run_id": run_id}
        tags = {
            "benchmark": "provider-compare",
            "benchmark_run_id": run_id,
            "provider": "modal-daemon",
        }
        return _create_modal_benchmark_computer(
            args,
            config=config,
            app_name=args.app_name,
            name=args.name,
            app_tags=app_tags,
            tags=tags,
        )

    lifecycle = measure_create_to_first_observation(
        name="product_create_to_first_screenshot",
        iterations=args.iterations,
        warmup_iterations=1,
        create=create_lifecycle,
        observe=lambda created: created[1],
        cleanup=lambda created: _cleanup_modal_benchmark_computer(created[0]),
        retain_final_measured_resource=True,
    )
    retained = lifecycle.retained_resource
    if retained is None or lifecycle.retained_started_at is None:
        _raise_modal_lifecycle_failures(lifecycle.failures, lifecycle.cleanup_errors)
        raise RuntimeError("Modal provider comparison did not create a benchmark sandbox")
    computer, metadata = retained
    metadata["modal_product_create_to_first_screenshot_samples_ms"] = lifecycle.samples_ms
    metadata["modal_product_create_to_first_screenshot_expected_samples"] = args.iterations
    if lifecycle.samples_ms:
        metadata["modal_cold_create_to_ready_ms"] = lifecycle.samples_ms[-1]
    if lifecycle.cleanup_errors:
        metadata["cost_notes"] = [
            "one or more lifecycle cleanup attempts failed; resource cost may be incomplete"
        ]
    result: dict[str, Any] | None = None
    final_cleanup_errors: list[CleanupError] = []
    try:
        result = run_provider_comparison(
            providers=providers,
            client=computer.client,
            mode="http",
            iterations=args.iterations,
            base_url=computer.client.base_url,
            sandbox_exec_runner=sandbox_exec_runner,
            sandbox_exec_setup_failure=sandbox_exec_setup_failure,
            environment_metadata=metadata,
            precomputed_provider_results=precomputed_results,
        )
    except Exception as exc:
        final_cleanup_errors = _cleanup_modal_benchmark_computer(computer)
        retained_runtime_seconds = time.perf_counter() - lifecycle.retained_started_at
        _add_cleanup_note(exc, [*lifecycle.cleanup_errors, *final_cleanup_errors])
        raise
    final_cleanup_errors = _cleanup_modal_benchmark_computer(computer)
    retained_runtime_seconds = time.perf_counter() - lifecycle.retained_started_at
    runtime_seconds = lifecycle.completed_runtime_seconds + retained_runtime_seconds
    finalize_provider_runtime(
        result,
        provider="modal-daemon",
        runtime_seconds=runtime_seconds,
    )
    add_provider_cleanup_errors(
        result,
        provider="modal-daemon",
        errors=[*lifecycle.cleanup_errors, *final_cleanup_errors],
    )
    return result


def _create_modal_benchmark_computer(
    args: argparse.Namespace,
    *,
    config: ComputerConfig,
    app_name: str,
    name: str | None,
    app_tags: dict[str, str],
    tags: dict[str, str],
) -> tuple[ComputerSandbox, dict[str, Any]]:
    import time

    started = time.perf_counter()
    computer = ComputerSandbox.create(
        config=config,
        app_name=app_name,
        name=name,
        app_tags=app_tags,
        tags=tags,
        wait=False,
    )
    create_return_ms = (time.perf_counter() - started) * 1000
    final_computer = computer
    try:
        computer.wait_until_ready(timeout=config.runtime.readiness_timeout_seconds)
        connect_ready_ms = (time.perf_counter() - started) * 1000
        final_ingress_ready_ms = connect_ready_ms
        if config.ingress == "attested-tunnel":
            sandbox_metadata = computer.metadata()
            final_computer = ComputerSandbox.attach(
                sandbox_id=sandbox_metadata.sandbox_id if sandbox_metadata else None,
                app_name=app_name,
                ingress="attested-tunnel",
                http2=config.network.daemon_http_version == "2",
                wait=True,
                readiness_timeout=config.runtime.readiness_timeout_seconds,
            )
            _raise_modal_cleanup_errors(_detach_modal_benchmark_computer(computer))
            final_ingress_ready_ms = (time.perf_counter() - started) * 1000
        first_screenshot = _modal_first_raw_screenshot_metadata(final_computer.client)
        first_screenshot_ms = (time.perf_counter() - started) * 1000
        metadata = {
            **_benchmark_environment_metadata(args),
            "modal_cold_create_to_ready_ms": first_screenshot_ms,
            "modal_cold_create_to_ready_definition": (
                "create wait=False to first raw full-screen screenshot over configured ingress"
            ),
            "startup_model": "modal_sandbox_image_daemon_start",
            "uses_snapshot_or_template": False,
            "readiness_contract": (
                "ComputerSandbox.create(wait=False) -> daemon /readyz -> configured ingress "
                "ready -> first raw full-screen screenshot"
            ),
            "setup_included": True,
            "ingress_included": True,
            "first_observation_api": "/v1/screenshots/full/raw",
            "modal_create_return_ms": create_return_ms,
            "modal_connect_ready_ms": connect_ready_ms,
            "modal_final_ingress_ready_ms": final_ingress_ready_ms,
            "modal_first_raw_screenshot_ms": first_screenshot_ms,
            **first_screenshot,
            "modal_run_id": config.run_id,
            "modal_app_name": app_name,
            "modal_sandbox_id": (
                final_computer.metadata().sandbox_id if final_computer.metadata() else None
            ),
            "modal_cpu_count": args.modal_cpu,
            "modal_memory_gib": (
                args.modal_memory_mib / 1024 if args.modal_memory_mib is not None else None
            ),
        }
        return final_computer, metadata
    except Exception as exc:
        cleanup_errors = _cleanup_modal_benchmark_computer(final_computer)
        if final_computer is not computer:
            cleanup_errors.extend(_detach_modal_benchmark_computer(computer))
        if cleanup_errors:
            exc.add_note(
                "Modal benchmark setup failed and cleanup also failed: "
                + ", ".join(method for method, _exc in cleanup_errors)
            )
        raise


def _modal_first_raw_screenshot_metadata(client: DaemonClient) -> dict[str, Any]:
    payload, headers = client.post_bytes_with_headers(
        "/v1/screenshots/full/raw",
        json={"format": "png", "show_cursor": False},
    )
    if not payload:
        raise RuntimeError("Modal raw screenshot was empty")
    return {
        "modal_first_raw_screenshot_size_bytes": len(payload),
        "modal_first_raw_screenshot_width": _int_header(headers, "x-computer-use-width"),
        "modal_first_raw_screenshot_height": _int_header(headers, "x-computer-use-height"),
        "modal_first_raw_screenshot_capture_backend": _str_header(
            headers, "x-computer-use-capture-backend"
        ),
    }


def _int_header(headers: Any, name: str) -> int | None:
    value = headers.get(name) if hasattr(headers, "get") else None
    if not isinstance(value, str) or not value.isdigit():
        return None
    return int(value)


def _str_header(headers: Any, name: str) -> str | None:
    value = headers.get(name) if hasattr(headers, "get") else None
    return value if isinstance(value, str) and value else None


def _cleanup_modal_benchmark_computer(computer: ComputerSandbox) -> list[CleanupError]:
    errors: list[CleanupError] = []
    try:
        computer.terminate(wait=True)
    except Exception as exc:
        errors.append(("terminate", exc))
    errors.extend(_detach_modal_benchmark_computer(computer))
    return errors


def _detach_modal_benchmark_computer(computer: ComputerSandbox) -> list[CleanupError]:
    errors: list[CleanupError] = []
    try:
        computer.detach()
    except Exception as exc:
        errors.append(("detach", exc))
    finally:
        close = getattr(computer.client, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                errors.append(("client.close", exc))
    return errors


def _raise_modal_cleanup_errors(errors: list[CleanupError]) -> None:
    if errors:
        raise RuntimeError(
            "Modal benchmark cleanup failed: " + ", ".join(method for method, _exc in errors)
        )


def _add_cleanup_note(exc: Exception, errors: list[CleanupError]) -> None:
    if errors:
        exc.add_note(
            "Modal benchmark cleanup also failed: "
            + ", ".join(method for method, _cleanup_exc in errors)
        )


def _raise_modal_lifecycle_failures(
    failures: list[dict[str, Any]], cleanup_errors: list[CleanupError]
) -> None:
    if failures or cleanup_errors:
        details = [str(failure.get("phase", "measure")) for failure in failures]
        details.extend(method for method, _exc in cleanup_errors)
        raise RuntimeError("Modal lifecycle sampling failed: " + ", ".join(details))


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
        "subprocess_backend": args.subprocess_backend,
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
                target_config_factory=lambda run_id: _modal_benchmark_config(args, run_id=run_id),
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
                subprocess_backend=args.subprocess_backend,
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
    config.actions.subprocess_backend = args.subprocess_backend
    if args.browser:
        config.browser = BrowserConfig(kind=args.browser, prewarm=False)
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


def _compare_providers(
    args: argparse.Namespace,
    *,
    parser: argparse.ArgumentParser | None = None,
) -> list[ComparisonProvider]:
    values: list[str] = []
    if args.providers:
        values.extend(provider.strip() for provider in args.providers.split(","))
    if args.provider:
        values.extend(args.provider)
    if not values:
        return list(DEFAULT_COMPARE_PROVIDERS)
    allowed = set(DEFAULT_COMPARE_PROVIDERS) | {"modal-exec", "daytona", "e2b", "tzafon"}
    invalid = [provider for provider in values if provider not in allowed]
    if invalid:
        if parser is not None:
            parser.error(f"invalid provider: {', '.join(invalid)}")
        raise SystemExit(f"invalid provider: {', '.join(invalid)}")
    return values  # type: ignore[return-value]


def _has_live_external_provider(providers: list[ComparisonProvider]) -> bool:
    return any(provider in {"daytona", "e2b", "tzafon"} for provider in providers)


def _load_benchmark_env_file(env_file: Path | None) -> None:
    from dotenv import dotenv_values

    path = env_file or Path.cwd() / ".env"
    if not path.is_file():
        return
    for key, value in dotenv_values(path).items():
        if key in _BENCHMARK_ENV_KEYS and value and key not in os.environ:
            os.environ[key] = value


_BENCHMARK_ENV_KEYS = frozenset(
    {
        "DAYTONA_API_KEY",
        "DAYTONA_API_URL",
        "DAYTONA_TARGET",
        "DAYTONA_SNAPSHOT",
        "E2B_API_KEY",
        "E2B_TEMPLATE",
        "LIGHTCONE_BASE_URL",
        "TZAFON_API_KEY",
    }
)


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
    resource_profile = getattr(args, "resource_profile", None)
    browser = getattr(args, "browser", None)
    benchmark_command = getattr(args, "benchmark_command", None)
    creates_modal_resource = bool(getattr(args, "create_modal_sandbox", False)) or (
        benchmark_command in {"modal-ingress-ab", "modal-region-ab", "modal-colocated-client"}
    )
    if creates_modal_resource:
        image_identity = f"inline:{browser or resource_profile or 'standard'}"
    else:
        image_identity = f"attached:{getattr(args, 'image_profile', None) or 'unavailable'}"
    metadata: dict[str, Any] = {
        "modal_region": getattr(args, "modal_region", None),
        "modal_ingress": getattr(args, "modal_ingress", None),
        "daemon_http_version": getattr(args, "daemon_http_version", None),
        "resource_profile": resource_profile,
        "browser": browser,
        "gpu": getattr(args, "gpu", None),
        "input_rate_limit_per_sec": getattr(args, "input_rate_limit_per_sec", None),
        "subprocess_backend": getattr(args, "subprocess_backend", None),
        "image_profile": getattr(args, "image_profile", None),
        "provenance": benchmark_provenance(
            caller_path="external-caller",
            modal_region=getattr(args, "modal_region", None),
            image_identity=image_identity,
            cpu=getattr(args, "modal_cpu", None),
            memory_mib=getattr(args, "modal_memory_mib", None),
            gpu=getattr(args, "gpu", None),
        ),
    }
    if getattr(args, "modal_billing_reconcile", False):
        metadata["modal_billing_reconciliation"] = modal_billing_reconciliation_request(
            start=_parse_cli_datetime(args.modal_billing_start),
            end=_parse_cli_datetime(args.modal_billing_end) if args.modal_billing_end else None,
            resolution=args.modal_billing_resolution,
            buffer_seconds=args.modal_billing_buffer_seconds,
            required_tags=_parse_key_value_pairs(args.modal_billing_tag),
            tag_names=args.modal_billing_tag_name or None,
            environment_name=args.modal_billing_environment,
        )
    return metadata


def _record_modal_resource_lifetime(
    result: dict[str, Any],
    environment_metadata: dict[str, Any],
    resource_lifetime_ms: float,
) -> None:
    environment_metadata["modal_resource_lifetime_ms"] = resource_lifetime_ms
    environment_metadata["cost_duration_policy"] = (
        "measured_resource_lifetime_including_creation_benchmark_and_teardown"
    )
    top_environment = result.setdefault("metadata", {}).setdefault("environment", {})
    if isinstance(top_environment, dict):
        top_environment.update(environment_metadata)
    estimate = estimate_surface_cost(
        "daemon-http",
        surface_status="ok" if result.get("ok") else "failed",
        runtime_seconds=resource_lifetime_ms / 1000,
        metadata={"environment": environment_metadata},
    )
    result["shared_resource_cost_estimate"] = estimate
    result["cost_status"] = {"shared_resource_estimate": estimate["status"]}


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
