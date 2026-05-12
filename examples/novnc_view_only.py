"""Optional noVNC viewing example.

The noVNC URL is a live desktop secret. This example prints only whether a
URL exists, not the URL itself.
"""

from modal_computer_use import ComputerConfig, ComputerSandbox


def main() -> None:
    computer = ComputerSandbox.create(config=ComputerConfig(expose_vnc="view_only"))
    try:
        computer.wait_until_ready()
        debug_urls = computer.debug.urls()
        print({"vnc_enabled": debug_urls.vnc is not None, "mode": "view_only"})
    finally:
        computer.terminate()
        computer.detach()


if __name__ == "__main__":
    main()
