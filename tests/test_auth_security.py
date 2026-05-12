from __future__ import annotations

from fastapi.testclient import TestClient

from modal_computer_use.daemon.app import create_app
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


def test_query_connect_tokens_are_rejected_when_configured(tmp_path) -> None:
    app = _app(tmp_path, require_connect_user=False, reject_query_tokens=True)

    with TestClient(app) as client:
        response = client.get("/v1/version?_modal_connect_token=secret")

    assert response.status_code == 401
    assert response.json()["code"] == "query_token_rejected"


def test_require_connect_user_rejects_missing_verified_user_metadata(tmp_path) -> None:
    app = _app(tmp_path, require_connect_user=True, reject_query_tokens=True)

    with TestClient(app) as client:
        missing = client.get("/v1/version")
        present = client.get("/v1/version", headers={"X-Verified-User-Data": "{}"})

    assert missing.status_code == 401
    assert missing.json()["code"] == "connect_token_required"
    assert present.status_code == 200
