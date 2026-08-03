"""Persist one run-scoped artifact to an explicitly selected Modal Volume v2.

Artifact URIs and raw paths can reveal run internals. This example writes a
small artifact and prints only bounded metadata plus explicit sync status. It
terminates the Sandbox it creates but deliberately retains the caller-owned
Volume and its persisted data.
"""

from __future__ import annotations

import argparse
import os
import uuid
from collections.abc import Sequence

from modal_computer_use import ComputerConfig, ComputerSandbox, StorageConfig

ARTIFACTS_DIR = "/home/desktop/artifacts"


def _environment_value(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--volume-name",
        default=_environment_value("MODAL_COMPUTER_USE_VOLUME_NAME"),
        help="Modal Volume name (or set MODAL_COMPUTER_USE_VOLUME_NAME)",
    )
    parser.add_argument(
        "--create-volume",
        action="store_true",
        help="create the named Volume v2 when it does not exist",
    )
    return parser


def _modal_volume(name: str, *, create_if_missing: bool) -> object:
    try:
        import modal
        from modal_proto import api_pb2
    except ImportError as exc:
        raise RuntimeError("install the modal extra before running this example") from exc

    volume = modal.Volume.from_name(
        name,
        create_if_missing=create_if_missing,
        version=api_pb2.VolumeFsVersion.Value("VOLUME_FS_VERSION_V2"),
    )
    return volume.hydrate()


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    volume_name = (args.volume_name or "").strip()
    if not volume_name:
        parser.error("provide --volume-name or set MODAL_COMPUTER_USE_VOLUME_NAME")

    suffix = uuid.uuid4().hex
    run_id = f"volume-artifacts-{suffix}"
    artifact_path = f"runs/{suffix}/example.txt"
    volume = _modal_volume(volume_name, create_if_missing=args.create_volume)
    computer = ComputerSandbox.create(
        config=ComputerConfig(
            run_id=run_id,
            storage=StorageConfig(persist_artifacts=True),
        ),
        volumes={ARTIFACTS_DIR: volume},
        wait=False,
    )
    try:
        computer.wait_until_ready()
        info = computer.artifacts.write_bytes(artifact_path, b"hello\n", "text/plain")
        sync = computer.artifacts.sync()
        if not sync.ok or not sync.persistent:
            raise RuntimeError("verified Modal Volume artifact sync did not succeed")
        print(
            {
                "artifact_kind": info.kind,
                "content_type": info.content_type,
                "size_bytes": info.size_bytes,
                "sync_ok": sync.ok,
                "persistent": sync.persistent,
            }
        )
    finally:
        try:
            computer.terminate(wait=True)
        finally:
            computer.detach()


if __name__ == "__main__":
    main()
