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
from .benchmarks.surfaces import (
    run_sdk_surface_benchmark,
    run_sdk_surface_benchmark_mock_local,
)
from .client import DaemonClient
from .config import BrowserConfig, ComputerConfig
from .errors import ModalNotInstalledError, SandboxUnavailableError
from .sandbox import ComputerSandbox, modal_sandbox_exec_runner_from_id
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
            "modal-daemon-connect ingress, then terminate it"
        ),
    )
    sdk_parser.add_argument("--app-name", default="modal-computer-use")
    sdk_parser.add_argument("--name")
    sdk_parser.add_argument("--modal-region")
    sdk_parser.add_argument("--resource-profile")
    sdk_parser.add_argument("--browser")
    sdk_parser.add_argument("--gpu")
    sdk_parser.add_argument("--modal-cpu", type=float)
    sdk_parser.add_argument("--modal-memory-mib", type=int)
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
        if args.create_modal_sandbox:
            if args.mock_local or args.base_url:
                sdk_parser.error(
                    "--create-modal-sandbox cannot be combined with --mock-local or --base-url"
                )
            if "daemon-http" not in surfaces:
                sdk_parser.error("--create-modal-sandbox requires surface daemon-http")
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
            base_url="https://connect.modal.run",
            sandbox_exec_runner=sandbox_exec_runner,
            sandbox_exec_setup_failure=sandbox_exec_setup_failure,
            environment_metadata=metadata,
        )
    finally:
        computer.terminate()
        computer.detach()


def _modal_benchmark_config(args: argparse.Namespace, *, run_id: str) -> ComputerConfig:
    config = ComputerConfig(run_id=run_id)
    config.runtime.modal_region = args.modal_region
    config.resources.profile = _modal_benchmark_resource_profile(args)
    config.resources.gpu = args.gpu
    config.resources.cpu = args.modal_cpu
    config.resources.memory_mib = args.modal_memory_mib
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
    allowed = set(DEFAULT_SDK_BENCHMARK_SURFACES) | {"sandbox-exec"}
    invalid = [surface for surface in values if surface not in allowed]
    if invalid:
        if parser is not None:
            parser.error(f"invalid benchmark surface: {', '.join(invalid)}")
        raise SystemExit(f"invalid benchmark surface: {', '.join(invalid)}")
    return values  # type: ignore[return-value]


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def _print_json(data: dict[str, Any]) -> None:
    print(_json_string(data))


def _json_string(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


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
        "resource_profile": args.resource_profile,
        "browser": args.browser,
        "gpu": args.gpu,
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
