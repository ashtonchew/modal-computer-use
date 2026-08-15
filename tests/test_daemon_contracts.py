from __future__ import annotations

import errno
import inspect
from pathlib import Path

import pytest

from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.models import ActionResult, ProcessStatus
from modal_computer_use.namespaces.actions import ActionsNamespace, AsyncActionsNamespace


def test_action_batch_uses_direct_route_readiness_authority(test_client, app) -> None:
    app.state.display_reconstruction_failed = True

    batch = test_client.post(
        "/v1/actions/run",
        json={"actions": [{"type": "move", "x": 1, "y": 2}]},
    )
    direct = test_client.post("/v1/mouse/move", json={"x": 1, "y": 2})

    assert batch.status_code == direct.status_code == 503
    assert batch.json()["code"] == direct.json()["code"] == "desktop_not_ready"
    assert batch.json()["details"] == direct.json()["details"]


@pytest.mark.parametrize("state_field", ["display_restart_in_progress", "supervisor_stopped"])
def test_action_batch_rejects_the_same_lifecycle_readiness_states(
    test_client,
    app,
    monkeypatch,
    state_field: str,
) -> None:
    if state_field == "display_restart_in_progress":
        app.state.display_restart_in_progress = True
    else:
        app.state.supervisor.running = False

    batch = test_client.post(
        "/v1/actions/run",
        json={"actions": [{"type": "move", "x": 1, "y": 2}]},
    )
    direct = test_client.post("/v1/mouse/move", json={"x": 1, "y": 2})

    assert batch.status_code == direct.status_code == 503
    assert batch.json()["details"] == direct.json()["details"]


def test_action_batch_checks_xvfb_status_like_direct_routes(test_client, app, monkeypatch) -> None:
    original_status = app.state.supervisor.status

    def status(name: str) -> ProcessStatus:
        if name == "xvfb":
            return ProcessStatus(name=name, status="failed")
        return original_status(name)

    monkeypatch.setattr(app.state.supervisor, "status", status)

    batch = test_client.post(
        "/v1/actions/run",
        json={"actions": [{"type": "move", "x": 1, "y": 2}]},
    )
    direct = test_client.post("/v1/mouse/move", json={"x": 1, "y": 2})

    assert batch.status_code == direct.status_code == 503
    assert batch.json()["details"] == direct.json()["details"]


