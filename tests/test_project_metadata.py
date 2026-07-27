from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
from email.message import Message
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKER_SPEC = importlib.util.spec_from_file_location(
    "check_distribution_metadata", ROOT / "scripts" / "check_distribution_metadata.py"
)
assert CHECKER_SPEC is not None and CHECKER_SPEC.loader is not None
CHECKER = importlib.util.module_from_spec(CHECKER_SPEC)
sys.modules[CHECKER_SPEC.name] = CHECKER
CHECKER_SPEC.loader.exec_module(CHECKER)
_parse_project_urls = CHECKER._parse_project_urls
_validate_metadata = CHECKER._validate_metadata
load_expected_metadata = CHECKER.load_expected_metadata


def test_project_uses_current_license_and_url_metadata() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["build-system"]["requires"] == ["hatchling>=1.27"]
    assert metadata["project"]["license"] == "MIT"
    assert metadata["project"]["license-files"] == ["LICENSE"]
    assert set(metadata["project"]["urls"]) == {
        "Homepage",
        "Documentation",
        "Repository",
        "Issues",
        "Changelog",
    }
    assert all(
        urlsplit(url).scheme == "https" and urlsplit(url).netloc == "github.com"
        for url in metadata["project"]["urls"].values()
    )
    assert (ROOT / metadata["project"]["readme"]).is_file()
    assert (ROOT / metadata["project"]["license-files"][0]).is_file()


def _core_metadata_bytes(*, version: str | None = None, duplicate_url: bool = False) -> bytes:
    expected = load_expected_metadata()
    metadata = Message()
    metadata["Name"] = expected.name
    metadata["Version"] = version or expected.version
    metadata["License-Expression"] = expected.license_expression
    for license_file in expected.license_files:
        metadata["License-File"] = license_file
    for label, url in expected.project_urls.items():
        metadata["Project-URL"] = f"{label}, {url}"
    if duplicate_url:
        metadata["Project-URL"] = f"Homepage, {expected.project_urls['Homepage']}"
    return metadata.as_bytes()


def test_distribution_metadata_expectations_come_from_pyproject() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    expected = load_expected_metadata()

    assert expected.name == project["name"]
    assert expected.version == project["version"]
    assert expected.license_expression == project["license"]
    assert expected.license_files == tuple(project["license-files"])
    assert expected.project_urls == project["urls"]


def test_distribution_metadata_rejects_source_version_mismatch() -> None:
    with pytest.raises(ValueError, match="expected Version"):
        _validate_metadata(
            _core_metadata_bytes(version="0.0.0"),
            archive=Path("example.whl"),
            expected=load_expected_metadata(),
        )


def test_distribution_metadata_rejects_duplicate_project_url_labels() -> None:
    metadata = Message()
    metadata["Project-URL"] = "Homepage, https://example.test"
    metadata["Project-URL"] = "Homepage, https://example.test"

    with pytest.raises(ValueError, match="duplicate Project-URL label"):
        _parse_project_urls(metadata)


def test_distribution_checker_requires_exactly_one_wheel_and_sdist(tmp_path: Path) -> None:
    wheel_one = tmp_path / "one.whl"
    wheel_two = tmp_path / "two.whl"
    sdist = tmp_path / "one.tar.gz"
    for path in (wheel_one, wheel_two, sdist):
        path.touch()

    completed = subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        [
            sys.executable,
            str(ROOT / "scripts" / "check_distribution_metadata.py"),
            str(wheel_one),
            str(wheel_two),
            str(sdist),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "expected exactly one wheel and one sdist" in completed.stderr
