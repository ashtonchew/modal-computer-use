from __future__ import annotations

import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from modal_computer_use.artifacts import ArtifactStore, normalize_artifact_path
from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.daemon.supervisor import Supervisor
from modal_computer_use.errors import ArtifactPathError, BudgetExceededError


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/x",
        "../x",
        "a/%2e%2e/x",
        "manifest.ndjson",
        "a/\x00b",
        "a/\x7fb",
        "a/%7Fb",
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


def test_artifact_manifest_prefix_matches_path_boundary(tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    store.write_bytes("foo/a.txt", b"foo")
    store.write_bytes("foobar/b.txt", b"foobar")

    assert [item.path for item in store.manifest("foo")] == ["foo/a.txt"]


def test_artifact_manifest_skips_corrupt_and_unsafe_entries(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "root")
    safe = store.write_bytes("safe/ok.txt", b"ok")
    unsafe = {
        "path": "../secret.txt",
        "uri": "artifact://../secret.txt",
        "kind": "file",
    }
    control = {
        "path": "logs/xvfb.log",
        "uri": "artifact://logs/xvfb.log",
        "kind": "file",
    }
    mismatch = {
        "path": "safe/mismatch.txt",
        "uri": "artifact://safe/other.txt",
        "kind": "file",
    }
    store.manifest_path.write_text(
        "\n".join(
            [
                json.dumps(unsafe),
                "{not-json",
                safe.model_dump_json(),
                json.dumps(control),
                json.dumps(mismatch),
            ]
        ),
        encoding="utf-8",
    )

    assert [item.path for item in store.manifest()] == ["safe/ok.txt"]


def test_artifact_byte_budget_ignores_control_manifest_bytes(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "root", max_total_bytes=3)

    store.write_bytes("a.txt", b"x")
    store.write_bytes("b.txt", b"y")
    store.write_bytes("c.txt", b"z")

    assert sum((item.size_bytes or 0) for item in store.list()) == 3


def test_artifact_symlink_escape(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "root")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (tmp_path / "root" / "link").symlink_to(outside)
    with pytest.raises(ArtifactPathError):
        store.resolve("link")


def test_artifact_symlink_alias_to_control_path_is_rejected(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "root")
    store.manifest_path.write_text("secret manifest")
    (tmp_path / "root" / "public").symlink_to(store.manifest_path)

    with pytest.raises(ArtifactPathError):
        store.read_bytes("public")

    with pytest.raises(ArtifactPathError):
        store.write_bytes("public", b"overwrite")

    assert store.manifest_path.read_text() == "secret manifest"


def test_artifact_symlink_parent_alias_to_control_path_is_rejected(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "root")
    trace_dir = tmp_path / "root" / "traces"
    trace_dir.mkdir()
    (trace_dir / "actions.ndjson").write_text("secret trace")
    (tmp_path / "root" / "alias").symlink_to(trace_dir)

    with pytest.raises(ArtifactPathError):
        store.read_bytes("alias/actions.ndjson")


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
        runtime_dir=tmp_path / "runtime",
        local_token="dev",
        vnc_mode="control",
        vnc_password="secret-pass",
    )
    app = create_app(settings)
    password_file = Supervisor(settings)._vnc_password_file()
    assert password_file.read_text() == "secret-pass"
    assert not password_file.is_relative_to(settings.artifacts_dir)

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


def test_artifact_route_rejects_raw_supervisor_logs(tmp_path) -> None:
    settings = DaemonSettings(
        backend="mock",
        artifacts_dir=tmp_path / "artifacts",
        recordings_dir=tmp_path / "recordings",
        local_token="dev",
    )
    app = create_app(settings)
    log_path = settings.artifacts_dir / "logs" / "xvfb.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("Bearer raw-log-secret\n", encoding="utf-8")

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        process_log = client.get("/v1/processes/xvfb/logs")
        artifact_log = client.get("/v1/artifacts/logs/xvfb.log")

    assert process_log.status_code == 200
    assert process_log.text == "Bearer [redacted]"
    assert artifact_log.status_code == 400
    assert artifact_log.json()["code"] == "unsafe_artifact_path"
    assert "raw-log-secret" not in artifact_log.text


def test_artifact_routes_reject_active_recording_target(tmp_path) -> None:
    settings = DaemonSettings(
        backend="mock",
        artifacts_dir=tmp_path / "artifacts",
        recordings_dir=tmp_path / "recordings",
        local_token="dev",
    )
    app = create_app(settings)

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        recording = client.post("/v1/recordings", json={}).json()
        public_path = recording["artifact_uri"].removeprefix("artifact://")
        write = client.put(f"/v1/artifacts/{public_path}", content=b"replacement")
        delete = client.delete(f"/v1/artifacts/{public_path}")

    assert write.status_code == 409
    assert write.json()["code"] == "artifact_in_use"
    assert delete.status_code == 409
    assert delete.json()["code"] == "artifact_in_use"


