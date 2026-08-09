from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import anyio
import pytest
from fastapi.testclient import TestClient

from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.desktop.xtest import (
    X11InputInjectionError,
    X11InputReleaseError,
    X11InputStateConflictError,
    X11InputUnavailableError,
)
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.routes.validation import (
    begin_display_restart,
    desktop_readiness,
    end_display_restart,
    http_observe_change_scope,
)
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.models import ActionResult, Point, ProcessStatus, Screenshot


def test_health_version_capabilities(test_client) -> None:
    assert test_client.get("/healthz").json() == {"ok": True}
    assert test_client.get("/readyz").json()["ready"] is True
    version = test_client.get("/v1/version").json()
    assert version["api_version"] == "v1"
    assert version["daemon_version"] == "2.0.0"
    assert version["sdk_min_version"] == "1.1.0"
    assert version["sdk_max_version"] == "2.x"
    caps = test_client.get("/v1/capabilities").json()
    assert "mouse" in caps["primitives"]
    assert caps["input_backend"] == "mock"
    assert caps["input_backend_configured"] == "mock"
    assert caps["input_backends_supported"] == ["mock"]
    assert caps["input_backends_available"] == ["mock"]
    assert caps["input_rate_limit_policy"] == "normalized-input-work-v1"
    assert caps["input_rate_limit_tokens_per_sec"] == 100
    assert caps["input_rate_limit_burst"] == 400
    for primitive in ("input", "lifecycle", "processes", "session", "debug"):
        assert primitive in caps["primitives"]
    assert "screenshot-binary-metadata-v1" in caps["primitives"]


@pytest.mark.parametrize("backend_used", ["xtest", "xdotool"])
def test_direct_mouse_move_reports_the_backend_used_without_changing_its_body(
    test_client,
    app,
    monkeypatch,
    backend_used: str,
) -> None:
    selected_backend = "before-operation"

    monkeypatch.setattr(
        type(app.state.backend),
        "input_backend",
        property(lambda _backend: selected_backend),
    )

    async def move(x: int, y: int) -> Point:
        nonlocal selected_backend
        selected_backend = backend_used
        return Point(x=x, y=y)

    app.state.backend.mouse_move = move

    response = test_client.post("/v1/mouse/move", json={"x": 7, "y": 9})

    assert response.status_code == 200
    assert response.json() == {"x": 7, "y": 9}
    assert response.headers["x-computer-use-input-backend"] == backend_used


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/v1/mouse/click", {"x": 7, "y": 9}),
        (
            "POST",
            "/v1/mouse/drag",
            {"start_x": 1, "start_y": 2, "end_x": 7, "end_y": 9},
        ),
        ("POST", "/v1/mouse/scroll", {"direction": "down", "amount": 2}),
        ("POST", "/v1/mouse/down", {"button": "left"}),
        ("POST", "/v1/mouse/up", {"button": "left"}),
        ("GET", "/v1/mouse/position", None),
    ],
)
def test_each_direct_mouse_route_reports_its_backend(
    test_client,
    method: str,
    path: str,
    payload: dict[str, object] | None,
) -> None:
    response = test_client.request(method, path, json=payload)

    assert response.status_code == 200
    assert response.headers["x-computer-use-input-backend"] == "mock"


def test_status_and_screenshot(test_client) -> None:
    status = test_client.get("/v1/computer/status").json()
    assert status["ready"] is True
    response = test_client.post("/v1/screenshots/full", json={"format": "png", "show_cursor": True})
    shot = Screenshot.model_validate(response.json())
    assert shot.width == 1024
    assert shot.coordinate_space.desktop_width == 1024
    assert shot.sha256


