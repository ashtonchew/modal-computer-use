"""Provision and claim bounded, one-shot Modal warm capacity."""

from __future__ import annotations

import os

from modal_computer_use import (
    BrowserConfig,
    ComputerConfig,
    ComputerSandboxManager,
    ResourceConfig,
    RuntimeConfig,
    WarmPoolPolicy,
)


def warm_config(*, region: str) -> ComputerConfig:
    return ComputerConfig(
        resources=ResourceConfig(profile="browser", cpu=4, memory_mib=8192),
        browser=BrowserConfig(kind="firefox", prewarm=True),
        runtime=RuntimeConfig(timeout_seconds=3600, modal_region=region),
    )


def main() -> None:
    region = os.environ.get("MODAL_COMPUTER_USE_REGION", "").strip()
    if not region:
        raise RuntimeError("set MODAL_COMPUTER_USE_REGION from a current region measurement")
    manager = ComputerSandboxManager()
    policy = WarmPoolPolicy(
        pool_name="interactive-firefox",
        capacity=2,
        min_remaining_seconds=300,
    )
    config = warm_config(region=region)
    fill = manager.fill_warm_pool(config=config, policy=policy)
    print(
        {
            "configured_capacity": fill.configured_capacity,
            "existing_count": fill.existing_count,
            "created_count": fill.created_count,
        }
    )

    claim = manager.claim_warm_pool(config=config, policy=policy)
    try:
        print(
            {
                "pool_hit": claim.metrics.hit,
                "cold_fallback": claim.metrics.cold_fallback,
                "claim_elapsed_ms": claim.metrics.claim_elapsed_ms,
                "remaining_lifetime_seconds": claim.metrics.remaining_lifetime_seconds,
                "cost_accounting": claim.metrics.cost_accounting,
            }
        )
        # Keep the action and frame loop on claim.computer. Do not requeue this Sandbox.
    finally:
        claim.close()


if __name__ == "__main__":
    main()
