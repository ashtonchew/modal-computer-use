from __future__ import annotations

import json
import logging
import sys

from fastapi.testclient import TestClient

from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.logging import JsonFormatter
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.models import ActionResult
from modal_computer_use.redaction import sanitize_payload


def _app(tmp_path, **overrides):
    return create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            **overrides,
        )
    )


def test_health_and_readyz_do_not_require_auth(tmp_path) -> None:
    app = _app(tmp_path, local_token="dev")

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200


def test_local_token_mode_rejects_missing_and_invalid_bearer_token(tmp_path) -> None:
    app = _app(tmp_path, local_token="dev")

    with TestClient(app) as client:
        missing = client.get("/v1/version")
        invalid = client.get("/v1/version", headers={"Authorization": "Bearer wrong"})

    assert missing.status_code == 401
    assert missing.json()["code"] == "unauthorized"
    assert invalid.status_code == 401
    assert invalid.json()["code"] == "unauthorized"


def test_local_token_mode_rejects_non_loopback_clients(tmp_path) -> None:
    app = _app(tmp_path, local_token="dev")

    with TestClient(
        app,
        client=("203.0.113.10", 50000),
        headers={"Authorization": "Bearer dev"},
    ) as client:
        response = client.get("/v1/version")

    assert response.status_code == 401
    assert response.json()["code"] == "local_token_requires_loopback"


def test_query_connect_tokens_are_rejected_when_configured(tmp_path) -> None:
    app = _app(tmp_path, require_connect_user=False, reject_query_tokens=True)

    with TestClient(app) as client:
        response = client.get("/v1/version?_modal_connect_token=secret")

    assert response.status_code == 401
    assert response.json()["code"] == "query_token_rejected"


def test_query_connect_tokens_are_rejected_on_health_and_readyz(tmp_path) -> None:
    app = _app(tmp_path, require_connect_user=False, reject_query_tokens=True)

    with TestClient(app) as client:
        health = client.get("/healthz?_modal_connect_token=secret")
        ready = client.get("/readyz?_modal_connect_token=secret")

    assert health.status_code == 401
    assert health.json()["code"] == "query_token_rejected"
    assert ready.status_code == 401
    assert ready.json()["code"] == "query_token_rejected"


def test_require_connect_user_rejects_missing_verified_user_metadata(tmp_path) -> None:
    app = _app(tmp_path, require_connect_user=True, reject_query_tokens=True)

    with TestClient(app) as client:
        missing = client.get("/v1/version")
        spoofed = client.get("/v1/version", headers={"X-Verified-User-Data": "{}"})
        invalid = client.get("/v1/version", headers={"X-Verified-User-Data": "not-json"})
        present = client.get(
            "/v1/version",
            headers={"X-Verified-User-Data": '{"sdk":"modal-computer-use"}'},
        )

    assert missing.status_code == 401
    assert missing.json()["code"] == "connect_token_required"
    assert spoofed.status_code == 401
    assert spoofed.json()["code"] == "invalid_verified_user_data"
    assert invalid.status_code == 401
    assert invalid.json()["code"] == "invalid_verified_user_data"
    assert present.status_code == 200


def test_require_connect_user_rejects_spoofed_public_clients(tmp_path) -> None:
    app = _app(tmp_path, require_connect_user=True, reject_query_tokens=True)

    with TestClient(
        app,
        client=("8.8.8.8", 50000),
        headers={"X-Verified-User-Data": '{"sdk":"modal-computer-use"}'},
    ) as client:
        response = client.get("/v1/version")

    assert response.status_code == 401
    assert response.json()["code"] == "connect_token_required"


def test_require_connect_user_rejects_spoofed_private_clients_by_default(tmp_path) -> None:
    app = _app(tmp_path, require_connect_user=True, reject_query_tokens=True)

    with TestClient(
        app,
        client=("10.0.0.5", 50000),
        headers={"X-Verified-User-Data": '{"sdk":"modal-computer-use"}'},
    ) as client:
        response = client.get("/v1/version")

    assert response.status_code == 401
    assert response.json()["code"] == "connect_token_required"


def test_require_connect_user_can_opt_into_private_connect_proxy_trust(tmp_path) -> None:
    app = _app(
        tmp_path,
        require_connect_user=True,
        reject_query_tokens=True,
        trust_private_connect_proxy=True,
    )

    with TestClient(
        app,
        client=("10.0.0.5", 50000),
        headers={"X-Verified-User-Data": '{"sdk":"modal-computer-use"}'},
    ) as client:
        response = client.get("/v1/version")

    assert response.status_code == 200


