from __future__ import annotations

import hashlib
import importlib.util
import json
import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest

from modal_computer_use import benchmarks as benchmark_package

ROOT = Path(__file__).resolve().parents[2]
LEGACY_MODULES = (
    "modal_computer_use.benchmarks.modal_optimization",
    "modal_computer_use.benchmarks.modal_optimization_execution",
)
LEGACY_SCRIPTS = (
    ROOT / "scripts" / "run_modal_optimization_benchmark.py",
    ROOT / "scripts" / "sanitize_modal_optimization_benchmark.py",
)
HISTORICAL_ARTIFACTS = (
    (
        ROOT / "benchmark-data" / "modal-optimization-results-2026-07-19.json",
        "8c21cf1338fd747dca57bca6941c307270069712",
        "3d4f93241e3791420f083aeb3045d589ee2f72f174b7393a87249ec49ec4ec9e",
    ),
    (
        ROOT / "benchmark-data" / "modal-optimization-native-x11-2026-07-24.json",
        "4ea0deb8d2cb37668cab3310a5394487e9140869",
        "66567de8a661bd0c5281187b274e7366735f4bb1ac4c4a7e59576d6e0c660b15",
    ),
)
CURRENT_BENCHMARK_SCRIPTS = (
    ROOT / "scripts" / "run_modal_v2_candidate_benchmark.py",
    ROOT / "scripts" / "sanitize_modal_v2_candidate_benchmark.py",
    ROOT / "scripts" / "run_modal_optimized_frontier_benchmark.py",
    ROOT / "scripts" / "sanitize_modal_optimized_frontier_benchmark.py",
)


def test_legacy_harness_modules_and_scripts_are_undiscoverable() -> None:
    discovered = {
        module.name
        for module in pkgutil.iter_modules(
            benchmark_package.__path__, prefix=f"{benchmark_package.__name__}."
        )
    }
    for module_name in LEGACY_MODULES:
        assert importlib.util.find_spec(module_name) is None
        assert module_name not in discovered
    for script in LEGACY_SCRIPTS:
        assert not script.exists()


def test_current_benchmark_modules_import_without_optional_providers() -> None:
    code = """
import importlib
import importlib.abc
import sys

blocked = {"modal", "openai", "anthropic", "daytona", "e2b_desktop", "tzafon"}
modules = (
    "modal_computer_use.benchmarks.modal_optimized_provider",
    "modal_computer_use.benchmarks.provider_results",
    "modal_computer_use.benchmarks.observation_surface",
    "modal_computer_use.benchmarks.modal_region_ab",
    "modal_computer_use.benchmarks.modal_v2_candidate",
    "modal_computer_use.benchmarks.modal_v2_candidate_execution",
    "modal_computer_use.benchmarks.modal_optimized_frontier",
    "modal_computer_use.benchmarks.modal_optimized_frontier_execution",
)

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split(".", 1)[0] in blocked:
            raise ImportError(f"blocked optional dependency: {fullname}")
        return None

sys.meta_path.insert(0, Blocker())
for module_name in modules:
    importlib.import_module(module_name)
assert blocked.isdisjoint(sys.modules)
"""
    result = subprocess.run(  # noqa: S603 - fixed interpreter and source string
        [sys.executable, "-c", code],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(("artifact_path", "source_sha", "artifact_sha256"), HISTORICAL_ARTIFACTS)
def test_historical_artifacts_retain_exact_legacy_provenance(
    artifact_path: Path,
    source_sha: str,
    artifact_sha256: str,
) -> None:
    raw = artifact_path.read_bytes()
    payload = json.loads(raw)

    assert hashlib.sha256(raw).hexdigest() == artifact_sha256
    assert payload["provenance"]["source_sha"] == source_sha
    assert payload["command_manifest"]["benchmark"].startswith(
        "uv run python scripts/run_modal_optimization_benchmark.py run "
    )
    assert payload["command_manifest"]["normalize"].startswith(
        "uv run python scripts/sanitize_modal_optimization_benchmark.py "
    )


def test_current_frontier_placement_artifact_is_retained() -> None:
    path = ROOT / "benchmark-data" / "modal-v2-placement-capability-2026-07-19.json"
    raw = path.read_bytes()

    assert json.loads(raw)["schema_version"] == 1
    assert hashlib.sha256(raw).hexdigest() == (
        "d5ee2b31d70e924bdd9b24c55c4361e0adee1234c18246b245d0568b8aa89244"
    )


@pytest.mark.parametrize("script", CURRENT_BENCHMARK_SCRIPTS)
def test_retained_benchmark_scripts_expose_help(script: Path) -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        [sys.executable, str(script), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
