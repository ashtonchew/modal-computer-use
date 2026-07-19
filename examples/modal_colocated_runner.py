"""Run user-owned code beside or inside a Modal desktop sandbox.

This example keeps latency-sensitive work off a broker hot path. The external
process creates the target desktop sandbox, then runs a user command in a
short-lived same-region runner sandbox. If Connect preparation fails before
dispatch, the command runs from the external caller. Post-dispatch errors
propagate so the command cannot run twice.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence

from modal_computer_use import (
    ComputerConfig,
    ComputerSandbox,
    ModalDaemonCommandResult,
    ModalSandboxExecResult,
    run_modal_daemon_command_with_fallback,
)


def run_colocated_command(
    command: Sequence[str],
    *,
    computer: ComputerSandbox,
    modal_region: str,
    app_name: str = "modal-computer-use",
    runner_name: str | None = None,
    env: dict[str, str] | None = None,
    runner_cpu: float | None = None,
    runner_memory_mib: int | None = None,
    exec_timeout_seconds: int = 240,
    external_runner: Callable[..., ModalSandboxExecResult] | None = None,
) -> ModalDaemonCommandResult:
    return run_modal_daemon_command_with_fallback(
        computer,
        tuple(command),
        app_name=app_name,
        modal_region=modal_region,
        runner_name=runner_name,
        env=env,
        runner_cpu=runner_cpu,
        runner_memory_mib=runner_memory_mib,
        exec_timeout_seconds=exec_timeout_seconds,
        external_runner=external_runner,
    )


def main() -> None:
    modal_region = os.environ.get("MODAL_COMPUTER_USE_REGION", "").strip()
    if not modal_region:
        raise RuntimeError(
            "set MODAL_COMPUTER_USE_REGION from a current production-shaped region measurement"
        )
    computer = ComputerSandbox.create(
        config=ComputerConfig(
            ingress="attested-tunnel",
            runtime={"modal_region": modal_region},
        )
    )
    try:
        computer.wait_until_ready()
        result = run_colocated_command(
            (
                "python",
                "-c",
                "import os; from modal_computer_use import DaemonClient; "
                "client = DaemonClient(os.environ['COMPUTER_USE_DAEMON_BASE_URL'], "
                "token=os.environ.get('COMPUTER_USE_DAEMON_TOKEN')); "
                "print(client.version())",
            ),
            computer=computer,
            modal_region=modal_region,
            runner_name="computer-use-colocated-runner",
        )
        print(
            {
                "runner_sandbox_id": result.result.sandbox_id,
                "returncode": result.result.returncode,
                "selected_path": result.selected_path,
                "fallback_used": result.fallback_used,
            }
        )
    finally:
        computer.terminate()
        computer.detach()


if __name__ == "__main__":
    main()
