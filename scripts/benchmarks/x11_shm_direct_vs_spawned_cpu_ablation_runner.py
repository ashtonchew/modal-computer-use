#!/usr/bin/env python3
"""Run fresh 1-CPU and 2-CPU X11-SHM discriminator Sandboxes.

Each CPU profile owns a new Chromium/Xvfb target and runs the unchanged
20-warmup/1000-pair direct-vs-spawned child.  The local entrypoint combines the
two sanitized observations into one non-gating ablation artifact.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import modal

from modal_computer_use.benchmarks import (
    x11_shm_direct_vs_spawned_cpu_ablation as probe,
)


def _load_base_runner() -> Any:
    try:
        return importlib.import_module("x11_shm_direct_vs_spawned_runner")
    except ModuleNotFoundError:
        path = Path(__file__).resolve().with_name("x11_shm_direct_vs_spawned_runner.py")
        spec = importlib.util.spec_from_file_location("x11_shm_direct_vs_spawned_runner", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("base X11-SHM runner is unavailable") from None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module


base_runner = _load_base_runner()

APP_NAME = "mcu-x11-shm-direct-vs-spawned-cpu-ablation"
OUTER_CPU = 2.0
MEMORY_MIB = 2048
PROFILE_CHILD_TIMEOUT_SECONDS = 1_500
OUTER_TIMEOUT_SECONDS = 3_900
REGION = base_runner.REGION
ENVIRONMENT = base_runner.ENVIRONMENT
MODAL_REGION = base_runner.REGION
RUN_TAG_PREFIX = "x11-shm-direct-vs-spawned-cpu-ablation"
_FIXTURE_IDENTITY = probe.fixture_identity(base_runner._load_fixture_html())

app = modal.App(APP_NAME)
image = base_runner.image


def _safe_sandbox_id(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value.startswith("sb-")
        or len(value) > 128
        or not all(character.isalnum() or character in "_.-" for character in value)
    ):
        return None
    return value


def _target_sandbox_id(computer: Any) -> str | None:
    try:
        metadata = computer.metadata()
    except Exception:
        return None
    return _safe_sandbox_id(getattr(metadata, "sandbox_id", None))


def _source_identity(runs: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    identities: list[dict[str, str]] = []
    for run in runs.values():
        observation = run.get("observation")
        target = observation.get("target_identity") if isinstance(observation, Mapping) else None
        if not isinstance(target, Mapping):
            return {}
        module_sha = target.get("module_sha256")
        image_id = target.get("image_object_id")
        if not isinstance(module_sha, str) or not isinstance(image_id, str):
            return {}
        identities.append({"module_sha256": module_sha, "image_object_id": image_id})
    if len(identities) != len(probe.CPU_RUNS) or any(item != identities[0] for item in identities):
        return {}
    return identities[0]


async def _measure_profile(
    label: str,
    resources: Mapping[str, float | int],
    *,
    invocation_tag: str,
) -> dict[str, Any]:
    cpu = resources["cpu"]
    run_tag = f"{invocation_tag}-{label}"
    context = base_runner._TargetContext(cpu=cpu, run_tag=run_tag)
    sandbox_id: str | None = None
    context_cleanup_failed = False
    try:
        computer = await context.__aenter__()
        sandbox_id = _target_sandbox_id(computer)
        observation = await base_runner._run_child(
            computer,
            cpu=cpu,
            timeout_seconds=PROFILE_CHILD_TIMEOUT_SECONDS,
        )
    except BaseException:
        observation = base_runner._empty_rejected_observation("session_start")
        observation["configured_resources"] = dict(resources)
    finally:
        try:
            await context.__aexit__(None, None, None)
        except BaseException:
            context_cleanup_failed = True
    cleanup = await base_runner._terminal_cleanup(run_tag)
    if context_cleanup_failed:
        errors = set(cleanup.get("cleanup_error_types", []))
        errors.add("CleanupError")
        cleanup["cleanup_error_types"] = sorted(errors)
        cleanup["succeeded"] = False
    return {
        "observation": observation,
        "cleanup": cleanup,
        "sandbox_id": sandbox_id,
    }


@app.function(
    image=image,
    cpu=OUTER_CPU,
    memory=MEMORY_MIB,
    timeout=OUTER_TIMEOUT_SECONDS,
    region=MODAL_REGION,
    retries=0,
)
def run() -> dict[str, Any]:
    async def measure() -> dict[str, Any]:
        invocation_tag = f"{RUN_TAG_PREFIX}-{uuid.uuid4().hex}"
        runs: dict[str, Any] = {}
        for label, resources in probe.CPU_RUNS.items():
            runs[label] = await _measure_profile(
                label,
                resources,
                invocation_tag=invocation_tag,
            )
        return {
            "runs": runs,
            "fixture_identity": _FIXTURE_IDENTITY,
            "source_identity": _source_identity(runs),
        }

    return asyncio.run(measure())


@app.local_entrypoint()
def main(output: str | None = None) -> None:
    if not output:
        raise SystemExit("an explicit --output artifact path is required")
    path = Path(output).expanduser()
    if not path.is_absolute():
        raise SystemExit("--output must be an absolute path")
    if path.exists() or path.is_symlink():
        raise SystemExit("--output already exists; refusing to overwrite it")
    path.parent.mkdir(parents=True, exist_ok=True)
    provenance = {
        label: base_runner.local_provenance(float(resources["cpu"]))
        for label, resources in probe.CPU_RUNS.items()
    }
    remote = cast(Mapping[str, Any], run.remote())
    raw_runs = remote.get("runs")
    if not isinstance(raw_runs, Mapping):
        raw_runs = {}
    runs: dict[str, Any] = {}
    for label in probe.CPU_RUNS:
        raw = raw_runs.get(label)
        if not isinstance(raw, Mapping):
            continue
        runs[label] = {
            "observation": raw.get("observation"),
            "cleanup": raw.get("cleanup"),
            "provenance": provenance[label],
            "sandbox_id": raw.get("sandbox_id"),
        }
    fixture_identity = remote.get("fixture_identity")
    fixture_identity = fixture_identity if fixture_identity == _FIXTURE_IDENTITY else ""
    source_identity = remote.get("source_identity")
    artifact = probe.build_artifact(
        runs,
        fixture_identity=fixture_identity,
        source_identity=source_identity if isinstance(source_identity, Mapping) else {},
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(json.dumps(artifact, indent=2, sort_keys=True))
    except (FileExistsError, OSError):
        raise SystemExit("--output appeared during the run; refusing to overwrite it") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    if artifact["status"] != "complete":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
