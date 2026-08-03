from __future__ import annotations

import argparse
import tarfile
import tomllib
import zipfile
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from email.message import Message
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
TYPING_MARKER = "modal_computer_use/py.typed"
SDIST_BACKEND_REQUIRED_FILES = frozenset({".gitignore", "PKG-INFO"})


@dataclass(frozen=True)
class ExpectedMetadata:
    name: str
    version: str
    license_expression: str
    license_files: tuple[str, ...]
    project_urls: dict[str, str]
    sdist_only_include: tuple[str, ...]


def load_expected_metadata(pyproject: Path = ROOT / "pyproject.toml") -> ExpectedMetadata:
    document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = document["project"]
    return ExpectedMetadata(
        name=project["name"],
        version=project["version"],
        license_expression=project["license"],
        license_files=tuple(project["license-files"]),
        project_urls=dict(project["urls"]),
        sdist_only_include=tuple(
            document["tool"]["hatch"]["build"]["targets"]["sdist"]["only-include"]
        ),
    )


def _one_matching_path(paths: Iterable[str], suffix: str, *, archive: Path) -> str:
    matches = [path for path in paths if path.endswith(suffix)]
    if len(matches) != 1:
        raise ValueError(
            f"{archive}: expected one {suffix} entry, found {len(matches)}: {matches}"
        )
    return matches[0]


def _parse_project_urls(metadata: Message) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in metadata.get_all("Project-URL", []):
        label, separator, url = value.partition(",")
        if not separator:
            raise ValueError(f"invalid Project-URL metadata: {value!r}")
        normalized_label = label.strip()
        if normalized_label in result:
            raise ValueError(f"duplicate Project-URL label: {normalized_label!r}")
        result[normalized_label] = url.strip()
    return result


def _validate_metadata(
    metadata_bytes: bytes, *, archive: Path, expected: ExpectedMetadata
) -> None:
    metadata = BytesParser().parsebytes(metadata_bytes)
    for header, expected_value in (("Name", expected.name), ("Version", expected.version)):
        value = metadata.get(header)
        if value != expected_value:
            raise ValueError(
                f"{archive}: expected {header} {expected_value!r}, found {value!r}"
            )

    license_expression = metadata.get("License-Expression")
    if license_expression != expected.license_expression:
        raise ValueError(
            f"{archive}: expected License-Expression {expected.license_expression!r}, "
            f"found {license_expression!r}"
        )

    license_files = metadata.get_all("License-File", [])
    if Counter(license_files) != Counter(expected.license_files):
        raise ValueError(
            f"{archive}: expected License-File entries {expected.license_files!r}, "
            f"found {license_files!r}"
        )

    project_urls = _parse_project_urls(metadata)
    if project_urls != expected.project_urls:
        raise ValueError(
            f"{archive}: Project-URL metadata differs\n"
            f"expected: {expected.project_urls!r}\n"
            f"found: {project_urls!r}"
        )


def _validate_wheel(path: Path, *, expected: ExpectedMetadata) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        metadata_path = _one_matching_path(names, ".dist-info/METADATA", archive=path)
        _validate_metadata(archive.read(metadata_path), archive=path, expected=expected)

        if TYPING_MARKER not in names:
            raise ValueError(f"{path}: missing bundled typing marker at {TYPING_MARKER}")

        metadata_root = PurePosixPath(metadata_path).parent
        for license_file in expected.license_files:
            license_path = str(metadata_root / "licenses" / license_file)
            if license_path not in names:
                raise ValueError(f"{path}: missing bundled license at {license_path}")


def _validate_sdist(path: Path, *, expected: ExpectedMetadata) -> None:
    with tarfile.open(path, mode="r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        metadata_path = _one_matching_path(names, "/PKG-INFO", archive=path)
        metadata_file = archive.extractfile(metadata_path)
        if metadata_file is None:
            raise ValueError(f"{path}: cannot read {metadata_path}")
        _validate_metadata(metadata_file.read(), archive=path, expected=expected)

        source_root = PurePosixPath(metadata_path).parent
        included_paths = tuple(PurePosixPath(item) for item in expected.sdist_only_include)
        included_files = {
            included_path
            for included_path in included_paths
            if (ROOT / included_path).is_file()
        }
        included_directories = set(included_paths) - included_files
        backend_required_paths = {
            PurePosixPath(item) for item in SDIST_BACKEND_REQUIRED_FILES
        }
        regular_paths: set[PurePosixPath] = set()
        unexpected_paths: list[str] = []
        for member in members:
            if not member.isfile():
                continue
            member_path = PurePosixPath(member.name)
            try:
                relative_path = member_path.relative_to(source_root)
            except ValueError:
                unexpected_paths.append(member.name)
                continue
            regular_paths.add(relative_path)
            if relative_path in backend_required_paths:
                continue
            if relative_path in included_files:
                continue
            if any(directory in relative_path.parents for directory in included_directories):
                continue
            unexpected_paths.append(member.name)

        if unexpected_paths:
            raise ValueError(f"{path}: unexpected sdist members: {sorted(unexpected_paths)!r}")

        required_paths = {
            *backend_required_paths,
            *included_files,
            PurePosixPath("src") / TYPING_MARKER,
        }
        missing_paths = sorted(str(item) for item in required_paths - regular_paths)
        if missing_paths:
            raise ValueError(f"{path}: missing required sdist members: {missing_paths!r}")

        for license_file in expected.license_files:
            license_path = str(source_root / license_file)
            if license_path not in names:
                raise ValueError(f"{path}: missing bundled license at {license_path}")


def validate_distribution(path: Path, *, expected: ExpectedMetadata) -> None:
    if path.suffix == ".whl":
        _validate_wheel(path, expected=expected)
        return
    if path.name.endswith(".tar.gz"):
        _validate_sdist(path, expected=expected)
        return
    raise ValueError(f"{path}: expected a .whl or .tar.gz distribution")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate metadata and file boundaries in built distributions."
    )
    parser.add_argument("distributions", nargs="+", type=Path)
    args = parser.parse_args()

    by_type: dict[str, list[Path]] = {"wheel": [], "sdist": []}
    for path in args.distributions:
        if not path.is_file():
            parser.error(f"distribution does not exist: {path}")
        if path.suffix == ".whl":
            by_type["wheel"].append(path)
        elif path.name.endswith(".tar.gz"):
            by_type["sdist"].append(path)
        else:
            parser.error(f"{path}: expected a .whl or .tar.gz distribution")

    invalid_counts = {kind: paths for kind, paths in by_type.items() if len(paths) != 1}
    if invalid_counts:
        details = ", ".join(
            f"{kind}={len(paths)}" for kind, paths in sorted(invalid_counts.items())
        )
        parser.error(f"expected exactly one wheel and one sdist; found {details}")

    expected = load_expected_metadata()
    for path in (*by_type["wheel"], *by_type["sdist"]):
        validate_distribution(path, expected=expected)
        print(f"validated {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
