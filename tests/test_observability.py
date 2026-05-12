from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi.testclient import TestClient

from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.observability import get_tracer
from modal_computer_use.transports.http import HTTPTransport


def test_otel_disabled_does_not_import_optional_package(monkeypatch) -> None:
    def fail_import(name: str) -> object:
        raise AssertionError(f"unexpected import: {name}")

    monkeypatch.setattr(importlib, "import_module", fail_import)
    tracer = get_tracer(enabled=False)

    with tracer.span("disabled.test") as span:
        span.set_attribute("safe", True)


def test_otel_enabled_without_package_is_noop(monkeypatch) -> None:
    def missing_import(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", missing_import)
    tracer = get_tracer(enabled=True)

    assert tracer.enabled is True
    assert tracer.available is False
    with tracer.span("missing.test") as span:
        span.set_attribute("safe", True)


@dataclass
class _CapturedSpan:
    name: str
    attributes: dict[str, Any]
    error: BaseException | None = None

    def __enter__(self) -> _CapturedSpan:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def record_exception(self, exc: BaseException) -> None:
        self.error = exc


@dataclass
class _CapturedTracer:
    spans: list[_CapturedSpan] = field(default_factory=list)

    def span(self, name: str, attributes: dict[str, object] | None = None) -> _CapturedSpan:
        span = _CapturedSpan(name=name, attributes=dict(attributes or {}))
        self.spans.append(span)
        return span


def test_daemon_route_span_uses_path_not_query_or_token(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )
    tracer = _CapturedTracer()
    app.state.tracer = tracer

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.get("/v1/version?token=secret")

    assert response.status_code == 200
    route_span = next(span for span in tracer.spans if span.name == "daemon.route")
    assert route_span.attributes["http.route"] == "/v1/version"
    assert "secret" not in str(route_span.attributes)
    assert route_span.attributes["http.status_code"] == 200


def test_sdk_request_span_uses_route_not_query_or_authorization(monkeypatch) -> None:
    tracer = _CapturedTracer()
    monkeypatch.setattr("modal_computer_use.transports.http.get_tracer", lambda **_: tracer)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-token"
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(
        base_url="http://daemon.local",
        transport=httpx.MockTransport(handler),
    )
    transport = HTTPTransport(
        "http://daemon.local",
        token="secret-token",
        client=client,
    )

    response = transport.request("GET", "/v1/version", params={"token": "secret"})

    assert response.json() == {"ok": True}
    sdk_span = tracer.spans[0]
    assert sdk_span.name == "sdk.request"
    assert sdk_span.attributes["http.route"] == "/v1/version"
    assert "secret" not in str(sdk_span.attributes)
