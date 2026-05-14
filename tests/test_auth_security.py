from __future__ import annotations

import json
import logging
import sys

from fastapi.testclient import TestClient

from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.logging import JsonFormatter
from modal_computer_use.daemon.settings import DaemonSettings


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
