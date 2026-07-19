from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from modal_computer_use.image import publish_named_images

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish revision-addressed standard, Firefox, and Chromium Modal Images."
    )
    parser.add_argument("--revision", help="full Git revision; defaults to the clean current HEAD")
    parser.add_argument("--environment", dest="environment_name")
    parser.add_argument("--app-name", default="modal-computer-use-image-builds")
    args = parser.parse_args()

    head, clean = _git_state()
    if not clean:
        parser.error("named Images can only be published from a clean worktree")
    revision = args.revision or head
    if revision != head:
        parser.error("--revision must match the current HEAD")

    identities = publish_named_images(
        revision=revision,
        app_name=args.app_name,
        environment_name=args.environment_name,
    )
    print(json.dumps(identities, indent=2, sort_keys=True))
    return 0


def _git_state() -> tuple[str, bool]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required to publish named Images")
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
