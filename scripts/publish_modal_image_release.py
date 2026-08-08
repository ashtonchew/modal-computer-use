from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from modal_computer_use.image import ImageReleaseSpec, publish_image_release

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build, canary, and publish one revision-addressed managed Modal Image."
        )
    )
    parser.add_argument("--logical-release", required=True, help="SDK release, such as 2.0.0")
    parser.add_argument(
        "--variant",
        required=True,
        choices=("standard", "firefox", "chromium"),
    )
    parser.add_argument("--environment", required=True, dest="environment_name")
    parser.add_argument("--manifest", required=True, type=Path, dest="manifest_path")
    parser.add_argument(
        "--image-builder-version",
        required=True,
        dest="expected_image_builder_version",
    )
    parser.add_argument("--revision", help="full Git revision; defaults to the clean current HEAD")
    parser.add_argument("--app-name", default="modal-computer-use-image-builds")
    args = parser.parse_args()

    head, clean = _git_state()
    if not clean:
        parser.error("managed Images can only be published from a clean worktree")
    revision = args.revision or head
    if revision != head:
        parser.error("--revision must match the current HEAD")

    record = publish_image_release(
        ImageReleaseSpec(
            source_revision=revision,
            logical_release=args.logical_release,
            image_variant=args.variant,
            environment_name=args.environment_name,
            manifest_path=args.manifest_path,
            expected_image_builder_version=args.expected_image_builder_version,
            app_name=args.app_name,
        )
    )
    print(json.dumps(record.to_dict(), indent=2, sort_keys=True))
    return 0


def _git_state() -> tuple[str, bool]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required to publish a managed Image release")
    revision = subprocess.run(  # noqa: S603 - resolved git binary and fixed arguments.
        [git, "-C", str(REPOSITORY_ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    status = subprocess.run(  # noqa: S603 - resolved git binary and fixed arguments.
        [git, "-C", str(REPOSITORY_ROOT), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    return revision, not status.strip()


if __name__ == "__main__":
    raise SystemExit(main())
