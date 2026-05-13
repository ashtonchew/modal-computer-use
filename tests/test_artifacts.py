from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from modal_computer_use.artifacts import ArtifactStore, normalize_artifact_path
from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.daemon.supervisor import Supervisor
from modal_computer_use.errors import ArtifactPathError


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/x",
        "../x",
        "a/%2e%2e/x",
        "manifest.ndjson",
        "a/\x00b",
        ".Secrets/x",
        "a/.Control/x",
    ],
)
def test_artifact_path_rejects_unsafe(path: str) -> None:
    with pytest.raises(ArtifactPathError):
        normalize_artifact_path(path)


def test_artifact_path_rejects_secret_control_segment() -> None:
    with pytest.raises(ArtifactPathError):
        normalize_artifact_path(".secrets/x11vnc.pass")


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


def test_artifact_list_rejects_symlink_escape(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "root")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (tmp_path / "root" / "link").symlink_to(outside)

    with pytest.raises(ArtifactPathError):
        store.list()


def test_artifact_route_rejects_vnc_password_control_path(tmp_path) -> None:
    settings = DaemonSettings(
        backend="mock",
        artifacts_dir=tmp_path / "artifacts",
        recordings_dir=tmp_path / "recordings",
        local_token="dev",
        vnc_mode="control",
        vnc_password="secret-pass",
    )
    app = create_app(settings)
    Supervisor(settings)._vnc_password_file()

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.get("/v1/artifacts/.secrets/x11vnc.pass")

    assert response.status_code == 400
    assert response.json()["code"] == "unsafe_artifact_path"


def test_artifact_route_rejects_mixed_case_control_segment(tmp_path) -> None:
    settings = DaemonSettings(
        backend="mock",
        artifacts_dir=tmp_path / "artifacts",
        recordings_dir=tmp_path / "recordings",
        local_token="dev",
    )
    app = create_app(settings)

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.put("/v1/artifacts/.Secrets/x11vnc.pass", content=b"secret")

    assert response.status_code == 400
    assert response.json()["code"] == "unsafe_artifact_path"


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
        persistent_verified=True,
        sync_runner=runner,
    )

    result = store.sync()

    assert result.ok is True
    assert result.persistent is True
    assert result.synced_paths == [(tmp_path / "root").as_posix()]
    assert calls == [(tmp_path / "root").as_posix()]


def test_artifact_sync_fails_closed_when_persistence_is_unverified(tmp_path) -> None:
    calls: list[str] = []

    def runner(path: str) -> subprocess.CompletedProcess[str]:
        calls.append(path)
        return subprocess.CompletedProcess(["sync", path], 0, "", "")

    store = ArtifactStore(
        tmp_path / "root",
        persistent=True,
        persistent_verified=False,
        sync_runner=runner,
    )

    result = store.sync()

    assert result.ok is False
    assert result.persistent is True
    assert result.synced_paths == []
    assert "verified Modal Volume" in (result.message or "")
    assert calls == []


def test_artifact_sync_reports_mountpoint_sync_failure(tmp_path) -> None:
    def runner(path: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["sync", path], 1, "", "failed")

    store = ArtifactStore(
        tmp_path / "root",
        persistent=True,
        persistent_verified=True,
        sync_runner=runner,
    )

    result = store.sync()

    assert result.ok is False
    assert result.persistent is True
    assert result.synced_paths == []
    assert "failed" in (result.message or "")
