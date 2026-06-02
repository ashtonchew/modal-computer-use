from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TWINE_CHECK_COMMAND = "uvx --from 'twine>=6.2.0' twine check dist/*"
UNPINNED_TWINE_CHECK_COMMAND = "uvx twine check dist/*"
STALE_BENCHMARK_TEST_PATH = "tests/test_benchmark_cli.py"


def test_release_checklist_matches_ci_package_metadata_checker() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-validation.yml").read_text(
        encoding="utf-8"
    )
    checklist = (ROOT / "docs" / "release-checklist.md").read_text(encoding="utf-8")

    assert TWINE_CHECK_COMMAND in workflow
    assert TWINE_CHECK_COMMAND in checklist
    assert UNPINNED_TWINE_CHECK_COMMAND not in checklist


def test_release_docs_reference_existing_benchmark_cli_tests() -> None:
    checklist = (ROOT / "docs" / "release-checklist.md").read_text(encoding="utf-8")
    spec = (ROOT / "docs" / "spec" / "modal_computer_use_spec_v7.md").read_text(
        encoding="utf-8"
    )

    assert STALE_BENCHMARK_TEST_PATH not in checklist
    assert STALE_BENCHMARK_TEST_PATH not in spec
    assert "tests/benchmarks/test_report_cli.py" in checklist
    assert "tests/benchmarks/test_action_batch_cli.py" in checklist
    assert (ROOT / "tests" / "benchmarks" / "test_report_cli.py").exists()
    assert (ROOT / "tests" / "benchmarks" / "test_action_batch_cli.py").exists()
