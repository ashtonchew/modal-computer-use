from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "check_github_release", SCRIPTS / "check_github_release.py"
)
assert SPEC is not None and SPEC.loader is not None
RELEASE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RELEASE
SPEC.loader.exec_module(RELEASE)


def _result(*, stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _payload(*, draft: bool, immutable: bool, assets: list[str] | None = None):
    return {
        "tagName": "v1.2.3",
        "isDraft": draft,
        "isImmutable": immutable,
        "assets": [{"name": name} for name in assets or []],
    }


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (None, "missing"),
        (_payload(draft=True, immutable=False), "draft"),
        (_payload(draft=False, immutable=True), "immutable"),
    ],
)
def test_classifies_safe_release_states(payload: dict[str, Any] | None, expected: str) -> None:
    assert RELEASE.classify_release(payload, tag="v1.2.3") == expected


@pytest.mark.parametrize(
    "payload",
    [
        _payload(draft=False, immutable=False),
        _payload(draft=True, immutable=True),
        {"tagName": "v9.9.9", "isDraft": True, "isImmutable": False},
        {"tagName": "v1.2.3", "isDraft": "true", "isImmutable": False},
    ],
)
def test_rejects_unsafe_or_malformed_release_states(payload: dict[str, Any]) -> None:
    with pytest.raises(RELEASE.GitHubReleaseError):
        RELEASE.classify_release(payload, tag="v1.2.3")


def test_release_query_distinguishes_absence_from_api_failure(monkeypatch) -> None:
    monkeypatch.setattr(RELEASE, "_executable", lambda name: name)

    assert RELEASE.query_release(
        "v1.2.3",
        runner=lambda _command: _result(stderr="release not found", returncode=1),
    ) is None

    with pytest.raises(RELEASE.GitHubReleaseError, match="service unavailable"):
        RELEASE.query_release(
            "v1.2.3",
            runner=lambda _command: _result(stderr="service unavailable", returncode=1),
        )


def test_remote_tag_must_be_annotated_and_match_expected_commit(monkeypatch) -> None:
    monkeypatch.setattr(RELEASE, "_executable", lambda name: name)
    tag_object = "1" * 40
    commit = "2" * 40
    output = (
        f"{tag_object}\trefs/tags/v1.2.3\n"
        f"{commit}\trefs/tags/v1.2.3^{{}}\n"
    )

    def runner(_command):
        return _result(stdout=output)

    RELEASE.verify_remote_tag("v1.2.3", commit, runner=runner)
    with pytest.raises(RELEASE.GitHubReleaseError, match="expected"):
        RELEASE.verify_remote_tag("v1.2.3", "3" * 40, runner=runner)

    def lightweight(_command):
        return _result(stdout=f"{commit}\trefs/tags/v1.2.3\n")

    with pytest.raises(RELEASE.GitHubReleaseError, match="not annotated"):
        RELEASE.verify_remote_tag("v1.2.3", commit, runner=lightweight)


def test_release_assets_must_match_the_complete_local_bundle(tmp_path: Path) -> None:
    distributions = tmp_path / "release"
    distributions.mkdir()
    wheel = distributions / "modal_computer_use-1.2.3-py3-none-any.whl"
    sdist = distributions / "modal_computer_use-1.2.3.tar.gz"
    checksums = tmp_path / "SHA256SUMS"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    checksums.write_text("checksums", encoding="utf-8")
    expected = RELEASE.expected_asset_names(
        distributions=distributions,
        checksums=checksums,
    )

    RELEASE.verify_asset_names(
        _payload(draft=True, immutable=False, assets=expected),
        expected=expected,
    )
    for actual in (expected[:-1], [*expected, "stale.zip"], [*expected, expected[0]]):
        with pytest.raises(RELEASE.GitHubReleaseError, match="release assets differ"):
            RELEASE.verify_asset_names(
                _payload(draft=True, immutable=False, assets=actual),
                expected=expected,
            )
