from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-release.yml"


def _job(source: str, name: str, next_name: str | None) -> str:
    start = source.index(f"  {name}:\n")
    end = len(source) if next_name is None else source.index(f"  {next_name}:\n", start)
    return source[start:end]


def test_release_workflow_builds_once_and_orders_publication() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")

    assert '      - "v*"' in source
    assert "workflow_dispatch:" not in source
    assert source.count("uv build --out-dir dist/release") == 1
    assert "--main-ref refs/remotes/origin/main" in source
    assert "group: publish-${{ github.ref }}" in source
    assert "cancel-in-progress: false" in source
    assert "needs: build" in _job(source, "publish-testpypi", "verify-testpypi")
    assert "needs: publish-testpypi" in _job(source, "verify-testpypi", "publish-pypi")
    assert "needs: verify-testpypi" in _job(source, "publish-pypi", "verify-pypi")
    assert "needs: publish-pypi" in _job(source, "verify-pypi", "github-release")
    assert "needs: verify-pypi" in _job(source, "github-release", None)


def test_oidc_jobs_are_code_free_and_least_privileged() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    testpypi = _job(source, "publish-testpypi", "verify-testpypi")
    pypi = _job(source, "publish-pypi", "verify-pypi")

    for job, environment in ((testpypi, "testpypi"), (pypi, "pypi")):
        assert f"name: {environment}" in job
        assert "id-token: write" in job
        assert "contents: write" not in job
        assert "actions/checkout@" not in job
        assert "scripts/" not in job
        assert "packages-dir: dist/release" in job

    assert "skip-existing: true" in testpypi
    assert "skip-existing:" not in pypi


def test_bundle_and_release_assets_remain_the_same_bytes() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    github_release = _job(source, "github-release", None)

    assert source.count("name: python-release-distributions") == 6
    assert (
        "uv run python scripts/smoke_distribution_install.py --distributions dist/release"
        in source
    )
    assert source.count("sha256sum --check ../SHA256SUMS") == 3
    assert "--draft" in github_release
    assert "scripts/check_github_release.py tag" in github_release
    assert '--expected-commit "$GITHUB_SHA"' in github_release
    assert "scripts/check_github_release.py state" in github_release
    assert "scripts/check_github_release.py assets" in github_release
    assert github_release.index("gh release create") < github_release.index("gh release upload")
    assert github_release.index("gh release upload") < github_release.index(
        "scripts/check_github_release.py assets"
    )
    assert github_release.index("scripts/check_github_release.py assets") < github_release.index(
        "--draft=false"
    )
    assert github_release.index("gh release upload") < github_release.index("--draft=false")
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in github_release
    assert "fetch-depth: 0" in github_release
    assert "persist-credentials: false" in github_release
    assert "if: steps.release.outputs.state == 'draft'" in github_release
    assert "for attempt in 1 2 3 4 5 6" in github_release
    assert 'if [ "$attempt" -eq 6 ]' in github_release
    assert "sleep 5" in github_release
    assert "gh release verify \"$GITHUB_REF_NAME\"" in github_release
    assert github_release.count("gh release verify-asset") == 3
    assert "GH_REPO: ${{ github.repository }}" in github_release


def test_pypi_verification_uses_the_documented_clean_install_command() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    verify_pypi = _job(source, "verify-pypi", "github-release")

    assert 'uv add "modal-computer-use[modal]"' in verify_pypi
    assert "import modal_computer_use" in verify_pypi
    assert 'scripts["computer-use-daemon"]' in verify_pypi


def test_release_workflow_uses_current_artifact_and_publisher_actions() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")

    assert "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1" in source
    assert (
        "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # v1.14.2"
        in source
    )
    assert "# v7.0.0" not in source
    assert "# v1.14.0" not in source