@pytest.mark.parametrize("show_cursor", [False, True])
def test_raw_screenshot_returns_image_bytes_and_complete_metadata(
    test_client,
    show_cursor: bool,
) -> None:
    response = test_client.post(
        "/v1/screenshots/full/raw",
        json={"format": "png", "show_cursor": show_cursor},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert response.headers["x-computer-use-width"] == "1024"
    assert response.headers["x-computer-use-height"] == "768"
    assert response.headers["x-computer-use-size-bytes"] == str(len(response.content))
    assert response.headers["x-computer-use-sha256"]
    assert response.headers["x-computer-use-capture-backend"]
    assert response.headers["x-computer-use-cursor-visible"] == str(show_cursor).lower()
    assert json.loads(response.headers["x-computer-use-cursor-position"]) == {"x": 0, "y": 0}
    timing = json.loads(response.headers["x-computer-use-timing-ms"])
    assert isinstance(timing, dict)


def test_raw_screenshot_rejects_artifact_storage(test_client) -> None:
    response = test_client.post(
        "/v1/screenshots/full/raw",
        json={"format": "png", "storage": "artifact"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_screenshot_storage"


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


def test_mock_lifecycle_stop_start_restart_reprobes_each_display_generation(
    test_client,
    app,
    monkeypatch,
) -> None:
    events: list[str] = []

    async def invalidate_display_generation() -> None:
        events.append("invalidate")

    async def ready() -> tuple[bool, list[str]]:
        events.append("ready")
        return True, []

    monkeypatch.setattr(
        app.state.backend,
        "invalidate_display_generation",
        invalidate_display_generation,
    )
    monkeypatch.setattr(app.state.backend, "ready", ready)

    assert test_client.post("/v1/computer/stop").status_code == 200
    assert test_client.post("/v1/computer/start").status_code == 200
    assert test_client.post("/v1/computer/restart").status_code == 200

    assert events == ["invalidate", "invalidate", "ready", "invalidate", "ready"]


@pytest.mark.parametrize(
    "path", ["/v1/computer/start", "/v1/computer/stop", "/v1/computer/restart"]
)
def test_display_lifecycle_invalidates_display_generation_before_supervisor_mutation(
    test_client, app, monkeypatch, path: str
) -> None:
    events: list[str] = []

    async def invalidate_display_generation() -> None:
        events.append("display-generation-invalidated")

    async def stop() -> None:
        events.append("supervisor-stop")

    async def start() -> None:
        events.append("supervisor-start")

    async def restart(name: str | None = None) -> None:
        assert name is None
        events.append("supervisor-restart")

    monkeypatch.setattr(
        app.state.backend,
        "invalidate_display_generation",
        invalidate_display_generation,
    )
    monkeypatch.setattr(app.state.supervisor, "stop", stop)
    monkeypatch.setattr(app.state.supervisor, "start", start)
    monkeypatch.setattr(app.state.supervisor, "restart", restart)

    response = test_client.post(path)

    assert response.status_code == 200
    expected_mutation = {
        "/v1/computer/start": "supervisor-start",
        "/v1/computer/stop": "supervisor-stop",
        "/v1/computer/restart": "supervisor-restart",
    }[path]
    assert events == ["display-generation-invalidated", expected_mutation]


def test_xvfb_process_restart_invalidates_display_generation_first(
    test_client, app, monkeypatch
) -> None:
    events: list[str] = []

    async def invalidate_display_generation() -> None:
        events.append("display-generation-invalidated")

    async def restart(name: str | None = None) -> None:
        assert name == "xvfb"
        events.append("supervisor-restart:xvfb")

    monkeypatch.setattr(
        app.state.backend,
        "invalidate_display_generation",
        invalidate_display_generation,
    )
    monkeypatch.setattr(app.state.supervisor, "restart", restart)

    response = test_client.post("/v1/processes/xvfb/restart")

    assert response.status_code == 200
    assert events == ["display-generation-invalidated", "supervisor-restart:xvfb"]


@pytest.mark.parametrize("path", ["/v1/computer/stop", "/v1/computer/restart"])
def test_display_lifecycle_still_mutates_supervisor_when_generation_invalidation_fails(
    test_client, app, monkeypatch, path: str
) -> None:
    events: list[str] = []

    async def invalidate_display_generation() -> None:
        events.append("display-generation-invalidated")
        raise RuntimeError("detach failed")

    async def stop() -> None:
        events.append("supervisor-stop")

    async def restart(name: str | None = None) -> None:
        assert name is None
        events.append("supervisor-restart")

    monkeypatch.setattr(
        app.state.backend,
        "invalidate_display_generation",
        invalidate_display_generation,
    )
    monkeypatch.setattr(app.state.supervisor, "stop", stop)
    monkeypatch.setattr(app.state.supervisor, "restart", restart)

    with pytest.raises(RuntimeError, match="detach failed"):
        test_client.post(path)

    expected_mutation = "supervisor-stop" if path.endswith("/stop") else "supervisor-restart"
    assert events == ["display-generation-invalidated", expected_mutation]


def test_display_lifecycle_preserves_generation_error_when_supervisor_also_fails(
    test_client,
    app,
    monkeypatch,
) -> None:
    events: list[str] = []

    async def invalidate_display_generation() -> None:
        events.append("display-generation-invalidated")
        raise RuntimeError("generation detach failed")

    async def restart() -> None:
        events.append("supervisor-restart")
        raise RuntimeError("supervisor failed")

    monkeypatch.setattr(
        app.state.backend,
        "invalidate_display_generation",
        invalidate_display_generation,
    )
    monkeypatch.setattr(app.state.supervisor, "restart", restart)

    with pytest.raises(RuntimeError, match="generation detach failed"):
        test_client.post("/v1/computer/restart")

    assert events == ["display-generation-invalidated", "supervisor-restart"]
    assert app.state.display_restart_in_progress is False
    assert app.state.display_reconstruction_failed is True
    assert test_client.get("/readyz").json()["ready"] is False


def test_display_restart_retries_transient_readiness_and_leaves_final_probe_recoverable(
    test_client,
    app,
    monkeypatch,
) -> None:
    outcomes = iter(
        [
            (False, ["display is settling"]),
            (False, ["display is still settling"]),
            (True, []),
        ]
    )
    calls = 0

    async def ready() -> tuple[bool, list[str]]:
        nonlocal calls
        calls += 1
        return next(outcomes)

    async def invalidate_display_generation() -> None:
        return None

    monkeypatch.setattr(app.state.backend, "ready", ready)
    monkeypatch.setattr(
        app.state.backend,
        "invalidate_display_generation",
        invalidate_display_generation,
    )

    response = test_client.post("/v1/computer/restart")

    assert response.status_code == 200
    assert test_client.get("/readyz").json()["ready"] is True
    assert calls == 3


def test_display_restart_verifies_display_before_configured_browser_recovery(
    test_client,
    app,
    monkeypatch,
) -> None:
    app.state.settings = replace(app.state.settings, browser_prewarm=True)
    events: list[str] = []

    async def ready() -> tuple[bool, list[str]]:
        events.append("ready")
        return True, []

    async def prewarm_browser() -> ActionResult:
        events.append("browser-prewarm")
        return ActionResult(ok=True)

    async def invalidate_display_generation() -> None:
        events.append("invalidate")

    monkeypatch.setattr(app.state.backend, "ready", ready)
    monkeypatch.setattr(app.state.backend, "prewarm_browser", prewarm_browser)
    monkeypatch.setattr(
        app.state.backend,
        "invalidate_display_generation",
        invalidate_display_generation,
    )

    response = test_client.post("/v1/computer/restart")

    assert response.status_code == 200
    assert events == ["invalidate", "ready", "browser-prewarm", "ready"]
    assert app.state.browser_prewarm.ok is True


def test_display_restart_reopens_configured_browser_url_after_readiness(
    test_client,
    app,
    monkeypatch,
) -> None:
    app.state.settings = replace(
        app.state.settings,
        browser_open_url_on_start="https://example.com",
    )
    events: list[str] = []

    async def ready() -> tuple[bool, list[str]]:
        events.append("ready")
        return True, []

    async def open_url(url: str, wait_for_window: bool = True) -> ActionResult:
        events.append(f"open-url:{url}:{wait_for_window}")
        return ActionResult(ok=True, output={"url": url})

    async def invalidate_display_generation() -> None:
        events.append("invalidate")

    monkeypatch.setattr(app.state.backend, "ready", ready)
    monkeypatch.setattr(app.state.backend, "open_url", open_url)
    monkeypatch.setattr(
        app.state.backend,
        "invalidate_display_generation",
        invalidate_display_generation,
    )

    response = test_client.post("/v1/computer/restart")

    assert response.status_code == 200
    assert events == [
        "invalidate",
        "ready",
        "open-url:https://example.com:True",
        "ready",
    ]
    assert app.state.browser_prewarm.ok is True
    assert app.state.browser_prewarm.output == {"url": "https://example.com"}


def test_display_restart_browser_failure_invalidates_successful_readiness_snapshot(
    test_client,
    app,
    monkeypatch,
) -> None:
    app.state.settings = replace(app.state.settings, browser_prewarm=True)
    outcomes = iter([(True, []), (False, ["browser left display unavailable"])])
    calls = 0

    async def ready() -> tuple[bool, list[str]]:
        nonlocal calls
        calls += 1
        return next(outcomes)

    async def prewarm_browser() -> ActionResult:
        return ActionResult(ok=False, message="browser failed")

    monkeypatch.setattr(app.state.backend, "ready", ready)
    monkeypatch.setattr(app.state.backend, "prewarm_browser", prewarm_browser)

    response = test_client.post("/v1/computer/restart")

    assert response.status_code == 503
    assert response.json()["code"] == "browser_recovery_failed"
    assert test_client.get("/readyz").json()["ready"] is False

    async def forced_readiness() -> tuple[bool, list[str]]:
        return await desktop_readiness(SimpleNamespace(app=app), force=True)

    assert anyio.run(forced_readiness) == (False, ["display lifecycle reconstruction failed"])
    assert calls == 1


def test_display_restart_and_http_observe_change_admission_exclude_both_interleavings(
    app,
) -> None:
    request = SimpleNamespace(app=app)

    async def exercise() -> None:
        async with http_observe_change_scope(request):
            with pytest.raises(DaemonError, match="active observe-change"):
                begin_display_restart(app.state)
        begin_display_restart(app.state)
        with pytest.raises(DaemonError, match="already in progress"):
            async with http_observe_change_scope(request):
                pass
        end_display_restart(app.state)

    anyio.run(exercise)


def test_readyz_is_closed_during_display_restart_transition(test_client, app) -> None:
    app.state.display_restart_in_progress = True

    response = test_client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "ready": False,
        "errors": ["display lifecycle mutation is in progress"],
    }


@pytest.mark.parametrize("busy_kind", ["recording", "websocket", "observe-change"])
def test_display_restart_rejects_active_display_observers(
    test_client,
    app,
    monkeypatch,
    busy_kind: str,
) -> None:
    if busy_kind == "recording":
        monkeypatch.setattr(
            app.state.recordings,
            "list",
            lambda: [SimpleNamespace(status="recording")],
        )
    elif busy_kind == "websocket":
        monkeypatch.setattr(
            app.state.websocket_admission,
            "active",
            lambda kind: 1 if kind == "observation" else 0,
        )
    else:
        app.state.active_http_observe_changes = 1

    response = test_client.post("/v1/computer/restart")

    assert response.status_code == 409
    assert response.json()["code"] == "display_restart_busy"


def test_clipboard_and_release_all(test_client) -> None:
    assert test_client.put("/v1/clipboard/text", json={"text": "secret"}).json()["ok"] is True
    assert test_client.get("/v1/clipboard/text").json()["text"] == "secret"
    assert test_client.delete("/v1/clipboard/text").json()["ok"] is True
    test_client.post("/v1/mouse/down", json={"button": "left"})
    released = test_client.post("/v1/input/release-all").json()
    assert released["ok"] is True
    assert "left" in released["output"]["buttons"]


def test_direct_release_all_reports_incomplete_cleanup(test_client, app) -> None:
    async def incomplete_release() -> ActionResult:
        return ActionResult(
            ok=False,
            message="failed to release all held input",
            output={
                "code": "release_all_incomplete",
                "keys": [],
                "buttons": [],
                "remaining": {"keys": ["shift"], "buttons": []},
                "failures": [
                    {
                        "kind": "key",
                        "value": "shift",
                        "input_backend": "xtest",
                        "code": "key_release_failed",
                    }
                ],
            },
        )

    app.state.backend.release_all = incomplete_release

    response = test_client.post("/v1/input/release-all")

    assert response.status_code == 400
    assert response.json() == {
        "code": "release_all_incomplete",
        "message": "failed to release all held input",
        "details": {
            "code": "release_all_incomplete",
            "keys": [],
            "buttons": [],
            "remaining": {"keys": ["shift"], "buttons": []},
            "failures": [
                {
                    "kind": "key",
                    "value": "shift",
                    "input_backend": "xtest",
                    "code": "key_release_failed",
                }
            ],
        },
    }


def test_action_budget_blocks_direct_release_all_before_backend_call(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            max_actions=0,
        )
    )
    calls = 0

    async def release_all() -> ActionResult:
        nonlocal calls
        calls += 1
        return ActionResult(ok=True)

    app.state.backend.release_all = release_all

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post("/v1/input/release-all")

    assert response.status_code == 429
    assert response.json()["code"] == "budget_exceeded"
    assert calls == 0
    assert app.state.action_count == 0


def test_rate_limit_blocks_direct_release_all_before_backend_call(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            input_rate_limit_per_sec=1,
            input_rate_limit_burst=1,
        )
    )
    calls = 0

    async def release_all() -> ActionResult:
        nonlocal calls
        calls += 1
        return ActionResult(ok=True)

    app.state.backend.release_all = release_all

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        first = client.post("/v1/mouse/move", json={"x": 1, "y": 1})
        second = client.post("/v1/input/release-all")

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["code"] == "rate_limited"
    assert calls == 0


