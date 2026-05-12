"""Example-level warm-pool strategy.

Production apps can keep Modal sandbox IDs in a queue and attach with
ComputerSandbox.attach(sandbox_id=...). The core package intentionally does not
run a queue service or hide sandbox lifecycle costs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from modal_computer_use import BrowserConfig, ComputerConfig, ComputerSandbox, ResourceConfig


@dataclass(frozen=True)
class WarmSandboxRef:
    sandbox_id: str
    expires_at: datetime


def create_warm_sandbox(*, run_id: str) -> WarmSandboxRef:
    config = ComputerConfig(
        run_id=run_id,
        resources=ResourceConfig(profile="browser", cpu=4, memory_mib=8192),
        browser=BrowserConfig(kind="firefox", prewarm=True),
    )
    computer = ComputerSandbox.create(config=config, wait=True)
    try:
        metadata = computer.metadata()
        if metadata is None:
            raise RuntimeError("sandbox metadata unavailable")
        computer.wait_until_ready()
        return WarmSandboxRef(
            sandbox_id=metadata.sandbox_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=config.runtime.timeout_seconds),
        )
    finally:
        computer.detach()


def claim_ready_sandbox(refs: list[WarmSandboxRef], *, min_ttl_seconds: int) -> ComputerSandbox:
    now = datetime.now(UTC)
    while refs:
        ref = refs.pop(0)
        if (ref.expires_at - now).total_seconds() < min_ttl_seconds:
            continue
        computer = ComputerSandbox.attach(sandbox_id=ref.sandbox_id)
        try:
            computer.wait_until_ready(timeout=15)
        except Exception:
            computer.detach()
            continue
        return computer
    raise RuntimeError("no warm sandbox with enough TTL is ready")


def main() -> None:
    print("Use create_warm_sandbox() from a scheduler and claim_ready_sandbox() from workers.")


if __name__ == "__main__":
    main()