def test_json_formatter_redacts_exception_messages() -> None:
    formatter = JsonFormatter()
    try:
        raise RuntimeError("typed secret should not appear")
    except RuntimeError:
        record = logging.getLogger("modal_computer_use.test").makeRecord(
            "modal_computer_use.test",
            logging.ERROR,
            __file__,
            1,
            "failed with typed secret should not appear",
            (),
            exc_info=sys.exc_info(),
        )

    payload = json.loads(formatter.format(record))

    assert payload["message"] == "[redacted exception]"
    assert payload["exc_info"]["redacted"] is True
    serialized = json.dumps(payload)
    assert "typed secret should not appear" not in serialized


def test_json_formatter_redacts_provider_credentials_in_extra() -> None:
    formatter = JsonFormatter()
    record = logging.getLogger("modal_computer_use.test").makeRecord(
        "modal_computer_use.test",
        logging.INFO,
        __file__,
        1,
        "safe message",
        (),
        exc_info=None,
        extra={"extra": {"api_key": "sk-test-secret", "password": "pw-secret"}},
    )

    payload = json.loads(formatter.format(record))
    serialized = json.dumps(payload)

    assert payload["api_key"]["redacted"] is True
    assert payload["password"]["redacted"] is True
    assert "sk-test-secret" not in serialized
    assert "pw-secret" not in serialized


def test_json_formatter_redacts_secret_bearing_observability_fields() -> None:
    formatter = JsonFormatter()
    record = logging.getLogger("modal_computer_use.test").makeRecord(
        "modal_computer_use.test",
        logging.INFO,
        __file__,
        1,
        "safe message",
        (),
        exc_info=None,
        extra={
            "extra": {
                "artifact_uri": "artifact://screenshots/private.png",
                "stdout": "Bearer stdout-secret",
                "stderr": "stderr-secret",
                "url": "https://novnc.example/?token=url-secret",
            }
        },
    )

    payload = json.loads(formatter.format(record))
    serialized = json.dumps(payload)

    assert payload["artifact_uri"]["redacted"] is True
    assert payload["stdout"]["redacted"] is True
    assert payload["stderr"]["redacted"] is True
    assert payload["url"]["redacted"] is True
    assert "artifact://screenshots/private.png" not in serialized
    assert "stdout-secret" not in serialized
    assert "stderr-secret" not in serialized
    assert "url-secret" not in serialized


def test_sanitize_payload_redacts_sensitive_numeric_values() -> None:
    payload = sanitize_payload(
        {
            "token": 12345,
            "api_key": 3.14,
            "password": False,
            "nested": {"artifact_bytes": 9},
            "safe_count": 7,
            "empty_token": None,
        }
    )

    assert payload["token"] == {"redacted": True}
    assert payload["api_key"] == {"redacted": True}
    assert payload["password"] == {"redacted": True}
    assert payload["nested"]["artifact_bytes"] == {"redacted": True}
    assert payload["safe_count"] == 7
    assert payload["empty_token"] is None


def test_command_run_sanitizes_stdout_stderr_and_message(tmp_path) -> None:
    app = _app(tmp_path, local_token="dev")

    async def run_command(command, timeout=30.0):
        return ActionResult(
            ok=False,
            message="Bearer message-secret",
            output={
                "returncode": 1,
                "stdout": "Bearer stdout-secret",
                "stderr": "artifact://logs/stderr-secret.txt",
            },
        )

    app.state.backend.run_command = run_command

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post("/v1/commands/run", json={"command": ["echo", "secret"]})

    body = response.json()
    serialized = json.dumps(body)

    assert response.status_code == 200
    assert body["message"] == "Bearer [redacted]"
    assert body["output"]["stdout"] == "Bearer [redacted]"
    assert body["output"]["stderr"] == "[redacted]"
    assert "message-secret" not in serialized
    assert "stdout-secret" not in serialized
    assert "stderr-secret" not in serialized


def test_direct_keyboard_and_clipboard_mutations_sanitize_reflected_text(tmp_path) -> None:
    app = _app(tmp_path, local_token="dev")

    async def keyboard_type(text, delay_ms=10, method="auto"):
        return ActionResult(
            ok=True,
            message=f"typed {text}",
            output={"echo": text, "text": text},
        )

    async def clipboard_set(text):
        return ActionResult(
            ok=True,
            message=f"copied {text}",
            output={"echo": text, "clipboard_text": text},
        )

    app.state.backend.keyboard_type = keyboard_type
    app.state.backend.clipboard_set = clipboard_set

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        typed = client.post("/v1/keyboard/type", json={"text": "typed-secret"})
        copied = client.put("/v1/clipboard/text", json={"text": "clip-secret"})

    assert typed.status_code == 200
    assert copied.status_code == 200
    serialized = json.dumps([typed.json(), copied.json()])
    assert "typed-secret" not in serialized
    assert "clip-secret" not in serialized
    assert typed.json()["message"] == "typed [redacted typed text]"
    assert typed.json()["output"]["text"]["redacted"] is True
    assert copied.json()["message"] == "copied [redacted clipboard text]"
    assert copied.json()["output"]["clipboard_text"]["redacted"] is True