def test_direct_action_result_failure_returns_http_error(test_client, app) -> None:
    async def mouse_down(button: str = "left", x: int | None = None, y: int | None = None):
        del button, x, y
        return ActionResult(ok=False, message="mouse down refused", output={"code": "denied"})

    app.state.backend.mouse_down = mouse_down

    response = test_client.post("/v1/mouse/down", json={"button": "left"})

    assert response.status_code == 400
    assert response.json()["code"] == "denied"
    assert response.json()["message"] == "mouse down refused"


@pytest.mark.parametrize(
    ("exception", "status_code", "code", "retry_safe", "emission_state", "message"),
    [
        (
            X11InputUnavailableError,
            503,
            "input_backend_unavailable",
            True,
            "not_started",
            "native input backend is unavailable before input emission",
        ),
        (
            X11InputInjectionError,
            500,
            "input_may_be_partial",
            False,
            "possibly_partial",
            "input may have been partially applied",
        ),
    ],
)
def test_direct_native_input_failure_preserves_retry_contract(
    test_client,
    app,
    exception,
    status_code: int,
    code: str,
    retry_safe: bool,
    emission_state: str,
    message: str,
) -> None:
    sentinel = "SENTINEL_TYPED_TEXT_MUST_NOT_LEAK"

    async def fail_move(x: int, y: int):
        del x, y
        raise exception(sentinel)

    app.state.backend.mouse_move = fail_move

    response = test_client.post("/v1/mouse/move", json={"x": 1, "y": 1})

    assert response.status_code == status_code
    assert response.headers["x-computer-use-error-code"] == code
    assert response.json() == {
        "code": code,
        "message": message,
        "details": {
            "input_backend": "xtest",
            "retry_safe": retry_safe,
            "emission_state": emission_state,
        },
    }
    assert sentinel not in response.text


