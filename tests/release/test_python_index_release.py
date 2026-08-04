from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "verify_python_index_release", ROOT / "scripts" / "verify_python_index_release.py"
)
assert SPEC is not None and SPEC.loader is not None
INDEX = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = INDEX
SPEC.loader.exec_module(INDEX)


def _distributions(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    directory = tmp_path / "release"
    directory.mkdir()
    wheel = directory / "modal_computer_use-1.2.3-py3-none-any.whl"
    sdist = directory / "modal_computer_use-1.2.3.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    expected = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (wheel, sdist)
    }
    return directory, expected


def _fetcher(expected: dict[str, str], *, provenance: bool = True):
    def fetch(url: str) -> dict[str, Any]:
        if "/pypi/" in url:
            return {
                "urls": [
                    {"filename": filename, "digests": {"sha256": digest}}
                    for filename, digest in expected.items()
                ]
            }
        return {"attestation_bundles": [{}] if provenance else []}

    return fetch


def test_accepts_exact_files_hashes_and_provenance(tmp_path: Path) -> None:
    directory, expected = _distributions(tmp_path)

    INDEX.verify_index_release_once(
        index_url="https://test.pypi.org",
        project="modal-computer-use",
        version="1.2.3",
        distributions=directory,
        fetch_json=_fetcher(expected),
    )


def test_rejects_wrong_hash_extra_file_and_missing_provenance(tmp_path: Path) -> None:
    directory, expected = _distributions(tmp_path)
    wrong = dict(expected)
    wrong[next(iter(wrong))] = "0" * 64
    with pytest.raises(INDEX.IndexReleaseError, match="published files differ"):
        INDEX.verify_index_release_once(
            index_url="https://pypi.org",
            project="modal-computer-use",
            version="1.2.3",
            distributions=directory,
            fetch_json=_fetcher(wrong),
        )

    extra = {**expected, "modal_computer_use-1.2.3.zip": "1" * 64}
    with pytest.raises(INDEX.IndexReleaseError, match="published files differ"):
        INDEX.verify_index_release_once(
            index_url="https://pypi.org",
            project="modal-computer-use",
            version="1.2.3",
            distributions=directory,
            fetch_json=_fetcher(extra),
        )

    with pytest.raises(INDEX.IndexReleaseError, match="has no provenance"):
        INDEX.verify_index_release_once(
            index_url="https://pypi.org",
            project="modal-computer-use",
            version="1.2.3",
            distributions=directory,
            fetch_json=_fetcher(expected, provenance=False),
        )


def test_retries_bounded_index_propagation(tmp_path: Path) -> None:
    directory, expected = _distributions(tmp_path)
    good = _fetcher(expected)
    calls = 0
    sleeps: list[float] = []

    def fetch(url: str) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"urls": []}
        return good(url)

    INDEX.verify_index_release(
        index_url="https://pypi.org",
        project="modal-computer-use",
        version="1.2.3",
        distributions=directory,
        attempts=2,
        delay_seconds=0.25,
        fetch_json=fetch,
        sleep=sleeps.append,
    )

    assert sleeps == [0.25]


def test_rejects_non_https_index_origin(tmp_path: Path) -> None:
    directory, expected = _distributions(tmp_path)

    with pytest.raises(INDEX.IndexReleaseError, match="HTTPS"):
        INDEX.verify_index_release_once(
            index_url="http://pypi.example",
            project="modal-computer-use",
            version="1.2.3",
            distributions=directory,
            fetch_json=_fetcher(expected),
        )


def test_rejects_invalid_retry_bounds(tmp_path: Path) -> None:
    directory, expected = _distributions(tmp_path)

    with pytest.raises(INDEX.IndexReleaseError, match="attempts must"):
        INDEX.verify_index_release(
            index_url="https://pypi.org",
            project="modal-computer-use",
            version="1.2.3",
            distributions=directory,
            attempts=0,
            delay_seconds=0,
            fetch_json=_fetcher(expected),
        )
    with pytest.raises(INDEX.IndexReleaseError, match="cannot be negative"):
        INDEX.verify_index_release(
            index_url="https://pypi.org",
            project="modal-computer-use",
            version="1.2.3",
            distributions=directory,
            attempts=1,
            delay_seconds=-1,
            fetch_json=_fetcher(expected),
        )
