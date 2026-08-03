from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "check_release_bundle", SCRIPTS / "check_release_bundle.py"
)
assert SPEC is not None and SPEC.loader is not None
BUNDLE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BUNDLE
SPEC.loader.exec_module(BUNDLE)


def _distributions(tmp_path: Path) -> tuple[Path, Path, Path]:
    directory = tmp_path / "release"
    directory.mkdir()
    wheel = directory / "modal_computer_use-1.2.3-py3-none-any.whl"
    sdist = directory / "modal_computer_use-1.2.3.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    return directory, wheel, sdist


def _line(path: Path) -> str:
    return f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"


def test_verifies_exact_distribution_pair_and_stable_checksum_order(tmp_path: Path) -> None:
    directory, wheel, sdist = _distributions(tmp_path)
    (directory / ".gitignore").write_text("*\n", encoding="utf-8")
    checksums = tmp_path / "SHA256SUMS"
    checksums.write_text(_line(wheel) + _line(sdist), encoding="utf-8")

    BUNDLE.verify_release_bundle(distributions=directory, checksums=checksums)


@pytest.mark.parametrize(
    "content",
    [
        "bad-digest  modal_computer_use-1.2.3.tar.gz\n",
        f"{'0' * 64}  ../modal_computer_use-1.2.3.tar.gz\n",
        f"{'0' * 64}  ..\\modal_computer_use-1.2.3.tar.gz\n",
        f"{'0' * 64}  duplicate.whl\n{'1' * 64}  duplicate.whl\n",
    ],
)
def test_rejects_malformed_checksum_entries(tmp_path: Path, content: str) -> None:
    directory, _, _ = _distributions(tmp_path)
    checksums = tmp_path / "SHA256SUMS"
    checksums.write_text(content, encoding="utf-8")

    with pytest.raises(BUNDLE.ReleaseBundleError):
        BUNDLE.verify_release_bundle(distributions=directory, checksums=checksums)


def test_rejects_digest_mutation_and_unexpected_distribution_file(tmp_path: Path) -> None:
    directory, wheel, sdist = _distributions(tmp_path)
    checksums = tmp_path / "SHA256SUMS"
    checksums.write_text(_line(wheel) + _line(sdist), encoding="utf-8")
    wheel.write_bytes(b"changed")

    with pytest.raises(BUNDLE.ReleaseBundleError, match="SHA-256 mismatch"):
        BUNDLE.verify_release_bundle(distributions=directory, checksums=checksums)

    (directory / "notes.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(BUNDLE.ReleaseBundleError, match="unexpected files"):
        BUNDLE.distribution_paths(directory)
