"""Pin Modal placement near the caller or model loop after measuring regions."""

from __future__ import annotations

from modal_computer_use import ComputerConfig, ComputerSandbox


def computer_config_for_model_loop(*, modal_region: str | None = None) -> ComputerConfig:
    return ComputerConfig(
        ingress="attested-tunnel",
        runtime={"modal_region": modal_region},
    )


def main() -> None:
    # Run `computer-use benchmark modal-region-ab` from the same caller/model-loop
    # environment first, then set the fastest measured region here.
    computer = ComputerSandbox.create(
        config=computer_config_for_model_loop(modal_region="us-west")
    )
    try:
        computer.wait_until_ready()
        print(
            {
                "ready": computer.status().ready,
                "modal_region": "us-west",
                "policy": "pinned near the benchmarked caller/model loop",
            }
        )
    finally:
        computer.terminate()
        computer.detach()


if __name__ == "__main__":
    main()