def test_raw_screenshot_rejects_body_idempotency_before_dispatch(test_client, app) -> None:
    calls = 0
    original_move = app.state.backend.mouse_move

    async def move(x: int, y: int):
        nonlocal calls
        calls += 1
        return await original_move(x, y)

    app.state.backend.mouse_move = move
    payload = {
        "actions": [{"type": "move", "x": 1, "y": 2}],
        "screenshot_after": True,
    }
    payload["idempotency_key"] = "raw-key"

    response = test_client.post("/v1/actions/run/raw-screenshot", json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "raw_screenshot_idempotency_not_supported"
    assert calls == 0


def test_raw_screenshot_header_idempotency_is_rejected_before_dispatch(test_client, app) -> None:
    calls = 0
    original_move = app.state.backend.mouse_move

    async def move(x: int, y: int):
        nonlocal calls
        calls += 1
        return await original_move(x, y)

    app.state.backend.mouse_move = move
    response = test_client.post(
        "/v1/actions/run/raw-screenshot",
        headers={"Idempotency-Key": "raw-key"},
        json={
            "actions": [{"type": "move", "x": 1, "y": 2}],
            "screenshot_after": True,
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "raw_screenshot_idempotency_not_supported"
    assert calls == 0


@pytest.mark.parametrize("path", ["/v1/commands/run", "/v1/apps/launch"])
def test_e2big_is_not_dispatched_and_sequence_can_be_reused(test_client, app, path: str) -> None:
    lease = test_client.post("/v1/leases/acquire", json={"run_id": f"e2big-{path}"})
    assert lease.status_code == 200
    body = lease.json()
    headers = {
        "X-Computer-Use-Lease-Id": body["lease_id"],
        "X-Computer-Use-Lease-Epoch": body["daemon_epoch"],
        "X-Computer-Use-Lease-Fence": str(body["fence"]),
        "X-Computer-Use-Lease-Token": lease.headers["x-computer-use-lease-token"],
        "X-Computer-Use-Operation-Sequence": "0",
    }
    if path.endswith("commands/run"):
        async def run_command(_command, timeout=30.0):
            raise OSError(errno.E2BIG, "private argument list")

        app.state.backend.run_command = run_command
        payload = {"command": ["true"]}
    else:
        async def launch(_command, _args=()):
            raise OSError(errno.E2BIG, "private argument list")

        app.state.backend.launch = launch
        payload = {"command": "true", "args": []}

    first = test_client.post(path, headers=headers, json=payload)
    assert first.status_code == 422
    assert first.json()["code"] == "command_too_large"
    assert "private argument list" not in first.text

    async def success(*_args, **_kwargs):
        return ActionResult(ok=True)

    if path.endswith("commands/run"):
        app.state.backend.run_command = success
    else:
        app.state.backend.launch = success
    second = test_client.post(path, headers=headers, json=payload)
    assert second.status_code == 200


def test_daemon_settings_validate_timeout_ranges_and_order(tmp_path) -> None:
    kwargs = {
        "backend": "mock",
        "artifacts_dir": tmp_path / "artifacts",
        "recordings_dir": tmp_path / "recordings",
        "local_token": "dev",
    }
    with pytest.raises(ValueError, match="DEFAULT_ACTION_TIMEOUT_MS"):
        DaemonSettings(**kwargs, default_action_timeout_ms=0)
    with pytest.raises(ValueError, match="MAX_ACTION_TIMEOUT_MS"):
        DaemonSettings(**kwargs, max_action_timeout_ms=600_001)
    with pytest.raises(ValueError, match="default_action_timeout_ms"):
        DaemonSettings(**kwargs, default_action_timeout_ms=20, max_action_timeout_ms=10)


def test_openapi_matches_auth_errors_and_raw_binary_contract() -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=Path("/tmp/contracts-artifacts"),
            recordings_dir=Path("/tmp/contracts-recordings"),
            local_token="dev",
        )
    )
    schema = app.openapi()
    assert "bearerAuth" in schema["components"]["securitySchemes"]
    assert "security" not in schema["paths"]["/readyz"]["get"]
    assert schema["paths"]["/v1/actions/run"]["post"]["security"] == [{"bearerAuth": []}]
    assert (
        schema["paths"]["/v1/actions/run"]["post"]["responses"]["422"]["content"]
        ["application/json"]["schema"]["$ref"]
        == "#/components/schemas/DaemonErrorResponse"
    )
    raw = schema["paths"]["/v1/actions/run/raw-screenshot"]["post"]
    assert not any(
        parameter.get("name") == "Idempotency-Key"
        for parameter in raw.get("parameters", [])
    )
    assert set(raw["responses"]["200"]["headers"]) >= {
        "x-computer-use-width",
        "x-computer-use-height",
        "x-computer-use-size-bytes",
        "x-computer-use-sha256",
        "x-computer-use-captured-at",
        "x-computer-use-coordinate-space",
        "x-computer-use-cursor-visible",
        "x-computer-use-cursor-position",
        "x-computer-use-action-result",
    }


def test_raw_screenshot_sdk_does_not_advertise_idempotency() -> None:
    for method in (
        ActionsNamespace.run_and_screenshot_bytes,
        ActionsNamespace.run_and_observe_change_screenshot_bytes,
        AsyncActionsNamespace.run_and_screenshot_bytes,
        AsyncActionsNamespace.run_and_observe_change_screenshot_bytes,
    ):
        assert "idempotency_key" not in inspect.signature(method).parameters
