from __future__ import annotations

import re
import shutil
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

SDK_DISTRIBUTIONS = (
    "modal-computer-use",
    "modal",
    "daytona",
    "e2b-desktop",
)


def benchmark_provenance(
    *,
    caller_path: str,
    modal_region: str | None,
    image_identity: str,
    cpu: float | None,
    memory_mib: int | None,
    gpu: str | None,
    git_revision: str | None = None,
    git_worktree_clean: bool | None = None,
) -> dict[str, Any]:
    """Collect safe, explicit benchmark inputs without importing provider SDKs."""
    if git_revision is None:
        revision, detected_clean = _git_state()
        git_worktree_clean = detected_clean
    else:
        revision = git_revision
    if re.fullmatch(r"[0-9a-f]{40}", revision or "") is None:
        revision = "unavailable"
    return {
        "resolved_resources": {
            "cpu": _resource_value(cpu),
            "memory": _resource_value(
                None if memory_mib is None else memory_mib / 1024,
                unit="GiB",
            ),
            "gpu": _resource_value(gpu),
        },
        "sdk_versions": {name: _distribution_version(name) for name in SDK_DISTRIBUTIONS},
        "image_identity": image_identity,
        "git_revision": revision,
        "git_worktree_clean": git_worktree_clean,
        "caller_path": caller_path,
        "region": modal_region or "provider-default",
        "cost_status": "see_run_and_surface_cost_status",
    }


def _resource_value(value: object | None, *, unit: str | None = None) -> dict[str, Any]:
    result = {
        "requested": value,
        "resolved": value,
        "status": "explicit" if value is not None else "provider_default_unavailable",
    }
    if unit is not None:
        result["unit"] = unit
    return result


def _distribution_version(distribution: str) -> str | None:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return None


def _git_state() -> tuple[str | None, bool | None]:
    git = shutil.which("git")
    if git is None:
        return None, None
    repository = Path(__file__).resolve().parents[3]
    try:
        revision_result = subprocess.run(  # noqa: S603 - resolved git binary and fixed arguments.
            [git, "-C", str(repository), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        status_result = subprocess.run(  # noqa: S603 - resolved git binary and fixed arguments.
            [git, "-C", str(repository), "status", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    revision = revision_result.stdout.strip()
    return (
        revision if revision_result.returncode == 0 else None,
        not status_result.stdout.strip() if status_result.returncode == 0 else None,
    )
