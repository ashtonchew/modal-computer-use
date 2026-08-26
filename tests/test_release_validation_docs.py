from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TWINE_CHECK_COMMAND = "uvx --from 'twine>=6.2.0' twine check dist/release/*"
METADATA_CHECK_COMMAND = (
    "uv run python scripts/check_distribution_metadata.py dist/release/*"
)
UNPINNED_TWINE_CHECK_COMMAND = "uvx twine check dist/*"
STALE_BENCHMARK_TEST_PATH = "tests/test_benchmark_cli.py"
SHARED_CORE_COMMANDS = (
    "uv sync --extra dev --extra modal --frozen",
    "uv run python scripts/export_openapi.py --check",
    "uv run ruff check .",
    "uv run mypy src",
    "uv run pytest -q",
    "uv run computer-use benchmark report --mock-local --iterations 5 "
    "--output benchmark-results/benchmark-report.json",
)
SHARED_BOUNDARY_SCANS = (
    '! rg "(^|[^A-Za-z0-9_])(import|from) +(openai|anthropic)" src',
    '! rg "NetworkFileSystem" src',
    '! rg -n "print\\([^\\n]*(vnc_url|debug\\.vnc_url|\\.uri|artifact_uri|token|'
    'data_base64|raw_path|stdout|stderr)" examples docs README.md',
    "uv run python scripts/check_repository_hygiene.py",
)
FRESH_DISTRIBUTION_COMMANDS = (
    "test ! -e dist/release",
    "mkdir -p dist/release",
    "uv build --out-dir dist/release",
)
FROZEN_SYNC_COMMAND = "uv sync --extra dev --extra modal --frozen"
RELEASE_BUNDLE_COMMAND = "uv run python scripts/check_release_bundle.py prepare"
RELEASE_CANDIDATE_COMMAND = (
    "uv run python scripts/check_release_candidate.py --tag v2.0.1"
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


def test_release_checklist_keeps_one_approved_distribution_build() -> None:
    checklist = (ROOT / "docs" / "release-checklist.md").read_text(encoding="utf-8")

    assert FROZEN_SYNC_COMMAND in checklist
    assert RELEASE_BUNDLE_COMMAND in checklist
    assert RELEASE_CANDIDATE_COMMAND in checklist
    assert "curated source distribution file set" in checklist
    assert "built once from that clean tagged commit" in checklist
    assert "Upload the wheel and source distribution from `dist/release` to TestPyPI" in checklist
    assert "Record approval for the production" in checklist
    assert "Upload the same approved files from `dist/release` to PyPI" in checklist
    assert "--index-url https://test.pypi.org" in checklist
    assert "--index-url https://pypi.org" in checklist
    assert 'uv add "modal-computer-use[modal]"' in checklist
    assert "Attach the same wheel, source" in checklist
    assert "distribution, and `dist/SHA256SUMS`" in checklist


def test_release_checklist_scans_every_public_ref_before_visibility() -> None:
    checklist = (ROOT / "docs" / "release-checklist.md").read_text(encoding="utf-8")

    assert "Every branch and tag that will become public has been listed" in checklist
    assert "complete reachable history" in checklist
    assert "repeat the scan from a fresh clone before you change repository visibility" in checklist


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
    spec = (ROOT / "docs" / "spec" / "product-spec.md").read_text(encoding="utf-8")

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
    assert "MODAL_COMPUTER_USE_HANDOFF_REGION: us-west-2" in handoff
    assert "MODAL_COMPUTER_USE_HANDOFF_REGION: us-west\n" not in handoff
    assert "MODAL_COMPUTER_USE_RUN_V1_SMOKE" not in handoff
    assert "MODAL_COMPUTER_USE_RUN_NOVNC_SMOKE" not in handoff
    assert "tests/modal_function_session_handoff_smoke_app.py" not in release
    assert HANDOFF_TEST_SELECTOR not in release


def test_workflows_pin_actions_and_limit_default_token_permissions() -> None:
    workflow_dir = ROOT / ".github" / "workflows"
    action_ref = re.compile(r"^\s*uses:\s+[^@\s]+@[0-9a-f]{40}\s+#\s+v\S+$")

    for path in workflow_dir.glob("*.yml"):
        source = path.read_text(encoding="utf-8")
        uses_lines = [line for line in source.splitlines() if "uses:" in line]
        assert uses_lines
        assert all(action_ref.match(line) for line in uses_lines), path.name
        assert "permissions:\n  contents: read" in source
        assert source.count("persist-credentials: false") == source.count(
            "actions/checkout@"
        )


def test_release_workflow_keeps_validation_output_ephemeral() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-validation.yml").read_text(
        encoding="utf-8"
    )

    assert "actions/upload-artifact@" not in workflow
    assert "Generate benchmark report" in workflow
    assert "Validate installed distributions" in workflow
    assert "scripts/smoke_distribution_install.py --distributions dist/release" in workflow
