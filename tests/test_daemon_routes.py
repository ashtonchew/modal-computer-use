from __future__ import annotations

import time

from fastapi.testclient import TestClient

from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.models import ProcessStatus, Screenshot


def test_health_version_capabilities(test_client) -> None:
    assert test_client.get("/healthz").json() == {"ok": True}
    assert test_client.get("/readyz").json()["ready"] is True
    assert test_client.get("/v1/version").json()["api_version"] == "v1"
    caps = test_client.get("/v1/capabilities").json()
    assert "mouse" in caps["primitives"]
    for primitive in ("input", "lifecycle", "processes", "session", "debug"):
        assert primitive in caps["primitives"]


def test_status_and_screenshot(test_client) -> None:
    status = test_client.get("/v1/computer/status").json()
    assert status["ready"] is True
    response = test_client.post("/v1/screenshots/full", json={"format": "png", "show_cursor": True})
    shot = Screenshot.model_validate(response.json())
    assert shot.width == 1440
    assert shot.coordinate_space.desktop_width == 1440
    assert shot.sha256


def test_status_reflects_stopped_mock_lifecycle(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        assert client.post("/v1/computer/stop").json()["status"] == "stopped"
        status = client.get("/v1/computer/status").json()

    assert status["status"] == "stopped"
    assert status["ready"] is False
    assert {item["status"] for item in status["processes"].values()} == {"stopped"}


def test_clipboard_and_release_all(test_client) -> None:
    assert test_client.put("/v1/clipboard/text", json={"text": "secret"}).json()["ok"] is True
    assert test_client.get("/v1/clipboard/text").json()["text"] == "secret"
    assert test_client.delete("/v1/clipboard/text").json()["ok"] is True
    test_client.post("/v1/mouse/down", json={"button": "left"})
    released = test_client.post("/v1/input/release-all").json()
    assert released["ok"] is True
    assert "left" in released["output"]["buttons"]


def test_readyz_checks_x11vnc_when_vnc_enabled(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            vnc_mode="view_only",
        )
    )

    original_status = app.state.supervisor.status

    def status(name: str) -> ProcessStatus:
        if name == "x11vnc":
            return ProcessStatus(name=name, status="failed")
        return original_status(name)

    app.state.supervisor.status = status

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.json()["ready"] is False
    assert "x11vnc is not running" in response.json()["errors"]


def test_status_uses_same_vnc_readiness_as_readyz(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            vnc_mode="view_only",
        )
    )
    original_status = app.state.supervisor.status

    def status(name: str) -> ProcessStatus:
        if name == "novnc":
            return ProcessStatus(name=name, status="failed")
        return original_status(name)

    app.state.supervisor.status = status

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        readyz = client.get("/readyz")
        status_response = client.get("/v1/computer/status")

    assert readyz.status_code == 503
    assert readyz.json()["ready"] is False
    assert "novnc is not running" in readyz.json()["errors"]
    assert status_response.status_code == 200
    assert status_response.json()["ready"] is False
    assert status_response.json()["status"] == "degraded"


def test_idle_budget_blocks_mutating_primitive_but_allows_status(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_idle_seconds=1,
        )
    )
    app.state.last_activity_at = time.monotonic() - 2

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        status = client.get("/v1/computer/status")
        response = client.put("/v1/clipboard/text", json={"text": "secret"})

    assert status.status_code == 200
    assert status.json()["budgets"]["max_idle_seconds"] == 1
    assert response.status_code == 429
    assert response.json()["code"] == "budget_exceeded"
    assert response.json()["message"] == "idle time budget exceeded"


def test_idle_budget_blocks_browser_and_app_mutations(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_idle_seconds=1,
        )
    )
    app.state.last_activity_at = time.monotonic() - 2

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        browser = client.post("/v1/browser/open-url", json={"url": "https://example.com"})
        launch = client.post("/v1/apps/launch", json={"command": "firefox"})

    assert browser.status_code == 429
    assert browser.json()["code"] == "budget_exceeded"
    assert launch.status_code == 429
    assert launch.json()["code"] == "budget_exceeded"


def test_idle_budget_blocks_commands(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_idle_seconds=1,
        )
    )
    app.state.last_activity_at = time.monotonic() - 2

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post("/v1/commands/run", json={"command": ["echo", "secret"]})

    assert response.status_code == 429
    assert response.json()["code"] == "budget_exceeded"


def test_idle_budget_blocks_lifecycle_and_process_mutations(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_idle_seconds=1,
        )
    )
    app.state.last_activity_at = time.monotonic() - 2

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        lifecycle = client.post("/v1/computer/restart")
        process = client.post("/v1/processes/xvfb/restart")

    assert lifecycle.status_code == 429
    assert lifecycle.json()["code"] == "budget_exceeded"
    assert process.status_code == 429
    assert process.json()["code"] == "budget_exceeded"
