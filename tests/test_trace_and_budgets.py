from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from modal_computer_use.adapters.provenance import (
    PROVIDER_ACTION_METADATA_KEY,
    PROVIDER_ACTION_REDACTIONS_METADATA_KEY,
)
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


def test_action_trace_promotes_redacted_provider_action_from_metadata(tmp_path) -> None:
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
                        "type": "type",
                        "text": "secret value",
                        "metadata": {
                            "policy": "fixture",
                            PROVIDER_ACTION_METADATA_KEY: {
                                "type": "type",
                                "text": {"redacted": True, "length": 12},
                            },
                            PROVIDER_ACTION_REDACTIONS_METADATA_KEY: ["text"],
                        },
                    }
                ],
                "source": "openai-adapter",
            },
        )

    assert response.status_code == 200
    entries = load_trace(tmp_path / "traces" / "actions.ndjson")
    assert len(entries) == 1
    assert entries[0].provider_action == {
        "type": "type",
        "text": {"redacted": True, "length": 12},
    }
    assert entries[0].normalized_action is not None
    assert entries[0].normalized_action["metadata"] == {"policy": "fixture"}
    assert entries[0].normalized_action["text"] == {"redacted": True, "length": 12}
    assert entries[0].redactions == ["text", "provider_action.text"]


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


def test_action_trace_error_includes_code(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            trace_dir=tmp_path / "traces",
            trace_actions=True,
            local_token="dev",
            default_action_timeout_ms=10,
        )
    )

    async def slow_move(x: int, y: int):
        await asyncio.sleep(1)

    app.state.backend.mouse_move = slow_move
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={"actions": [{"type": "move", "x": 10, "y": 20}]},
        )

    assert response.status_code == 200
    entries = load_trace(tmp_path / "traces" / "actions.ndjson")
    assert entries[0].error is not None
    assert entries[0].error["code"] == "timeout"
    assert "timed out" in entries[0].error["message"]


def test_failed_action_counts_against_action_budget_and_traces_error(tmp_path) -> None:
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

    async def fail_move(x: int, y: int):
        raise RuntimeError("synthetic move failure")

    app.state.backend.mouse_move = fail_move
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={"actions": [{"type": "move", "x": 10, "y": 20}]},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["results"][0]["error_code"] == "action_failed"
    assert app.state.action_count == 1
    entries = load_trace(tmp_path / "traces" / "actions.ndjson")
    assert entries[0].error == {
        "code": "action_failed",
        "message": "synthetic move failure",
    }


def test_action_budget_exceeded_does_not_execute_action(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            trace_dir=tmp_path / "traces",
            trace_actions=True,
            local_token="dev",
            max_actions=0,
        )
    )
    calls = 0

    async def move(x: int, y: int):
        nonlocal calls
        calls += 1

    app.state.backend.mouse_move = move
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={"actions": [{"type": "move", "x": 10, "y": 20}]},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["results"][0]["error_code"] == "budget_exceeded"
    assert body["results"][0]["output"]["code"] == "budget_exceeded"
    assert body["results"][0]["output"]["budgets"]["actions"] == 0
    assert calls == 0
    assert app.state.action_count == 0
    entries = load_trace(tmp_path / "traces" / "actions.ndjson")
    assert entries[0].error == {
        "code": "budget_exceeded",
        "message": "action budget exceeded",
    }


def test_screenshot_action_budget_failure_is_single_traced_result(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            trace_dir=tmp_path / "traces",
            trace_actions=True,
            local_token="dev",
            max_screenshots=0,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [
                    {"type": "screenshot", "options": {"storage": "artifact"}},
                ]
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert len(body["results"]) == 1
    assert body["results"][0]["type"] == "screenshot"
    assert body["results"][0]["error_code"] == "budget_exceeded"
    assert body["results"][0]["output"]["budgets"]["screenshots"] == 1
    assert app.state.action_count == 0
    assert app.state.screenshot_count == 1
    entries = load_trace(tmp_path / "traces" / "actions.ndjson")
    assert len(entries) == 1
    assert entries[0].error is not None
    assert entries[0].error["code"] == "budget_exceeded"


def test_screenshot_after_budget_failure_records_trace_shape(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            trace_dir=tmp_path / "traces",
            trace_actions=True,
            local_token="dev",
            max_screenshots=0,
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
    body = response.json()
    assert body["ok"] is False
    assert body["screenshot"] is None
    assert [item["type"] for item in body["results"]] == ["move", "screenshot_after"]
    assert body["results"][1]["error_code"] == "budget_exceeded"
    assert body["results"][1]["output"]["budgets"]["screenshots"] == 1
    assert app.state.action_count == 1
    assert app.state.screenshot_count == 1
    entries = load_trace(tmp_path / "traces" / "actions.ndjson")
    assert len(entries) == 2
    assert entries[1].normalized_action == {"type": "screenshot_after"}
    assert entries[1].screenshot_after_uri is not None
    assert entries[1].coordinate_space is not None
    assert entries[1].error is not None
    assert entries[1].error["code"] == "budget_exceeded"


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
