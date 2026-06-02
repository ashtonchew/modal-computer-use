"""Run user-owned code beside or inside a Modal desktop sandbox.

This example keeps latency-sensitive work off a broker hot path. The external
process creates the target desktop sandbox, then runs a user command either in a
short-lived same-region runner sandbox or inside the target sandbox over
``http://127.0.0.1:8080``.
"""

from __future__ import annotations

from collections.abc import Sequence

from modal_computer_use import (
    ComputerConfig,
    ComputerSandbox,
    ModalDaemonEndpointPath,
    ModalSandboxExecResult,
    run_modal_daemon_command,
)


def run_colocated_command(
    command: Sequence[str],
    *,
    computer: ComputerSandbox,
    modal_region: str,
    path: ModalDaemonEndpointPath = "inherited",
    app_name: str = "modal-computer-use",
    runner_name: str | None = None,
    env: dict[str, str] | None = None,
    runner_cpu: float | None = None,
    runner_memory_mib: int | None = None,
    exec_timeout_seconds: int = 240,
) -> ModalSandboxExecResult:
    return run_modal_daemon_command(
        computer,
        tuple(command),
        path=path,
        app_name=app_name,
        modal_region=modal_region,
        runner_name=runner_name,
        env=env,
        runner_cpu=runner_cpu,
        runner_memory_mib=runner_memory_mib,
        exec_timeout_seconds=exec_timeout_seconds,
    )


def main() -> None:
    modal_region = "us-west"
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
            path="target-loopback",
            runner_name="computer-use-colocated-runner",
        )
        print({"runner_sandbox_id": result.sandbox_id, "returncode": result.returncode})
    finally:
        computer.terminate()
        computer.detach()


if __name__ == "__main__":
    main()
