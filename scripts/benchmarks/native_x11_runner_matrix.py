from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from modal_computer_use.benchmarks.provenance import benchmark_provenance
from modal_computer_use.benchmarks.sdk import run_sdk_surface_benchmark
from modal_computer_use.config import ComputerConfig
from modal_computer_use.sandbox import ComputerSandbox
from modal_computer_use.state import new_run_id

MATRIX_NAME = "native-x11-runner-matrix"
SCHEMA_VERSION = 1
BLOCKS = 3
ITERATIONS = 30
WARMUP_ITERATIONS = 1
ORDER_SEED = 20260802
APP_NAME = "modal-computer-use-native-x11-runner-matrix"
REGION = "us-west-2"
CPU = 4.0
MEMORY_MIB = 8192
INGRESS = "connect"
HTTP_VERSION = "1.1"
RESOURCE_PROFILE = "standard"
INPUT_RATE_LIMIT_PER_SEC = 0
DEFAULT_OUTPUT_ROOT = Path("benchmark-results/native-x11-runner-matrix-2026-08-02")

CELLS: tuple[tuple[str, str], ...] = (
    ("xtest", "asyncio"),
    ("xtest", "isolated-asyncio"),
    ("xdotool", "asyncio"),
    ("xdotool", "isolated-asyncio"),
)

ComputerFactory = Callable[..., ComputerSandbox]
BenchmarkRunner = Callable[..., dict[str, Any]]
RunIdFactory = Callable[[], str]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the canonical native-X11 input/subprocess runner matrix"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "run"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
        command_parser.add_argument("--order-seed", type=int, default=ORDER_SEED)
    args = parser.parse_args(argv)

    source = _source_state()
    plan = build_plan(output_root=args.output_root, order_seed=args.order_seed, source=source)
    if args.command == "plan":
        print(_serialize_json(plan), end="")
        return 0
    if source["git_worktree_clean"] is not True:
        raise RuntimeError("matrix execution requires a clean Git worktree")
    manifest_path = execute_matrix(plan, output_root=args.output_root)
    print(_serialize_json({"status": "complete", "manifest": manifest_path.as_posix()}), end="")
    return 0


def build_plan(
    *,
    output_root: Path,
    order_seed: int,
    source: dict[str, Any],
) -> dict[str, Any]:
    source_with_runner = {**source, "runner": _runner_identity()}
    rng = random.Random(order_seed)  # noqa: S311 - reproducible experiment ordering.
    schedule: list[dict[str, Any]] = []
    sequence = 0
    for block in range(1, BLOCKS + 1):
        cells = list(CELLS)
        rng.shuffle(cells)
        for block_order, (input_backend, subprocess_backend) in enumerate(cells, start=1):
            sequence += 1
            cell_id = f"b{block:02d}-{input_backend}-{subprocess_backend}"
            relative_path = Path("raw") / f"{sequence:02d}-{cell_id}.json"
            schedule.append(
                {
                    "sequence": sequence,
                    "block": block,
                    "block_order": block_order,
                    "cell_id": cell_id,
                    "input_backend": input_backend,
                    "subprocess_backend": subprocess_backend,
                    "raw_artifact": relative_path.as_posix(),
                }
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": MATRIX_NAME,
        "status": "planned",
        "source": source_with_runner,
        "order_seed": order_seed,
        "controls": _controls(),
        "schedule": schedule,
        "assembly": {
            "manifest": (output_root / "manifest.json").as_posix(),
            "expected_blocks": BLOCKS,
            "expected_cells": len(schedule),
            "group_by": ["block", "input_backend", "subprocess_backend"],
            "raw_artifacts": [
                (output_root / item["raw_artifact"]).as_posix() for item in schedule
            ],
        },
    }


def execute_matrix(
    plan: dict[str, Any],
    *,
    output_root: Path,
    computer_factory: ComputerFactory | None = None,
    benchmark_runner: BenchmarkRunner | None = None,
    run_id_factory: RunIdFactory | None = None,
) -> Path:
    computer_factory = computer_factory or ComputerSandbox.create
    benchmark_runner = benchmark_runner or run_sdk_surface_benchmark
    run_id_factory = run_id_factory or new_run_id
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = output_root / "manifest.json"
    manifest = {
        **plan,
        "status": "running",
        "generated_at": _utc_now(),
        "cells": [
            {
                **item,
                "status": "pending",
                "artifact_sha256": None,
                "run_id": None,
            }
            for item in plan["schedule"]
        ],
    }
    _write_json(manifest_path, manifest)

    for cell in manifest["cells"]:
        cell["status"] = "running"
        _write_json(manifest_path, manifest)
        run_id = run_id_factory()
        cell["run_id"] = run_id
        artifact_path = output_root / cell["raw_artifact"]
        try:
            artifact = _execute_cell(
                cell,
                source=plan["source"],
                run_id=run_id,
                computer_factory=computer_factory,
                benchmark_runner=benchmark_runner,
            )
            artifact_bytes = _serialize_json(artifact).encode()
            _write_new_bytes(artifact_path, artifact_bytes)
            cell["artifact_sha256"] = hashlib.sha256(artifact_bytes).hexdigest()
            cell["status"] = "complete" if artifact.get("ok") is True else "failed"
            _write_json(manifest_path, manifest)
            if cell["status"] != "complete":
                raise RuntimeError(f"matrix cell {cell['cell_id']} did not complete successfully")
        except BaseException as exc:
            cell["status"] = "failed"
            cell["error_type"] = type(exc).__name__
            manifest["status"] = "failed"
            manifest["completed_at"] = _utc_now()
            _write_json(manifest_path, manifest)
            raise

    manifest["status"] = "complete"
    manifest["completed_at"] = _utc_now()
    _write_json(manifest_path, manifest)
    return manifest_path


