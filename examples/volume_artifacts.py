"""Volume-backed artifact pattern with safe output.

Artifact URIs and raw paths can reveal run internals. This example writes a
small artifact and prints only bounded metadata plus explicit sync status.
"""

from modal_computer_use import ComputerSandbox


def main() -> None:
    computer = ComputerSandbox.local(token="dev")
    info = computer.artifacts.write_bytes("downloads/example.txt", b"hello\n", "text/plain")
    sync = computer.artifacts.sync()
    print(
        {
            "artifact_kind": info.kind,
            "content_type": info.content_type,
            "size_bytes": info.size_bytes,
            "sync_ok": sync.ok,
            "persistent": sync.persistent,
        }
    )


if __name__ == "__main__":
    main()
