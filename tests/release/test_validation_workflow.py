from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release-validation.yml"


def test_core_checks_supported_python_versions_with_frozen_lock() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")

    assert 'python-version: ["3.12", "3.14"]' in source
    assert "UV_PYTHON: ${{ matrix.python-version }}" in source
    assert "uv sync --extra dev --extra modal --frozen" in source


def test_security_job_runs_pinned_audit_tools() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")

    assert "name: Dependency and security checks" in source
    assert "pip-audit==2.10.1" in source
    assert "bandit==1.9.4" in source
    assert "semgrep==1.172.0" in source
    assert "--all-extras --no-hashes --no-emit-project" in source