def _execute_cell(
    cell: dict[str, Any],
    *,
    source: dict[str, Any],
    run_id: str,
    computer_factory: ComputerFactory,
    benchmark_runner: BenchmarkRunner,
) -> dict[str, Any]:
    config = _computer_config(cell, run_id=run_id)
    started = time.perf_counter()
    computer: ComputerSandbox | None = None
    cleanup_errors: list[str] = []
    try:
        computer = computer_factory(
            config=config,
            app_name=APP_NAME,
            app_tags={"benchmark": MATRIX_NAME, "benchmark_run_id": run_id},
            tags={
                "benchmark": MATRIX_NAME,
                "benchmark_run_id": run_id,
                "matrix_cell": cell["cell_id"],
            },
            wait=True,
        )
        target_versions = _target_runtime_versions(computer)
        first_frame = computer.first_valid_frame(config)
        metadata = _environment_metadata(
            cell,
            source=source,
            run_id=run_id,
            computer=computer,
            target_versions=target_versions,
            first_frame_size_bytes=len(first_frame),
        )
        result = benchmark_runner(
            surfaces=["daemon-http"],
            iterations=ITERATIONS,
            warmup_iterations=WARMUP_ITERATIONS,
            client=computer.client,
            mode="http",
            base_url=computer.client.base_url,
            environment_metadata=metadata,
        )
        if not isinstance(result, dict):
            raise TypeError("benchmark runner must return a JSON object")
    finally:
        if computer is not None:
            try:
                computer.terminate(wait=True)
            except Exception as exc:
                cleanup_errors.append(f"terminate:{type(exc).__name__}")
            try:
                computer.client.close()
            except Exception as exc:
                cleanup_errors.append(f"client_close:{type(exc).__name__}")

    resource_lifetime_ms = (time.perf_counter() - started) * 1000
    environment = result.setdefault("metadata", {}).setdefault("environment", {})
    if not isinstance(environment, dict):
        raise TypeError("benchmark metadata.environment must be an object")
    environment["modal_resource_lifetime_ms"] = resource_lifetime_ms
    environment["cost_duration_policy"] = (
        "measured_resource_lifetime_including_creation_benchmark_and_teardown"
    )
    environment["cleanup"] = {
        "attempted": True,
        "succeeded": not cleanup_errors,
        "errors": cleanup_errors,
    }
    if cleanup_errors:
        raise RuntimeError("matrix cell cleanup failed: " + ", ".join(cleanup_errors))
    return result


def _computer_config(cell: dict[str, Any], *, run_id: str) -> ComputerConfig:
    config = ComputerConfig(run_id=run_id)
    config.ingress = INGRESS
    config.network.daemon_http_version = HTTP_VERSION
    config.runtime.modal_region = REGION
    config.resources.profile = RESOURCE_PROFILE
    config.resources.cpu = CPU
    config.resources.memory_mib = MEMORY_MIB
    config.actions.input_rate_limit_per_sec = INPUT_RATE_LIMIT_PER_SEC
    config.actions.input_backend = cell["input_backend"]
    config.actions.subprocess_backend = cell["subprocess_backend"]
    return config


def _environment_metadata(
    cell: dict[str, Any],
    *,
    source: dict[str, Any],
    run_id: str,
    computer: ComputerSandbox,
    target_versions: dict[str, Any],
    first_frame_size_bytes: int,
) -> dict[str, Any]:
    sandbox = computer.metadata()
    return {
        **_controls(),
        "modal_run_id": run_id,
        "modal_app_name": APP_NAME,
        "modal_sandbox_id": None if sandbox is None else sandbox.sandbox_id,
        "actual_placement": computer.runtime_placement(),
        "first_observation_api": "ComputerSandbox.first_valid_frame",
        "first_observation_size_bytes": first_frame_size_bytes,
        "matrix_cell": {
            key: cell[key]
            for key in (
                "sequence",
                "block",
                "block_order",
                "cell_id",
                "input_backend",
                "subprocess_backend",
            )
        },
        "target_runtime_versions": target_versions,
        "matrix_runner": source["runner"],
        "provenance": benchmark_provenance(
            caller_path="external-caller",
            modal_region=REGION,
            image_identity="inline:standard",
            cpu=CPU,
            memory_mib=MEMORY_MIB,
            gpu=None,
            git_revision=source["git_revision"],
            git_worktree_clean=source["git_worktree_clean"],
        ),
    }