def test_direct_release_failure_is_retry_safe_and_preserves_backend(test_client, app) -> None:
    async def fail_up(button: str, x: int | None = None, y: int | None = None):
        del button, x, y
        raise X11InputReleaseError(
            "private release details",
            input_backend="xdotool",
        )

    app.state.backend.mouse_up = fail_up

    response = test_client.post("/v1/mouse/up", json={"button": "left"})

    assert response.status_code == 500
    assert response.json() == {
        "code": "input_may_be_partial",
        "message": "input release may have been partially applied",
        "details": {
            "input_backend": "xdotool",
            "retry_safe": True,
            "emission_state": "possibly_partial",
        },
    }
    assert "private release details" not in response.text


def test_direct_unavailable_failure_preserves_xdotool_identity(test_client, app) -> None:
    async def fail_move(x: int, y: int):
        del x, y
        raise X11InputUnavailableError(
            "private adapter details",
            input_backend="xdotool",
        )

    app.state.backend.mouse_move = fail_move

    response = test_client.post("/v1/mouse/move", json={"x": 1, "y": 1})

    assert response.status_code == 503
    assert response.json()["details"]["input_backend"] == "xdotool"
    assert "private adapter details" not in response.text


def test_direct_partial_failure_preserves_xdotool_identity(test_client, app) -> None:
    async def fail_move(x: int, y: int):
        del x, y
        raise X11InputInjectionError(
            "private adapter details",
            input_backend="xdotool",
        )

    app.state.backend.mouse_move = fail_move

    response = test_client.post("/v1/mouse/move", json={"x": 1, "y": 1})

    assert response.status_code == 500
    assert response.json()["details"] == {
        "input_backend": "xdotool",
        "retry_safe": False,
        "emission_state": "possibly_partial",
    }
    assert "private adapter details" not in response.text


