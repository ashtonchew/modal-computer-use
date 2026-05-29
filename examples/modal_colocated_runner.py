"""Run user-owned code beside a Modal desktop sandbox.

This example keeps the latency-sensitive loop inside Modal without adding a
hosted broker. The external process creates the target desktop sandbox, then
starts a short-lived runner Sandbox in the same Modal region. The runner talks
directly to the target daemon.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from modal_computer_use import ComputerConfig, ComputerSandbox
from modal_computer_use.sandbox import ModalSandboxExecResult, modal_sandbox_exec_once


@dataclass(frozen=True)
class ColocatedRunnerTarget:
    base_url: str
    token: str | None
    sandbox_id: str | None


def target_from_computer(computer: ComputerSandbox) -> ColocatedRunnerTarget:
    metadata = computer.metadata()
    return ColocatedRunnerTarget(
        base_url=computer.client.base_url,
        token=getattr(computer.client.transport, "token", None),
        sandbox_id=None if metadata is None else metadata.sandbox_id,
    )


def colocated_runner_env(target: ColocatedRunnerTarget) -> dict[str, str]:
    env = {
        "COMPUTER_USE_DAEMON_BASE_URL": target.base_url,
    }
    if target.token:
        env["COMPUTER_USE_DAEMON_TOKEN"] = target.token
    if target.sandbox_id:
        env["COMPUTER_USE_TARGET_SANDBOX_ID"] = target.sandbox_id
    return env


def run_colocated_command(
    command: Sequence[str],
    *,
    target: ColocatedRunnerTarget,
    app_name: str = "modal-computer-use",
    runner_name: str | None = None,
    modal_region: str,
    env: dict[str, str] | None = None,
    runner_cpu: float | None = None,
    runner_memory_mib: int | None = None,
    exec_timeout_seconds: int = 240,
) -> ModalSandboxExecResult:
    runner_env = colocated_runner_env(target)
    if env:
        runner_env.update(env)
    return modal_sandbox_exec_once(
        tuple(command),
        app_name=app_name,
        name=runner_name,
        region=modal_region,
        env=runner_env,
        tags={"computer-use.runner": "colocated"},
        cpu=runner_cpu,
        memory_mib=runner_memory_mib,
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
            target=target_from_computer(computer),
            modal_region=modal_region,
            runner_name="computer-use-colocated-runner",
        )
        print({"runner_sandbox_id": result.sandbox_id, "returncode": result.returncode})
    finally:
        computer.terminate()
        computer.detach()


if __name__ == "__main__":
    main()