def _target_runtime_versions(computer: ComputerSandbox) -> dict[str, Any]:
    python_payload = _command_stdout(
        computer,
        "python3",
        "-c",
        (
            "import importlib.metadata as m,json,platform\n"
            "def package_version(name):\n"
            "    try:\n"
            "        return m.version(name)\n"
            "    except m.PackageNotFoundError:\n"
            "        return None\n"
            "print(json.dumps({'python':platform.python_version(),"
            "'modal-computer-use':package_version('modal-computer-use'),"
            "'uvicorn':package_version('uvicorn'),'uvloop':package_version('uvloop')},"
            "sort_keys=True))"
        ),
    )
    os_payload = _command_stdout(
        computer,
        "python3",
        "-c",
        (
            "import json,pathlib;"
            "rows=(line.split('=',1) for line in "
            "pathlib.Path('/etc/os-release').read_text().splitlines() "
            "if '=' in line);data={key:value.strip(chr(34)) for key,value in rows};"
            "selected={key:data.get(key) for key in ('ID','VERSION_ID','PRETTY_NAME')};"
            "print(json.dumps(selected,sort_keys=True))"
        ),
    )
    packages_payload = _command_stdout(
        computer,
        "dpkg-query",
        "-W",
        "-f=${binary:Package}\t${Version}\n",
        "xdotool",
        "xvfb",
        "libxtst6",
        "libx11-6",
    )
    xdotool_version = _command_stdout(computer, "xdotool", "version")
    return {
        "python_packages": _json_object(python_payload, label="Python package probe"),
        "os_release": _json_object(os_payload, label="OS release probe"),
        "debian_packages": _tabular_versions(packages_payload),
        "xdotool": xdotool_version,
    }


def _command_stdout(computer: ComputerSandbox, *command: str) -> str:
    result = computer.commands.run(*command, timeout=30)
    if result.ok is not True or not isinstance(result.output, dict):
        raise RuntimeError(f"runtime version probe failed: {command[0]}")
    stdout = result.output.get("stdout")
    if not isinstance(stdout, str) or not stdout.strip():
        raise RuntimeError(f"runtime version probe returned no stdout: {command[0]}")
    return stdout.strip()


def _json_object(payload: str, *, label: str) -> dict[str, Any]:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} did not return an object")
    return value


def _tabular_versions(payload: str) -> dict[str, str]:
    versions: dict[str, str] = {}
    for line in payload.splitlines():
        package, separator, package_version = line.partition("\t")
        if not separator or not package or not package_version:
            raise RuntimeError("Debian package probe returned malformed output")
        versions[package] = package_version
    if not versions:
        raise RuntimeError("Debian package probe returned no packages")
    return dict(sorted(versions.items()))


def _controls() -> dict[str, Any]:
    return {
        "blocks": BLOCKS,
        "iterations_per_cell": ITERATIONS,
        "warmup_iterations_per_cell": WARMUP_ITERATIONS,
        "modal_ingress": INGRESS,
        "daemon_http_version": HTTP_VERSION,
        "modal_region": REGION,
        "modal_cpu": CPU,
        "modal_memory_mib": MEMORY_MIB,
        "resource_profile": RESOURCE_PROFILE,
        "input_rate_limit_per_sec": INPUT_RATE_LIMIT_PER_SEC,
        "fresh_sandbox_per_cell": True,
    }


def _source_state() -> dict[str, Any]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("matrix execution requires Git")
    revision = subprocess.run(  # noqa: S603 - resolved Git and fixed arguments.
        [git, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()
    status = subprocess.run(  # noqa: S603 - resolved Git and fixed arguments.
        [git, "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout
    branch = subprocess.run(  # noqa: S603 - resolved Git and fixed arguments.
        [git, "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()
    return {
        "git_revision": revision,
        "git_worktree_clean": not status.strip(),
        "git_branch": branch or None,
    }


def _runner_identity() -> dict[str, str]:
    script = Path(__file__).resolve()
    repository = script.parents[2]
    return {
        "name": script.name,
        "path": script.relative_to(repository).as_posix(),
        "sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(_serialize_json(payload), encoding="utf-8")
    temporary.replace(path)


def _write_new_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)


def _serialize_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
