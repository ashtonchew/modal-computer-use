"""Directory snapshot example.

Modal directory snapshots capture one directory as a reusable Image. Restore
them with mount_image() on a fresh normal sandbox; do not use the snapshot as
the whole desktop image. They are not durable storage or a guarantee that GUI
memory state and browser sessions are restored exactly.
"""

from modal_computer_use import ComputerConfig, ComputerSandbox


def main() -> None:
    snapshot_image = None
    computer = ComputerSandbox.create(config=ComputerConfig())
    try:
        computer.wait_until_ready()
        computer.artifacts.write_bytes("snapshots/ready.txt", b"desktop prepared\n", "text/plain")
        snapshot_image = computer.snapshot_directory("/home/desktop/artifacts/snapshots")
        print({"snapshot_created": True, "snapshot_type": type(snapshot_image).__name__})
    finally:
        computer.terminate()
        computer.detach()

    restored = ComputerSandbox.create(config=ComputerConfig())
    try:
        restored.wait_until_ready()
        restored.mount_image("/home/desktop/artifacts/snapshots", snapshot_image)
        marker = restored.artifacts.read_bytes("snapshots/ready.txt")
        print({"mounted": True, "marker_bytes": len(marker)})
    finally:
        restored.terminate()
        restored.detach()


if __name__ == "__main__":
    main()
