from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from modal_computer_use.models import SandboxRef


def _load_example():
    path = Path(__file__).resolve().parents[2] / "examples" / "modal_session_broker.py"
    spec = importlib.util.spec_from_file_location("modal_session_broker_example", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


broker = _load_example()


class FakeManager:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.attached: list[str] = []
        self.terminated: list[str] = []
        self.refs = [
            SandboxRef(
                sandbox_id="sb-listed",
                app_name="modal-computer-use",
                run_id="run-listed",
                owner="owner-a",
                created_at=datetime(2026, 5, 29, tzinfo=UTC),
                status="ready",
            )
        ]

    def create(self, **kwargs):
        self.created.append(kwargs)
        return FakeComputer(
            sandbox_id="sb-created",
            run_id=kwargs["config"].run_id,
            owner=kwargs["owner"],
            token="created-token",
        )

    def attach(self, sandbox_id: str):
        self.attached.append(sandbox_id)
        return FakeComputer(sandbox_id=sandbox_id, run_id="run-attached", token="attach-token")

    def list(self, *, owner: str | None = None):
        return [ref for ref in self.refs if owner is None or ref.owner == owner]

    def terminate(self, sandbox_id: str) -> None:
        self.terminated.append(sandbox_id)


def test_session_broker_is_explicitly_privileged_single_trust_domain() -> None:
    assert "Privileged single-trust-domain" in broker.__doc__
    assert "does not perform application authentication" in broker.__doc__


class FakeComputer:
    def __init__(
        self,
        *,
        sandbox_id: str,
        run_id: str | None,
        owner: str | None = None,
        token: str | None,
    ) -> None:
        self.client = SimpleNamespace(
            base_url="https://daemon.example.modal.host",
            transport=SimpleNamespace(token=token),
        )
        self._metadata = SandboxRef(
            sandbox_id=sandbox_id,
            app_name="modal-computer-use",
            run_id=run_id,
            owner=owner,
            created_at=datetime(2026, 5, 29, tzinfo=UTC),
            status="ready",
        )
        self.detached = False

    def metadata(self):
        return self._metadata

    def detach(self) -> None:
        self.detached = True


def test_session_broker_create_session_returns_direct_daemon_endpoint_without_token_by_default():
    manager = FakeManager()
    client = TestClient(
        broker.build_session_broker_app(
            broker.SessionBrokerService(manager=manager)  # type: ignore[arg-type]
        )
    )

    response = client.post(
        "/sessions",
        json={
            "run_id": "run-created",
            "owner": "owner-a",
            "modal_region": "us-west",
            "resource_profile": "browser",
            "browser": "chromium",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["sandbox_id"] == "sb-created"
    assert payload["daemon_base_url"] == "https://daemon.example.modal.host"
    assert payload["daemon_token"] is None
    created_config = manager.created[0]["config"]
    assert created_config.runtime.modal_region == "us-west"
    assert created_config.resources.profile == "browser"
    assert created_config.browser.kind == "chromium"


def test_session_broker_can_list_get_with_token_and_terminate():
    manager = FakeManager()
    client = TestClient(
        broker.build_session_broker_app(
            broker.SessionBrokerService(manager=manager)  # type: ignore[arg-type]
        )
    )

    list_response = client.get("/sessions?owner=owner-a")
    get_response = client.get("/sessions/sb-attached?include_daemon_token=true")
    delete_response = client.delete("/sessions/sb-attached")

    assert list_response.status_code == 200
    assert list_response.json()["sessions"][0]["sandbox_id"] == "sb-listed"
    assert get_response.status_code == 200
    expected_token = "attach-" + "token"
    assert get_response.json()["daemon_token"] == expected_token
    assert delete_response.status_code == 204
    assert manager.attached == ["sb-attached"]
    assert manager.terminated == ["sb-attached"]