def test_artifact_route_reports_conflict_when_target_is_directory(tmp_path) -> None:
    settings = DaemonSettings(
        backend="mock",
        artifacts_dir=tmp_path / "artifacts",
        recordings_dir=tmp_path / "recordings",
        local_token="dev",
    )
    app = create_app(settings)
    (settings.artifacts_dir / "reports").mkdir(parents=True)

    with TestClient(
        app,
        headers={"Authorization": "Bearer dev"},
        raise_server_exceptions=False,
    ) as client:
        response = client.put("/v1/artifacts/reports", content=b"replacement")

    assert response.status_code == 409
    assert response.json()["code"] == "artifact_path_conflict"
    assert not (settings.artifacts_dir / "manifest.ndjson").exists()


def test_artifact_route_reports_conflict_when_parent_is_file(tmp_path) -> None:
    settings = DaemonSettings(
        backend="mock",
        artifacts_dir=tmp_path / "artifacts",
        recordings_dir=tmp_path / "recordings",
        local_token="dev",
    )
    app = create_app(settings)
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    (settings.artifacts_dir / "reports").write_bytes(b"file")

    with TestClient(
        app,
        headers={"Authorization": "Bearer dev"},
        raise_server_exceptions=False,
    ) as client:
        response = client.put("/v1/artifacts/reports/result.txt", content=b"replacement")

    assert response.status_code == 409
    assert response.json()["code"] == "artifact_path_conflict"


def test_artifact_upload_rechecks_quota_before_atomic_replace(tmp_path, monkeypatch) -> None:
    settings = DaemonSettings(
        backend="mock",
        artifacts_dir=tmp_path / "artifacts",
        recordings_dir=tmp_path / "recordings",
        local_token="dev",
    )
    app = create_app(settings)
    app.state.artifacts.write_bytes("report.txt", b"original")

    def reject_commit(_target, _incoming_size) -> None:
        raise BudgetExceededError("artifact byte budget exceeded")

    monkeypatch.setattr(app.state.artifacts, "_enforce_write_budget", reject_commit)
    with TestClient(
        app,
        headers={"Authorization": "Bearer dev"},
        raise_server_exceptions=False,
    ) as client:
        response = client.put("/v1/artifacts/report.txt", content=b"replacement")

    assert response.status_code == 429
    assert app.state.artifacts.read_bytes("report.txt") == b"original"


def test_artifact_upload_restores_target_and_manifest_on_commit_failure(
    tmp_path, monkeypatch
) -> None:
    settings = DaemonSettings(
        backend="mock",
        artifacts_dir=tmp_path / "artifacts",
        recordings_dir=tmp_path / "recordings",
        local_token="dev",
    )
    app = create_app(settings)
    app.state.artifacts.write_bytes("report.txt", b"original")
    manifest_before = app.state.artifacts.manifest_path.read_bytes()

    def fail_manifest(_info) -> None:
        with app.state.artifacts.manifest_path.open("ab") as handle:
            handle.write(b"partial")
        raise OSError("manifest write failed")

    monkeypatch.setattr(app.state.artifacts, "append_manifest", fail_manifest)
    with TestClient(
        app,
        headers={"Authorization": "Bearer dev"},
        raise_server_exceptions=False,
    ) as client:
        response = client.put("/v1/artifacts/report.txt", content=b"replacement")

    assert response.status_code == 500
    assert app.state.artifacts.read_bytes("report.txt") == b"original"
    assert app.state.artifacts.manifest_path.read_bytes() == manifest_before


def test_artifact_upload_reuses_streaming_digest_without_second_read(
    tmp_path, monkeypatch
) -> None:
    settings = DaemonSettings(
        backend="mock",
        artifacts_dir=tmp_path / "artifacts",
        recordings_dir=tmp_path / "recordings",
        local_token="dev",
    )
    app = create_app(settings)
    original_info = app.state.artifacts._info
    calls: list[tuple[int | None, str | None]] = []

    def tracked_info(path, **kwargs):
        calls.append((kwargs.get("known_size_bytes"), kwargs.get("known_sha256")))
        return original_info(path, **kwargs)

    monkeypatch.setattr(app.state.artifacts, "_info", tracked_info)
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.put("/v1/artifacts/report.txt", content=b"content")

    assert response.status_code == 200
    assert response.json()["size_bytes"] == 7
    assert calls == [(7, response.json()["sha256"])]


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
    assert result.synced_paths == ["artifact-root"]
    assert (tmp_path / "root").as_posix() not in result.model_dump_json()
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
