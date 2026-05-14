from __future__ import annotations

import asyncio
import hashlib

from fastapi.testclient import TestClient

from modal_computer_use.adapters.provenance import (
    PROVIDER_ACTION_METADATA_KEY,
    PROVIDER_ACTION_REDACTIONS_METADATA_KEY,
)
from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.errors import DaemonError
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
    assert entries[0].normalized_action["text"] == {
        "redacted": True,
        "length": 12,
        "sha256": hashlib.sha256(b"secret value").hexdigest(),
    }
    assert entries[0].redactions == ["text"]


def test_action_trace_redacts_nested_hold_typed_text(tmp_path) -> None:
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
                        "type": "hold_key",
                        "key": "shift",
                        "actions": [{"type": "type", "text": "nested secret"}],
                    }
                ]
            },
        )

    assert response.status_code == 200
    raw_trace = (tmp_path / "traces" / "actions.ndjson").read_text()
    assert "nested secret" not in raw_trace
    entries = load_trace(tmp_path / "traces" / "actions.ndjson")
    assert entries[0].normalized_action is not None
    assert entries[0].normalized_action["actions"][0]["text"] == {
        "redacted": True,
        "length": 13,
        "sha256": hashlib.sha256(b"nested secret").hexdigest(),
    }
    assert entries[0].redactions == ["actions[0].text"]


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
    assert entries[0].normalized_action["text"] == {
        "redacted": True,
        "length": 12,
        "sha256": hashlib.sha256(b"secret value").hexdigest(),
    }
    assert entries[0].redactions == ["text", "provider_action.text"]


def test_action_logs_redact_raw_provider_action_metadata(tmp_path, caplog) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )

    with (
        caplog.at_level("INFO", logger="modal_computer_use.daemon.actions"),
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
    ):
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [
                    {
                        "type": "move",
                        "x": 10,
                        "y": 20,
                        "metadata": {
                            PROVIDER_ACTION_METADATA_KEY: {
                                "type": "type",
                                "text": "raw-provider-secret",
                            }
                        },
                    }
                ]
            },
        )

    assert response.status_code == 200
    serialized_logs = "\n".join(str(record.__dict__) for record in caplog.records)
    assert "raw-provider-secret" not in serialized_logs


def test_action_trace_redacts_sensitive_metadata(tmp_path) -> None:
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
                        "type": "move",
                        "x": 10,
                        "y": 20,
                        "metadata": {
                            "authorization": "Bearer metadata-secret",
                            "url": "https://novnc.example/?token=metadata-secret",
                            "note": "Bearer note-secret artifact://screenshots/private.png",
                        },
                    }
                ]
            },
        )

    assert response.status_code == 200
    raw_trace = (tmp_path / "traces" / "actions.ndjson").read_text()
    assert "metadata-secret" not in raw_trace
    assert "note-secret" not in raw_trace
    assert "artifact://screenshots/private.png" not in raw_trace
    entries = load_trace(tmp_path / "traces" / "actions.ndjson")
    assert entries[0].normalized_action is not None
    metadata = entries[0].normalized_action["metadata"]
    assert metadata["authorization"]["redacted"] is True
    assert metadata["url"]["redacted"] is True
    assert metadata["note"] == "Bearer [redacted] [redacted]"
    assert entries[0].redactions == [
        "metadata.authorization",
        "metadata.url",
        "metadata.note",
    ]


def test_action_trace_keeps_artifact_screenshot_after_uri_out_of_top_level(tmp_path) -> None:
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
    assert entries[1].screenshot_after_uri is None
    assert entries[1].coordinate_space is not None


def test_action_trace_does_not_write_raw_artifact_uris(tmp_path) -> None:
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
    trace_payload = (tmp_path / "traces" / "actions.ndjson").read_text(encoding="utf-8")
    assert "artifact://screenshots/" not in trace_payload


def test_action_trace_records_screenshot_action_coordinate_space_without_raw_uri(tmp_path) -> None:
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
    assert entries[0].screenshot_after_uri is None
    assert entries[0].coordinate_space is not None
    assert entries[0].coordinate_space.source_region is not None
    assert entries[0].coordinate_space.source_region.x == 10
    assert entries[0].coordinate_space.image_width == 200


def test_action_trace_redacts_inline_screenshot_payload(tmp_path) -> None:
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
            json={"actions": [{"type": "screenshot"}]},
        )

    assert response.status_code == 200
    raw_trace = (tmp_path / "traces" / "actions.ndjson").read_text()
    assert '"data_base64":' not in raw_trace
    assert '"bytes":' not in raw_trace
    entries = load_trace(tmp_path / "traces" / "actions.ndjson")
    assert entries[0].result is not None
    output = entries[0].result["output"]
    assert output["sha256"]
    assert output["size_bytes"] > 0


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


