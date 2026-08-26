from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release-validation.yml"


def test_core_checks_supported_python_versions_with_frozen_lock() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")

    assert 'python-version: ["3.12", "3.13", "3.14"]' in source
    assert "UV_PYTHON: ${{ matrix.python-version }}" in source
    assert "uv sync --extra dev --extra modal --frozen" in source


def test_minimum_direct_dependencies_are_type_checked_and_tested() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")

    assert "name: Minimum direct dependency checks" in source
    assert "uv lock --resolution lowest-direct" in source
    assert "uv sync --extra dev --extra modal --frozen" in source
    assert "uv run --frozen mypy src" in source
    assert "uv run --frozen pytest -q" in source


def test_security_job_runs_pinned_audit_tools() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")

    assert "name: Dependency and security checks" in source
    assert "pip-audit==2.10.1" in source
    assert "bandit==1.9.4" in source
    assert "semgrep==1.172.0" in source
    assert "--all-extras --no-hashes --no-emit-project" in source


def test_dependabot_covers_python_actions_and_native_rust() -> None:
    source = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")

    assert "package-ecosystem: uv" in source
    assert "package-ecosystem: github-actions" in source
    assert "package-ecosystem: cargo" in source
    assert "directory: /src/modal_computer_use/_native/x11_shm" in source
