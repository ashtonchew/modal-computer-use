from __future__ import annotations

import subprocess

import pytest

from modal_computer_use.artifacts import ArtifactStore, normalize_artifact_path
from modal_computer_use.errors import ArtifactPathError


@pytest.mark.parametrize("path", ["/tmp/x", "../x", "a/%2e%2e/x", "manifest.ndjson", "a/\x00b"])
def test_artifact_path_rejects_unsafe(path: str) -> None:
    with pytest.raises(ArtifactPathError):
        normalize_artifact_path(path)


def test_artifact_store_roundtrip(tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    info = store.write_bytes("downloads/a.txt", b"hello", content_type="text/plain")
    assert info.uri == "artifact://downloads/a.txt"
    assert store.read_bytes("downloads/a.txt") == b"hello"
    assert store.manifest()[0].path == "downloads/a.txt"


def test_artifact_symlink_escape(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "root")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (tmp_path / "root" / "link").symlink_to(outside)
    with pytest.raises(ArtifactPathError):
        store.resolve("link")


def test_artifact_sync_reports_noop_without_persistence(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "root")

    result = store.sync()

    assert result.ok is True
    assert result.persistent is False
    assert result.synced_paths == []
    assert "no-op" in (result.message or "")


def test_artifact_sync_runs_mountpoint_sync_for_persistent_volume(tmp_path) -> None:
    calls: list[str] = []

    def runner(path: str) -> subprocess.CompletedProcess[str]:
        calls.append(path)
        return subprocess.CompletedProcess(["sync", path], 0, "", "")

    store = ArtifactStore(
        tmp_path / "root",
        persistent=True,
        sync_runner=runner,
    )

    result = store.sync()

    assert result.ok is True
    assert result.persistent is True
    assert result.synced_paths == [(tmp_path / "root").as_posix()]
    assert calls == [(tmp_path / "root").as_posix()]


def test_artifact_sync_reports_mountpoint_sync_failure(tmp_path) -> None:
    def runner(path: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["sync", path], 1, "", "failed")

    store = ArtifactStore(
        tmp_path / "root",
        persistent=True,
        sync_runner=runner,
    )

    result = store.sync()

    assert result.ok is False
    assert result.persistent is True
    assert result.synced_paths == []
    assert "failed" in (result.message or "")