def test_direct_input_state_conflict_is_retry_safe(test_client, app) -> None:
    async def fail_move(x: int, y: int):
        del x, y
        raise X11InputStateConflictError("private state details")

    app.state.backend.mouse_move = fail_move

    response = test_client.post("/v1/mouse/move", json={"x": 1, "y": 1})

    assert response.status_code == 409
    assert response.headers["x-computer-use-error-code"] == "input_state_conflict"
    assert response.json() == {
        "code": "input_state_conflict",
        "message": "input target is already held",
        "details": {
            "retry_safe": True,
            "emission_state": "not_started",
        },
    }


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


def test_idle_budget_blocks_open_artifact_before_path_lookup(tmp_path) -> None:
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
        missing = client.post(
            "/v1/apps/open-artifact",
            json={"path": "private/customer-secret.txt"},
        )

    assert missing.status_code == 429
    assert missing.json()["code"] == "budget_exceeded"
    assert missing.json()["message"] == "idle time budget exceeded"
    assert "customer-secret" not in missing.text


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


def test_unknown_process_restart_returns_structured_client_error(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )

    with TestClient(
        app,
        headers={"Authorization": "Bearer dev"},
        raise_server_exceptions=False,
    ) as client:
        response = client.post("/v1/processes/not-a-process/restart")

    assert response.status_code == 404
    assert response.json() == {
        "code": "unknown_process",
        "message": "unknown process",
        "details": {"name": "not-a-process"},
    }
