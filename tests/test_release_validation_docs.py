from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TWINE_CHECK_COMMAND = "uvx --from 'twine>=6.2.0' twine check dist/release/*"
METADATA_CHECK_COMMAND = (
    "uv run python scripts/check_distribution_metadata.py dist/release/*"
)
UNPINNED_TWINE_CHECK_COMMAND = "uvx twine check dist/*"
STALE_BENCHMARK_TEST_PATH = "tests/test_benchmark_cli.py"
SHARED_CORE_COMMANDS = (
    "uv sync --extra dev --extra modal",
    "uv run python scripts/export_openapi.py --check",
    "uv run ruff check .",
    "uv run mypy src",
    "uv run pytest -q",
    "uv run computer-use benchmark report --mock-local --iterations 5 "
    "--output benchmark-report.json",
)
SHARED_BOUNDARY_SCANS = (
    '! rg "(^|[^A-Za-z0-9_])(import|from) +(openai|anthropic)" src',
    '! rg "NetworkFileSystem" src',
    '! rg -n "print\\([^\\n]*(vnc_url|debug\\.vnc_url|\\.uri|artifact_uri|token|'
    'data_base64|raw_path|stdout|stderr)" examples docs README.md',
)
FRESH_DISTRIBUTION_COMMANDS = (
    "test ! -e dist/release",
    "mkdir -p dist/release",
    "uv build --out-dir dist/release",
)
HANDOFF_TEST_SELECTOR = "-k test_modal_deployed_function_session_handoff_smoke"


def test_release_checklist_matches_ci_package_metadata_checker() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-validation.yml").read_text(
        encoding="utf-8"
    )
    checklist = (ROOT / "docs" / "release-checklist.md").read_text(encoding="utf-8")

    assert TWINE_CHECK_COMMAND in workflow
    assert TWINE_CHECK_COMMAND in checklist
    assert METADATA_CHECK_COMMAND in workflow
    assert METADATA_CHECK_COMMAND in checklist
    assert UNPINNED_TWINE_CHECK_COMMAND not in checklist


def test_release_build_uses_a_fresh_dedicated_output_directory() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-validation.yml").read_text(
        encoding="utf-8"
    )
    checklist = (ROOT / "docs" / "release-checklist.md").read_text(encoding="utf-8")

    for command in FRESH_DISTRIBUTION_COMMANDS:
        assert command in workflow
        assert command in checklist


def test_release_checklist_matches_commands_it_claims_to_share_with_ci() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-validation.yml").read_text(
        encoding="utf-8"
    )
    checklist = (ROOT / "docs" / "release-checklist.md").read_text(encoding="utf-8")

    for command in (*SHARED_CORE_COMMANDS, *SHARED_BOUNDARY_SCANS):
        assert command in workflow
        assert command in checklist


def test_release_docs_reference_existing_benchmark_cli_tests() -> None:
    checklist = (ROOT / "docs" / "release-checklist.md").read_text(encoding="utf-8")
    spec = (ROOT / "docs" / "spec" / "modal_computer_use_spec_v8.md").read_text(
        encoding="utf-8"
    )

    assert STALE_BENCHMARK_TEST_PATH not in checklist
    assert STALE_BENCHMARK_TEST_PATH not in spec
    assert "tests/benchmarks/test_report_cli.py" in checklist
    assert "tests/benchmarks/test_action_batch_cli.py" in checklist
    assert (ROOT / "tests" / "benchmarks" / "test_report_cli.py").exists()
    assert (ROOT / "tests" / "benchmarks" / "test_action_batch_cli.py").exists()


def test_manual_handoff_workflow_targets_only_the_bounded_handoff_smoke() -> None:
    handoff = (ROOT / ".github" / "workflows" / "modal-handoff-smoke.yml").read_text(
        encoding="utf-8"
    )
    release = (ROOT / ".github" / "workflows" / "release-validation.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch:" in handoff
    assert "environment: modal-smoke" in handoff
    assert "group: protected-modal-handoff-smoke" in handoff
    assert "tests/modal_function_session_handoff_smoke_app.py" in handoff
    assert HANDOFF_TEST_SELECTOR in handoff
    assert "MODAL_COMPUTER_USE_RUN_HANDOFF_SMOKE" in handoff
    assert "MODAL_COMPUTER_USE_RUN_V1_SMOKE" not in handoff
    assert "MODAL_COMPUTER_USE_RUN_NOVNC_SMOKE" not in handoff
    assert "tests/modal_function_session_handoff_smoke_app.py" not in release
    assert HANDOFF_TEST_SELECTOR not in release
