"""Browser prewarm and optional GPU profile example.

GPU is opt-in. Use it only when page rendering is the measured bottleneck.
"""

from modal_computer_use import BrowserConfig, ComputerConfig, ComputerSandbox, ResourceConfig


def browser_config(*, use_gpu: bool = False) -> ComputerConfig:
    return ComputerConfig(
        resources=ResourceConfig(
            profile="browser-gpu" if use_gpu else "browser",
            cpu=4,
            memory_mib=8192,
            gpu="T4" if use_gpu else None,
        ),
        browser=BrowserConfig(kind="firefox", prewarm=True),
    )


def main() -> None:
    computer = ComputerSandbox.create(config=browser_config(use_gpu=False))
    try:
        computer.wait_until_ready()
        computer.browser.open_url("https://example.com")
        status = computer.status()
        print(
            {
                "ready": status.ready,
                "profile": status.resources.get("profile"),
                "browser_prewarm": True,
                "gpu_enabled": False,
            }
        )
    finally:
        computer.terminate()
        computer.detach()


if __name__ == "__main__":
    main()
