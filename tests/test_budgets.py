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


def test_screenshot_artifact_write_is_rejected_before_file_persists(tmp_path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=artifacts_dir,
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_artifact_bytes=1,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post("/v1/screenshots/full", json={"storage": "artifact"})

    assert response.status_code == 429
    assert response.json()["code"] == "budget_exceeded"
    assert not list((artifacts_dir / "screenshots").glob("*.png"))
    assert not (artifacts_dir / "manifest.ndjson").exists()


def test_action_screenshot_artifact_budget_failure_uses_budget_code(tmp_path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=artifacts_dir,
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_artifact_bytes=1,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={"actions": [{"type": "screenshot", "options": {"storage": "artifact"}}]},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["results"][0]["error_code"] == "budget_exceeded"
    assert body["results"][0]["output"]["code"] == "budget_exceeded"
    assert not list((artifacts_dir / "screenshots").glob("*.png"))
    assert not (artifacts_dir / "manifest.ndjson").exists()


def test_screenshot_after_artifact_budget_failure_uses_budget_code(tmp_path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=artifacts_dir,
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_artifact_bytes=1,
            post_action_delay_ms=0,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [{"type": "move", "x": 1, "y": 2}],
                "screenshot_after": True,
                "screenshot_options": {"storage": "artifact"},
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["results"][-1]["type"] == "screenshot_after"
    assert body["results"][-1]["error_code"] == "budget_exceeded"
    assert body["results"][-1]["output"]["code"] == "budget_exceeded"
    assert not list((artifacts_dir / "screenshots").glob("*.png"))
