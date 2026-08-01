from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.hot_session import HotSessionClient
from modal_computer_use.models import ActionBatchResult
from modal_computer_use.transports.hot_session import _websocket_url


def _app(tmp_path, **overrides):
    return create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            **overrides,
        )
    )


def test_hot_session_websocket_url_preserves_base_path_and_query() -> None:
    assert _websocket_url(
        "https://connect.modal.run/abc123?workspace=ws&_modal_connect_token=secret",
        "/v1/session/hot",
    ) == "wss://connect.modal.run/abc123/v1/session/hot?workspace=ws&_modal_connect_token=secret"


def test_hot_session_rejects_missing_auth(tmp_path) -> None:
    app = _app(tmp_path, local_token="dev")

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/v1/session/hot"),
    ):
        pass

    assert exc.value.code == 1008


def test_hot_session_connection_limit_is_global(tmp_path) -> None:
    app = _app(tmp_path, local_token="dev", max_hot_session_connections=1)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/session/hot") as first,
    ):
        assert first.receive_json()["type"] == "ready"
        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect("/v1/session/hot"),
        ):
            pass

    assert exc_info.value.code == 1013


def test_hot_session_runs_actions_and_raw_screenshot(test_client) -> None:
    with test_client.websocket_connect("/v1/session/hot") as websocket:
        assert websocket.receive_json()["type"] == "ready"

        websocket.send_json(
            {
                "id": "1",
                "op": "run_actions",
                "payload": {"actions": [{"type": "move", "x": 10, "y": 20}]},
            }
        )
        action = websocket.receive_json()

        websocket.send_json(
            {
                "id": "2",
                "op": "run_raw_screenshot",
                "payload": {
                    "actions": [{"type": "click", "x": 10, "y": 20}],
                    "screenshot_after": True,
                    "screenshot_options": {"format": "png", "show_cursor": False},
                },
            }
        )
        header = websocket.receive_json()
        payload = websocket.receive_bytes()

    assert action["type"] == "result"
    assert action["ok"] is True
    assert action["result"]["ok"] is True
    assert header["type"] == "binary"
    assert header["content_type"] == "image/png"
    assert header["headers"]["x-computer-use-width"] == "1024"
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")


def test_hot_session_screenshot_raw_reserves_screenshot_budget(tmp_path) -> None:
    app = _app(tmp_path, local_token="dev", max_screenshots=0)

    with (
        TestClient(app, headers={"Authorization": "Bearer dev"}) as client,
        client.websocket_connect("/v1/session/hot") as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "1",
                "op": "screenshot_raw",
                "payload": {"format": "png", "show_cursor": False},
            }
        )
        response = websocket.receive_json()

    assert response["type"] == "error"
    assert response["error"]["code"] == "budget_exceeded"


def test_hot_session_client_marshals_actions_and_binary_results() -> None:
    transport = _FakeHotTransport()
    client = HotSessionClient(transport)  # type: ignore[arg-type]

    result = client.run_actions([{"type": "move", "x": 1, "y": 2}])
    shot = client.run_actions_with_raw_screenshot(
        [{"type": "click", "x": 1, "y": 2}],
        screenshot_options={"format": "png", "show_cursor": False},
    )

    assert isinstance(result, ActionBatchResult)
    assert result.ok is True
    assert shot.payload == b"png"
    assert transport.calls[0][0] == "run_actions"
    assert transport.calls[1][0] == "run_raw_screenshot"
    assert transport.calls[1][1]["screenshot_after"] is True


class _FakeHotTransport:
    def __init__(self) -> None:
        self.calls = []

    def request(self, op, payload):
        self.calls.append((op, payload))
        return {"ok": True, "results": [], "timing": {"daemon_ms": 1.0}}

    def request_binary(self, op, payload):
        from modal_computer_use.transports import HotSessionBinaryResult

        self.calls.append((op, payload))
        return HotSessionBinaryResult(
            payload=b"png",
            headers={"x-computer-use-width": "1"},
            result={"ok": True, "results": []},
            content_type="image/png",
        )
