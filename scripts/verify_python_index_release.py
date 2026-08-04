from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any


class IndexReleaseError(ValueError):
    """Raised when an index does not expose the expected release bytes."""


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 - caller restricts the base URL to HTTPS.
        url,
        headers={"Accept": "application/json", "User-Agent": "modal-computer-use-release-check"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        document = json.load(response)
    if not isinstance(document, dict):
        raise IndexReleaseError(f"{url}: expected a JSON object")
    return document


def _expected_distributions(directory: Path) -> dict[str, str]:
    if not directory.is_dir():
        raise IndexReleaseError(f"distribution directory does not exist: {directory}")
    paths = sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and (path.suffix == ".whl" or path.name.endswith(".tar.gz"))
    )
    wheels = [path for path in paths if path.suffix == ".whl"]
    sdists = [path for path in paths if path.name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1:
        raise IndexReleaseError(
            f"expected exactly one wheel and one sdist; found wheel={len(wheels)}, "
            f"sdist={len(sdists)}"
        )
    return {path.name: _digest(path) for path in paths}


def verify_index_release_once(
    *,
    index_url: str,
    project: str,
    version: str,
    distributions: Path,
    fetch_json: Callable[[str], dict[str, Any]] = _get_json,
) -> None:
    parsed = urllib.parse.urlparse(index_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise IndexReleaseError("index URL must be an HTTPS origin or path without query data")
    base = index_url.rstrip("/")
    project_path = urllib.parse.quote(project, safe="")
    version_path = urllib.parse.quote(version, safe="")
    release = fetch_json(f"{base}/pypi/{project_path}/{version_path}/json")
    files = release.get("urls")
    if not isinstance(files, list):
        raise IndexReleaseError("release response is missing its file list")

    expected = _expected_distributions(distributions)
    published: dict[str, str] = {}
    for item in files:
        if not isinstance(item, dict):
            raise IndexReleaseError("release response contains a malformed file")
        filename = item.get("filename")
        digests = item.get("digests")
        if not isinstance(filename, str) or not isinstance(digests, dict):
            raise IndexReleaseError("release response contains incomplete file metadata")
        sha256 = digests.get("sha256")
        if not isinstance(sha256, str):
            raise IndexReleaseError(f"published file {filename!r} has no SHA-256 digest")
        if filename in published:
            raise IndexReleaseError(f"release response repeats {filename!r}")
        published[filename] = sha256

    if published != expected:
        raise IndexReleaseError(
            f"published files differ: expected {expected!r}, found {published!r}"
        )

    for filename in expected:
        filename_path = urllib.parse.quote(filename, safe="")
        provenance = fetch_json(
            f"{base}/integrity/{project_path}/{version_path}/{filename_path}/provenance"
        )
        bundles = provenance.get("attestation_bundles")
        if not isinstance(bundles, list) or not bundles:
            raise IndexReleaseError(f"published file {filename!r} has no provenance")


def verify_index_release(
    *,
    index_url: str,
    project: str,
    version: str,
    distributions: Path,
    attempts: int,
    delay_seconds: float,
    fetch_json: Callable[[str], dict[str, Any]] = _get_json,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    if attempts < 1:
        raise IndexReleaseError("attempts must be at least one")
    if delay_seconds < 0:
        raise IndexReleaseError("delay seconds cannot be negative")
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            verify_index_release_once(
                index_url=index_url,
                project=project,
                version=version,
                distributions=distributions,
                fetch_json=fetch_json,
            )
            return
        except (IndexReleaseError, OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < attempts:
                sleep(delay_seconds)
    raise IndexReleaseError(f"index verification failed after {attempts} attempts: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify exact distribution hashes and provenance on a Python package index."
    )
    parser.add_argument("--index-url", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--distributions", required=True, type=Path)
    parser.add_argument("--attempts", type=int, default=12)
    parser.add_argument("--delay-seconds", type=float, default=5)
    args = parser.parse_args()
    try:
        verify_index_release(
            index_url=args.index_url,
            project=args.project,
            version=args.version,
            distributions=args.distributions,
            attempts=args.attempts,
            delay_seconds=args.delay_seconds,
        )
    except (IndexReleaseError, OSError) as exc:
        parser.error(str(exc))
    print(f"verified {args.project} {args.version} on {args.index_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
