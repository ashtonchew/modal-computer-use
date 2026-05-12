"""Filesystem snapshot example.

Modal filesystem snapshots capture filesystem changes as a reusable Image.
They are not a guarantee that GUI memory state or browser sessions are
restored exactly.
"""

from modal_computer_use import ComputerConfig, ComputerSandbox


def main() -> None:
    computer = ComputerSandbox.create(config=ComputerConfig())
    try:
        computer.wait_until_ready()
        computer.artifacts.write_bytes("snapshots/ready.txt", b"desktop prepared\n", "text/plain")
        snapshot_image = computer.snapshot_filesystem()
        print({"snapshot_created": True, "snapshot_type": type(snapshot_image).__name__})
    finally:
        computer.terminate()
        computer.detach()


if __name__ == "__main__":
    main()
