from __future__ import annotations

import argparse
import hashlib
from pathlib import Path, PurePath

from check_distribution_metadata import load_expected_metadata, validate_distribution

ROOT = Path(__file__).resolve().parents[1]


class ReleaseBundleError(ValueError):
    """Raised when a release bundle is incomplete or has changed."""


def distribution_paths(directory: Path) -> tuple[Path, Path]:
    if not directory.is_dir():
        raise ReleaseBundleError(f"distribution directory does not exist: {directory}")
    entries = sorted(
        path for path in directory.iterdir() if path.is_file() and path.name != ".gitignore"
    )
    wheels = [path for path in entries if path.suffix == ".whl"]
    sdists = [path for path in entries if path.name.endswith(".tar.gz")]
    unsupported = [path.name for path in entries if path not in (*wheels, *sdists)]
    if unsupported:
        raise ReleaseBundleError(f"unexpected files in distribution directory: {unsupported}")
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseBundleError(
            f"expected exactly one wheel and one sdist; found wheel={len(wheels)}, "
            f"sdist={len(sdists)}"
        )
    return wheels[0], sdists[0]


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare_release_bundle(*, distributions: Path, checksums: Path) -> None:
    paths = distribution_paths(distributions)
    expected = load_expected_metadata(ROOT / "pyproject.toml")
    for path in paths:
        validate_distribution(path, expected=expected)
    checksums.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(f"{_digest(path)}  {path.name}\n" for path in sorted(paths))
    checksums.write_text(content, encoding="utf-8")


def verify_release_bundle(*, distributions: Path, checksums: Path) -> None:
    paths = distribution_paths(distributions)
    if not checksums.is_file():
        raise ReleaseBundleError(f"checksum file does not exist: {checksums}")

    recorded: dict[str, str] = {}
    for line_number, line in enumerate(checksums.read_text(encoding="utf-8").splitlines(), start=1):
        parts = line.split()
        if len(parts) != 2:
            raise ReleaseBundleError(f"{checksums}:{line_number}: expected SHA-256 and filename")
        digest, filename = parts
        if not _is_sha256(digest):
            raise ReleaseBundleError(f"{checksums}:{line_number}: invalid SHA-256 digest")
        if (
            PurePath(filename).name != filename
            or "/" in filename
            or "\\" in filename
            or filename in {".", ".."}
        ):
            raise ReleaseBundleError(f"{checksums}:{line_number}: filename must be a basename")
        if filename in recorded:
            raise ReleaseBundleError(f"{checksums}:{line_number}: duplicate filename {filename!r}")
        recorded[filename] = digest

    expected_names = {path.name for path in paths}
    if set(recorded) != expected_names:
        raise ReleaseBundleError(
            f"checksum entries differ: expected {sorted(expected_names)}, found {sorted(recorded)}"
        )
    for path in paths:
        actual = _digest(path)
        if recorded[path.name] != actual:
            raise ReleaseBundleError(f"SHA-256 mismatch for {path.name}")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare or verify a Python release bundle.")
    parser.add_argument("mode", choices=("prepare", "verify"))
    parser.add_argument("--distributions", required=True, type=Path)
    parser.add_argument("--checksums", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.mode == "prepare":
            prepare_release_bundle(distributions=args.distributions, checksums=args.checksums)
        else:
            verify_release_bundle(distributions=args.distributions, checksums=args.checksums)
    except (OSError, ReleaseBundleError, ValueError) as exc:
        parser.error(str(exc))
    verb = "prepared" if args.mode == "prepare" else "verified"
    print(f"{verb} release bundle at {args.distributions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
