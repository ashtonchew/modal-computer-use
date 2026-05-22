from __future__ import annotations

import importlib
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi.testclient import TestClient

from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.observability import OptionalSpan, get_tracer
from modal_computer_use.redaction import RedactedException
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


class _RawContextManager:
    def __init__(self) -> None:
        self.span = _CapturedSpan(name="raw", attributes={})
        self.exit_args: tuple[object, object, object] | None = None

    def __enter__(self) -> _CapturedSpan:
        return self.span

    def __exit__(self, *args: object) -> None:
        self.exit_args = args
        return None


def test_optional_span_redacts_recorded_and_context_exceptions() -> None:
    context = _RawContextManager()
    span = OptionalSpan(context)

    with span as active:
        active.record_exception(RuntimeError("failed for Bearer secret-token"))

    assert isinstance(context.span.error, RedactedException)
    assert "secret-token" not in str(context.span.error)

    context = _RawContextManager()
    try:
        with OptionalSpan(context):
            raise RuntimeError("failed for Bearer secret-token")
    except RuntimeError:
        pass

    assert context.exit_args is not None
    _, exc, traceback = context.exit_args
    assert isinstance(exc, RedactedException)
    assert traceback is None
    assert "secret-token" not in str(exc)


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


def test_daemon_route_span_uses_template_for_artifact_path_params(tmp_path) -> None:
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
        write = client.put("/v1/artifacts/private/secret-name.png", content=b"ok")
        tracer.spans.clear()
        response = client.get("/v1/artifacts/private/secret-name.png")

    assert write.status_code == 200
    assert response.status_code == 200
    route_span = next(span for span in tracer.spans if span.name == "daemon.route")
    assert route_span.attributes["http.route"] == "/v1/artifacts/{path:path}"
    assert "secret-name" not in str(route_span.attributes)


def test_daemon_route_span_records_safe_error_code_only(tmp_path) -> None:
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
        response = client.get("/v1/artifacts/private/secret-name.png")

    assert response.status_code == 404
    route_span = next(span for span in tracer.spans if span.name == "daemon.route")
    assert route_span.attributes["error.code"] == "not_found"
    assert "secret-name" not in str(route_span.attributes)


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


def test_http_transport_passes_http2_to_default_client(monkeypatch) -> None:
    calls = []

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)

        def close(self) -> None:
            pass

    monkeypatch.setattr("modal_computer_use.transports.http.httpx.Client", FakeClient)

    transport = HTTPTransport("https://daemon.example", http2=True)
    transport.close()

    assert calls == [
        {
            "base_url": "https://daemon.example",
            "timeout": 30.0,
            "http2": True,
        }
    ]


def test_sdk_request_span_strips_inline_query_from_path(monkeypatch) -> None:
    tracer = _CapturedTracer()
    monkeypatch.setattr("modal_computer_use.transports.http.get_tracer", lambda **_: tracer)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/version"
        assert request.url.query == b"token=secret"
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(
        base_url="http://daemon.local",
        transport=httpx.MockTransport(handler),
    )
    transport = HTTPTransport("http://daemon.local", client=client)

    response = transport.request("GET", "/v1/version?token=secret")

    assert response.json() == {"ok": True}
    sdk_span = tracer.spans[0]
    assert sdk_span.attributes["http.route"] == "/v1/version"
    assert "secret" not in str(sdk_span.attributes)


def test_sdk_request_span_templates_artifact_paths(monkeypatch) -> None:
    tracer = _CapturedTracer()
    monkeypatch.setattr("modal_computer_use.transports.http.get_tracer", lambda **_: tracer)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/artifacts/private/secret-name.png"
        return httpx.Response(200, content=b"ok")

    client = httpx.Client(
        base_url="http://daemon.local",
        transport=httpx.MockTransport(handler),
    )
    transport = HTTPTransport("http://daemon.local", client=client)

    response = transport.request("GET", "/v1/artifacts/private/secret-name.png")

    assert response.content == b"ok"
    sdk_span = tracer.spans[0]
    assert sdk_span.attributes["http.route"] == "/v1/artifacts/{path:path}"
    assert "secret-name" not in str(sdk_span.attributes)


def test_sdk_request_span_records_safe_error_code(monkeypatch) -> None:
    tracer = _CapturedTracer()
    monkeypatch.setattr("modal_computer_use.transports.http.get_tracer", lambda **_: tracer)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "code": "unsafe_artifact_path",
                "message": "unsafe path private/secret-name.png",
                "details": {},
            },
        )

    client = httpx.Client(
        base_url="http://daemon.local",
        transport=httpx.MockTransport(handler),
    )
    transport = HTTPTransport("http://daemon.local", client=client)

    with suppress(Exception):
        transport.request("GET", "/v1/artifacts/private/secret-name.png")

    sdk_span = tracer.spans[0]
    assert sdk_span.attributes["error.code"] == "unsafe_artifact_path"
    assert "secret-name" not in str(sdk_span.attributes)
