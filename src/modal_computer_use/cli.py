from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .benchmarks import (
    DEFAULT_COMPARE_PROVIDERS,
    ComparisonProvider,
    run_action_batch_benchmark,
    run_action_batch_benchmark_mock_local,
    run_benchmark_report,
    run_benchmark_report_mock_local,
    run_provider_comparison,
    run_provider_comparison_mock_local,
)
from .client import DaemonClient
from .errors import ModalNotInstalledError, SandboxUnavailableError
from .sandbox import ComputerSandbox, modal_sandbox_exec_runner_from_id
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
    benchmark_subparsers = benchmark_parser.add_subparsers(
        dest="benchmark_command", required=True
    )
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
        ],
        help="provider to benchmark; may be passed more than once",
    )
    compare_parser.add_argument(
        "--providers",
        help="comma-separated provider list; defaults to modal-daemon,openai,anthropic,generic",
    )
    compare_parser.add_argument("--sandbox-id")
    compare_parser.add_argument("--modal-region")
    compare_parser.add_argument("--resource-profile")
    compare_parser.add_argument("--browser")
    compare_parser.add_argument("--gpu")
    compare_parser.add_argument("--image-profile", dest="image_profile")
    compare_parser.add_argument("--image-variant", dest="image_profile")
    compare_parser.add_argument("--iterations", type=_positive_int, default=5)
    compare_parser.add_argument("--output", type=Path)
    compare_parser.add_argument(
        "--env-file",
        type=Path,
        help="load provider benchmark credentials from a dotenv file; existing env vars win",
    )
    compare_parser.add_argument("--json", action="store_true", default=True)

    args = parser.parse_args(argv)
    if args.command == "benchmark" and args.benchmark_command == "report":
        if args.mock_local and args.include_sandbox_exec:
            report_parser.error(
                "--include-sandbox-exec requires --base-url and an existing --sandbox-id"
            )
        if args.include_sandbox_exec and not args.sandbox_id:
            report_parser.error("--include-sandbox-exec requires --sandbox-id")
    if args.command == "benchmark" and args.benchmark_command == "compare":
        providers = _compare_providers(args)
        if "modal-daemon" in providers and not (args.mock_local or args.base_url):
            compare_parser.error("modal-daemon comparison requires --mock-local or --base-url")
        if "modal-exec" in providers and not args.sandbox_id:
            compare_parser.error("modal-exec comparison requires --sandbox-id")
        if args.env_file is not None and not args.env_file.is_file():
            compare_parser.error("--env-file must point to an existing file")
    if args.command == "trace" and args.trace_command == "validate":
        return _trace_validate(args.path)
    if args.command == "trace" and args.trace_command == "replay":
        if not args.dry_run and not (args.base_url or args.sandbox_id or args.target_run_id):
            replay_parser.error(
                "real replay requires --base-url, --sandbox-id, or --target-run-id"
            )
        return _trace_replay(args)
    if args.benchmark_command == "action-batch":
        return _benchmark_action_batch(args)
    if args.benchmark_command == "compare":
        return _benchmark_compare(args)
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
        result = run_provider_comparison_mock_local(
            providers=providers,
            iterations=args.iterations,
            sandbox_exec_runner=sandbox_exec_runner,
            sandbox_exec_setup_failure=sandbox_exec_setup_failure,
            environment_metadata=_benchmark_environment_metadata(args),
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


def _compare_providers(args: argparse.Namespace) -> list[ComparisonProvider]:
    values: list[str] = []
    if args.providers:
        values.extend(provider.strip() for provider in args.providers.split(","))
    if args.provider:
        values.extend(args.provider)
    if not values:
        return list(DEFAULT_COMPARE_PROVIDERS)
    allowed = set(DEFAULT_COMPARE_PROVIDERS) | {"modal-exec", "daytona", "e2b"}
    invalid = [provider for provider in values if provider not in allowed]
    if invalid:
        raise SystemExit(f"invalid provider: {', '.join(invalid)}")
    return values  # type: ignore[return-value]


def _has_live_external_provider(providers: list[ComparisonProvider]) -> bool:
    return any(provider in {"daytona", "e2b"} for provider in providers)


def _load_benchmark_env_file(env_file: Path | None) -> None:
    from dotenv import load_dotenv

    path = env_file or Path.cwd() / ".env"
    if not path.is_file():
        return
    load_dotenv(path, override=False)


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


def _benchmark_environment_metadata(args: argparse.Namespace) -> dict[str, str | None]:
    return {
        "modal_region": args.modal_region,
        "resource_profile": args.resource_profile,
        "browser": args.browser,
        "gpu": args.gpu,
        "image_profile": args.image_profile,
    }


if __name__ == "__main__":
    raise SystemExit(main())