def test_command_run_executes_under_input_lock(tmp_path) -> None:
    app = _app(tmp_path, local_token="dev")
    observed_locked = False

    async def run_command(command, timeout=30.0):
        nonlocal observed_locked
        observed_locked = app.state.input_lock.locked()
        return ActionResult(ok=True, output={"command": list(command)})

    app.state.backend.run_command = run_command

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post("/v1/commands/run", json={"command": ["true"]})

    assert response.status_code == 200
    assert observed_locked is True


def test_process_log_routes_sanitize_secret_bearing_tails(tmp_path) -> None:
    app = _app(tmp_path, local_token="dev")
    app.state.supervisor.log_dir.mkdir(parents=True)
    (app.state.supervisor.log_dir / "xvfb.log").write_text(
        "safe\nBearer log-secret\n",
    )
    (app.state.supervisor.log_dir / "xvfb.stderr.log").write_text(
        "artifact://logs/stderr-secret.txt\n",
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        logs = client.get("/v1/processes/xvfb/logs")
        stderr = client.get("/v1/processes/xvfb/stderr")
        errors = client.get("/v1/processes/xvfb/errors")

    assert logs.status_code == 200
    assert stderr.status_code == 200
    assert errors.status_code == 200
    assert logs.text == "safe\nBearer [redacted]"
    assert stderr.text == "[redacted]"
    assert errors.text == "[redacted]"
    assert "log-secret" not in logs.text
    assert "stderr-secret" not in stderr.text
    assert "stderr-secret" not in errors.text


def test_process_log_tail_query_is_bounded(tmp_path) -> None:
    app = _app(tmp_path, local_token="dev")
    app.state.supervisor.log_dir.mkdir(parents=True)
    (app.state.supervisor.log_dir / "xvfb.log").write_text(
        "\n".join(f"line-{index}" for index in range(5)),
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        valid = client.get("/v1/processes/xvfb/logs?tail=2")
        zero = client.get("/v1/processes/xvfb/logs?tail=0")
        negative = client.get("/v1/processes/xvfb/logs?tail=-1")
        too_large = client.get("/v1/processes/xvfb/logs?tail=1001")

    assert valid.status_code == 200
    assert valid.text == "line-3\nline-4"
    assert zero.status_code == 422
    assert negative.status_code == 422
    assert too_large.status_code == 422


def test_browser_and_app_routes_sanitize_reflected_urls_and_args(tmp_path) -> None:
    app = _app(tmp_path, local_token="dev")

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        browser = client.post(
            "/v1/browser/open-url",
            json={"url": "https://example.test/path?_modal_connect_token=url-secret"},
        )
        app_launch = client.post(
            "/v1/apps/launch",
            json={
                "command": "browser",
                "args": ["https://example.test/path?_modal_connect_token=arg-secret"],
            },
        )

    serialized = json.dumps({"browser": browser.json(), "app": app_launch.json()})
    assert browser.status_code == 200
    assert app_launch.status_code == 200
    assert "url-secret" not in serialized
    assert "arg-secret" not in serialized


def test_open_artifact_does_not_reflect_absolute_artifact_path(tmp_path) -> None:
    app = _app(tmp_path, local_token="dev")

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        write = client.put("/v1/artifacts/reports/result.txt", content=b"ok")
        response = client.post("/v1/apps/open-artifact", json={"path": "reports/result.txt"})

    serialized = json.dumps(response.json())
    assert write.status_code == 200
    assert response.status_code == 200
    assert str(app.state.settings.artifacts_dir) not in serialized
    assert "reports/result.txt" not in serialized


def test_daemon_error_details_sanitize_secret_bearing_values(tmp_path) -> None:
    app = _app(tmp_path, local_token="dev")

    @app.get("/v1/test/unsafe-error")
    async def unsafe_error():
        raise DaemonError(
            "failed with Bearer message-secret",
            code="unsafe_test",
            details={
                "stdout": "Bearer stdout-secret",
                "stderr": "artifact://logs/stderr-secret.txt",
                "nested": {"url": "https://example.test/?token=url-secret"},
            },
        )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.get("/v1/test/unsafe-error")

    serialized = json.dumps(response.json())
    assert response.status_code == 400
    assert "message-secret" not in serialized
    assert "stdout-secret" not in serialized
    assert "stderr-secret" not in serialized
    assert "url-secret" not in serialized
