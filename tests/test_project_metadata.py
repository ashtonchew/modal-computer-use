from __future__ import annotations

import importlib.util
import inspect
import io
import json
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from email.message import Message
from pathlib import Path
from typing import get_type_hints

import pytest

import modal_computer_use

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
SDIST_ONLY_INCLUDE = (
    "LICENSE",
    "README.md",
    "CHANGELOG.md",
    "pyproject.toml",
    "src/modal_computer_use",
)


def test_project_uses_current_license_and_url_metadata() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["build-system"]["requires"] == ["hatchling>=1.27"]
    assert metadata["project"]["license"] == "MIT"
    assert metadata["project"]["license-files"] == ["LICENSE"]
    assert metadata["project"]["urls"] == {
        "Homepage": "https://github.com/ashtonchew/modal-computer-use",
        "Documentation": (
            "https://github.com/ashtonchew/modal-computer-use/blob/main/docs/README.md"
        ),
        "Repository": "https://github.com/ashtonchew/modal-computer-use",
        "Issues": "https://github.com/ashtonchew/modal-computer-use/issues",
        "Changelog": (
            "https://github.com/ashtonchew/modal-computer-use/blob/main/CHANGELOG.md"
        ),
    }
    assert (ROOT / metadata["project"]["readme"]).is_file()
    assert (ROOT / metadata["project"]["license-files"][0]).is_file()


def test_dependency_metadata_uses_inline_pillow_typing_and_keeps_direct_h2() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert "h2>=4.1" in project["dependencies"]
    assert all(
        requirement.lower() != "types-pillow"
        for requirement in project["optional-dependencies"]["dev"]
    )


def test_project_version_matches_runtime_and_openapi() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    openapi = json.loads((ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))

    assert project["version"] == modal_computer_use.__version__ == openapi["info"]["version"]


def test_typed_package_marker_is_present_in_source() -> None:
    assert (ROOT / "src" / "modal_computer_use" / "py.typed").is_file()


def test_root_exported_functions_have_complete_resolvable_annotations() -> None:
    for name in modal_computer_use.__all__:
        value = getattr(modal_computer_use, name)
        if not inspect.isfunction(value):
            continue
        signature = inspect.signature(value)
        assert signature.return_annotation is not inspect.Signature.empty, name
        missing = [
            parameter.name
            for parameter in signature.parameters.values()
            if parameter.annotation is inspect.Signature.empty
        ]
        assert not missing, f"{name} has unannotated parameters: {missing}"
        get_type_hints(value)


def test_public_computer_sandbox_class_type_hints_resolve() -> None:
    public_classes = (
        modal_computer_use.ComputerSandboxManager,
        modal_computer_use.ComputerSandbox,
    )
    for cls in public_classes:
        for name, descriptor in vars(cls).items():
            if name.startswith("_"):
                continue
            value = (
                descriptor.__func__
                if isinstance(descriptor, classmethod | staticmethod)
                else descriptor
            )
            if inspect.isfunction(value):
                get_type_hints(value)


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
    document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = document["project"]
    expected = load_expected_metadata()

    assert expected.name == project["name"]
    assert expected.version == project["version"]
    assert expected.license_expression == project["license"]
    assert expected.license_files == tuple(project["license-files"])
    assert expected.project_urls == project["urls"]
    assert expected.sdist_only_include == SDIST_ONLY_INCLUDE
    assert tuple(
        document["tool"]["hatch"]["build"]["targets"]["sdist"]["only-include"]
    ) == SDIST_ONLY_INCLUDE


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


def _write_sdist_member(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(data)
    archive.addfile(member, io.BytesIO(data))


def _write_valid_sdist(
    archive: tarfile.TarFile,
    *,
    metadata: bytes,
    omitted: frozenset[str] = frozenset(),
    extra_members: tuple[str, ...] = (),
) -> None:
    root = "modal_computer_use-1.0.0"
    members = {
        ".gitignore": b"dist/",
        "PKG-INFO": metadata,
        "LICENSE": b"MIT",
        "README.md": b"# modal-computer-use",
        "CHANGELOG.md": b"# Changelog",
        "pyproject.toml": b"[build-system]",
        f"src/{CHECKER.TYPING_MARKER}": b"",
    }
    for name, data in members.items():
        if name not in omitted:
            _write_sdist_member(archive, f"{root}/{name}", data)
    for name in extra_members:
        _write_sdist_member(archive, f"{root}/{name}", b"unexpected")


def test_distribution_checker_accepts_typing_marker_in_wheel_and_sdist(
    tmp_path: Path,
) -> None:
    metadata = _core_metadata_bytes()
    wheel = tmp_path / "modal_computer_use-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr("modal_computer_use/py.typed", b"")
        archive.writestr("modal_computer_use-1.0.0.dist-info/METADATA", metadata)
        archive.writestr("modal_computer_use-1.0.0.dist-info/licenses/LICENSE", b"MIT")

    sdist = tmp_path / "modal_computer_use-1.0.0.tar.gz"
    with tarfile.open(sdist, mode="w:gz") as archive:
        _write_valid_sdist(archive, metadata=metadata)

    expected = load_expected_metadata()
    CHECKER.validate_distribution(wheel, expected=expected)
    CHECKER.validate_distribution(sdist, expected=expected)


def test_distribution_checker_rejects_unexpected_sdist_member(tmp_path: Path) -> None:
    sdist = tmp_path / "modal_computer_use-1.0.0.tar.gz"
    with tarfile.open(sdist, mode="w:gz") as archive:
        _write_valid_sdist(
            archive,
            metadata=_core_metadata_bytes(),
            extra_members=("docs/drafts/article.md",),
        )

    with pytest.raises(ValueError, match="unexpected sdist members"):
        CHECKER.validate_distribution(sdist, expected=load_expected_metadata())


def test_distribution_checker_rejects_missing_required_sdist_member(
    tmp_path: Path,
) -> None:
    sdist = tmp_path / "modal_computer_use-1.0.0.tar.gz"
    with tarfile.open(sdist, mode="w:gz") as archive:
        _write_valid_sdist(
            archive,
            metadata=_core_metadata_bytes(),
            omitted=frozenset({"CHANGELOG.md"}),
        )

    with pytest.raises(ValueError, match=r"missing required sdist members.*CHANGELOG\.md"):
        CHECKER.validate_distribution(sdist, expected=load_expected_metadata())


def test_strict_downstream_mypy_accepts_root_imports(tmp_path: Path) -> None:
    config = tmp_path / "mypy.ini"
    config.write_text("[mypy]\nstrict = True\npython_version = 3.12\n", encoding="utf-8")
    consumer = tmp_path / "consumer.py"
    consumer.write_text(
        """from typing import assert_type

from modal_computer_use import (
    AsyncDaemonClient,
    ComputerConfig,
    Point,
    RuntimeConfig,
    __version__,
)

config = ComputerConfig()
assert_type(config, ComputerConfig)
assert_type(config.runtime, RuntimeConfig)
assert_type(Point(x=1, y=2).x, int)
assert_type(__version__, str)

async def use_async_client(client: AsyncDaemonClient) -> None:
    assert_type(await client.mouse.position(), Point)
""",
        encoding="utf-8",
    )

    completed = subprocess.run(  # noqa: S603 - fixed interpreter and module
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(config),
            str(consumer),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


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
