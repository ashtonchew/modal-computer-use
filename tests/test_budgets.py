from __future__ import annotations

from fastapi.testclient import TestClient

from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.settings import DaemonSettings


def test_status_includes_budget_snapshot(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_actions=10,
            max_screenshots=3,
            max_artifact_bytes=100_000,
            max_recording_seconds=60,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        client.post("/v1/actions/run", json={"actions": [{"type": "move", "x": 1, "y": 2}]})
        status = client.get("/v1/computer/status").json()

    assert status["budgets"]["actions"] == 1
    assert status["budgets"]["max_actions"] == 10
    assert status["budgets"]["screenshots"] == 0
    assert status["budgets"]["max_screenshots"] == 3
    assert status["budgets"]["artifact_bytes"] == 0
    assert status["budgets"]["max_artifact_bytes"] == 100_000
    assert status["budgets"]["max_recording_seconds"] == 60


def test_recording_stop_counts_recording_bytes_against_artifact_budget(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_artifact_bytes=1,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        started = client.post("/v1/recordings", json={}).json()
        response = client.post(f"/v1/recordings/{started['id']}/stop")

    assert response.status_code == 429
    assert response.json()["code"] == "budget_exceeded"
    assert response.json()["details"]["budgets"]["artifact_bytes"] > 1


def test_direct_artifact_write_enforces_artifact_byte_budget(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_artifact_bytes=1,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.put("/v1/artifacts/big.bin", content=b"xx")

    assert response.status_code == 429
    assert response.json()["code"] == "budget_exceeded"
    assert response.json()["details"]["budgets"]["artifact_bytes"] == 2
