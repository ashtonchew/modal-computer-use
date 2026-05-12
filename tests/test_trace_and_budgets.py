from __future__ import annotations

from fastapi.testclient import TestClient

from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.tracing import load_trace


def test_action_trace_redacts_typed_text(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            trace_dir=tmp_path / "traces",
            trace_actions=True,
            local_token="dev",
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={"actions": [{"type": "type", "text": "secret value"}]},
        )

    assert response.status_code == 200
    entries = load_trace(tmp_path / "traces" / "actions.ndjson")
    assert len(entries) == 1
    assert entries[0].normalized_action is not None
    assert entries[0].normalized_action["text"] == {"redacted": True, "length": 12}
    assert entries[0].redactions == ["text"]


def test_action_trace_records_artifact_screenshot_after_uri(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            trace_dir=tmp_path / "traces",
            trace_actions=True,
            local_token="dev",
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [{"type": "move", "x": 10, "y": 20}],
                "screenshot_after": True,
                "screenshot_options": {"storage": "artifact"},
            },
        )

    assert response.status_code == 200
    entries = load_trace(tmp_path / "traces" / "actions.ndjson")
    assert len(entries) == 2
    assert entries[1].normalized_action == {"type": "screenshot_after"}
    assert entries[1].screenshot_after_uri is not None
    assert entries[1].screenshot_after_uri.startswith("artifact://screenshots/")
    assert entries[1].coordinate_space is not None


def test_action_trace_records_screenshot_action_coordinate_space_and_uri(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            trace_dir=tmp_path / "traces",
            trace_actions=True,
            local_token="dev",
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [
                    {
                        "type": "zoom",
                        "region": {"x": 10, "y": 20, "width": 100, "height": 50},
                        "scale": 2,
                        "options": {"storage": "artifact"},
                    }
                ]
            },
        )

    assert response.status_code == 200
    entries = load_trace(tmp_path / "traces" / "actions.ndjson")
    assert len(entries) == 1
    assert entries[0].normalized_action is not None
    assert entries[0].normalized_action["type"] == "zoom"
    assert entries[0].screenshot_after_uri is not None
    assert entries[0].screenshot_after_uri.startswith("artifact://screenshots/")
    assert entries[0].coordinate_space is not None
    assert entries[0].coordinate_space.source_region is not None
    assert entries[0].coordinate_space.source_region.x == 10
    assert entries[0].coordinate_space.image_width == 200


def test_screenshot_budget_exceeded(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_screenshots=1,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        assert client.post("/v1/screenshots/full", json={}).status_code == 200
        response = client.post("/v1/screenshots/full", json={})

    assert response.status_code == 429
    assert response.json()["code"] == "budget_exceeded"


def test_artifact_byte_budget_exceeded_after_artifact_screenshot(tmp_path) -> None:
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
        response = client.post("/v1/screenshots/full", json={"storage": "artifact"})

    assert response.status_code == 429
    assert response.json()["code"] == "budget_exceeded"


def test_action_geometry_validation_rejects_out_of_bounds(test_client) -> None:
    response = test_client.post(
        "/v1/actions/run",
        json={"actions": [{"type": "move", "x": 1440, "y": 1}]},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "action_validation_failed"