def test_failed_action_sanitizes_secret_bearing_exception_text_in_response_and_trace(
    tmp_path,
) -> None:
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
        raise RuntimeError("Bearer supersecret artifact://screenshots/private.png")

    app.state.backend.mouse_move = fail_move
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={"actions": [{"type": "move", "x": 10, "y": 20}]},
        )

    assert response.status_code == 200
    serialized = response.text
    assert "supersecret" not in serialized
    assert "artifact://screenshots/private.png" not in serialized
    entries = load_trace(tmp_path / "traces" / "actions.ndjson")
    trace_payload = entries[0].model_dump_json()
    assert "supersecret" not in trace_payload
    assert "artifact://screenshots/private.png" not in trace_payload


def test_screenshot_after_sanitizes_secret_bearing_exception_text(tmp_path) -> None:
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

    async def fail_screenshot(*_args, **_kwargs):
        raise RuntimeError("Bearer shot-secret artifact://screenshots/private.png")

    app.state.backend.screenshot = fail_screenshot
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [{"type": "move", "x": 10, "y": 20}],
                "screenshot_after": True,
            },
        )

    assert response.status_code == 200
    serialized = response.text
    assert "shot-secret" not in serialized
    assert "artifact://screenshots/private.png" not in serialized
    entries = load_trace(tmp_path / "traces" / "actions.ndjson")
    trace_payload = entries[1].model_dump_json()
    assert "shot-secret" not in trace_payload
    assert "artifact://screenshots/private.png" not in trace_payload


def test_screenshot_after_trace_redacts_secret_bearing_exception_details(tmp_path) -> None:
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

    async def fail_screenshot(*_args, **_kwargs):
        raise DaemonError(
            "screenshot failed",
            details={
                "stdout": "Bearer stdout-secret",
                "stderr": "stderr-secret",
                "artifact_uri": "artifact://screenshots/private.png",
            },
        )

    app.state.backend.screenshot = fail_screenshot
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [{"type": "move", "x": 10, "y": 20}],
                "screenshot_after": True,
            },
        )

    assert response.status_code == 200
    entries = load_trace(tmp_path / "traces" / "actions.ndjson")
    trace_payload = entries[1].model_dump_json()
    assert "stdout-secret" not in trace_payload
    assert "stderr-secret" not in trace_payload
    assert "artifact://screenshots/private.png" not in trace_payload
    assert entries[1].result is not None
    output = entries[1].result["output"]
    assert output["stdout"]["redacted"] is True
    assert output["stderr"]["redacted"] is True
    assert output["artifact_uri"]["redacted"] is True


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


def test_action_budget_failure_suppresses_screenshot_after(tmp_path) -> None:
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
    assert body["screenshot"] is None
    assert [item["type"] for item in body["results"]] == ["move"]
    assert app.state.screenshot_count == 0
    assert not list((tmp_path / "artifacts" / "screenshots").glob("*.png"))
    entries = load_trace(tmp_path / "traces" / "actions.ndjson")
    assert len(entries) == 1


def test_action_rate_limit_stops_batch_without_executing_extra_actions(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            trace_dir=tmp_path / "traces",
            trace_actions=True,
            local_token="dev",
            input_rate_limit_per_sec=1,
        )
    )
    calls = 0
    original = app.state.backend.mouse_move

    async def move(x: int, y: int):
        nonlocal calls
        calls += 1
        return await original(x, y)

    app.state.backend.mouse_move = move
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [
                    {"type": "move", "x": 10, "y": 20},
                    {"type": "move", "x": 30, "y": 40},
                ]
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert [item["error_code"] for item in body["results"]] == [None, "rate_limited"]
    assert body["results"][1]["output"]["rate_limit_per_sec"] == 1
    assert calls == 1
    assert app.state.action_count == 1
    entries = load_trace(tmp_path / "traces" / "actions.ndjson")
    assert entries[1].error == {
        "code": "rate_limited",
        "message": "action rate limit exceeded",
    }


def test_action_rate_limit_applies_to_direct_mouse_routes(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            input_rate_limit_per_sec=1,
        )
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        first = client.post("/v1/mouse/move", json={"x": 10, "y": 20})
        second = client.post("/v1/mouse/move", json={"x": 30, "y": 40})

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["code"] == "rate_limited"
    assert "token" not in str(second.json()).lower()


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
    assert body["results"][0]["output"]["budgets"]["screenshots"] == 0
    assert app.state.action_count == 0
    assert app.state.screenshot_count == 0
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
    assert body["results"][1]["output"]["budgets"]["screenshots"] == 0
    assert app.state.action_count == 1
    assert app.state.screenshot_count == 0
    entries = load_trace(tmp_path / "traces" / "actions.ndjson")
    assert len(entries) == 2
    assert entries[1].normalized_action == {"type": "screenshot_after"}
    assert entries[1].screenshot_after_uri is None
    assert entries[1].coordinate_space is None
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


def test_direct_screenshot_failure_does_not_increment_budget(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )

    async def fail_screenshot(*args, **kwargs):
        raise RuntimeError("synthetic screenshot failure")

    app.state.backend.screenshot = fail_screenshot
    with TestClient(
        app,
        headers={"Authorization": "Bearer dev"},
        raise_server_exceptions=False,
    ) as client:
        response = client.post("/v1/screenshots/full", json={})

    assert response.status_code == 500
    assert app.state.screenshot_count == 0


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
