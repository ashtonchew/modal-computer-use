from __future__ import annotations

import asyncio
import gc
import inspect
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import modal_computer_use.namespaces as namespaces
from modal_computer_use.client import AsyncDaemonClient
from modal_computer_use.errors import AuthenticationError, DaemonHTTPError
from modal_computer_use.hot_session import AsyncHotSessionClient
from modal_computer_use.observations import AsyncObservationClient
from modal_computer_use.transports import (
    AsyncHotSessionTransport,
    AsyncHTTPTransport,
    AsyncObservationStreamTransport,
)

ASYNC_NAMESPACE_NAMES = (
    "actions",
    "apps",
    "artifacts",
    "browser",
    "clipboard",
    "commands",
    "debug",
    "display",
    "input",
    "keyboard",
    "lifecycle",
    "mouse",
    "processes",
    "recordings",
    "screenshots",
    "session",
    "windows",
)


@pytest.mark.asyncio
async def test_async_http_transport_reuses_client_and_injects_private_metadata(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/download":
            return httpx.Response(200, content=b"payload", request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    http_client = httpx.AsyncClient(
        base_url="https://daemon.example",
        transport=httpx.MockTransport(handler),
    )
    metadata_calls = 0

    def metadata() -> dict[str, str]:
        nonlocal metadata_calls
        metadata_calls += 1
        return {"X-Computer-Use-Fence": str(metadata_calls)}

    transport = AsyncHTTPTransport(
        "https://daemon.example",
        token="test-token",
        client=http_client,
        _metadata_headers=metadata,
    )
    async with AsyncDaemonClient(
        "https://daemon.example",
        transport=transport,
    ) as client:
        assert await client.get_json("/healthz") == {"ok": True}
        output = await client.download("/download", tmp_path / "nested" / "artifact.bin")

    assert output.read_bytes() == b"payload"
    assert len(requests) == 2
    assert requests[0].headers["authorization"] == "Bearer test-token"
    assert requests[0].headers["x-computer-use-fence"] == "1"
    assert requests[1].headers["x-computer-use-fence"] == "2"
    assert http_client.is_closed


@pytest.mark.asyncio
async def test_async_daemon_client_composes_cached_namespaces() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/mouse/move"
        return httpx.Response(200, json={"x": 10, "y": 20}, request=request)

    http_client = httpx.AsyncClient(
        base_url="https://daemon.example",
        transport=httpx.MockTransport(handler),
    )
    client = AsyncDaemonClient(
        "https://daemon.example",
        transport=AsyncHTTPTransport("https://daemon.example", client=http_client),
    )

    for name in ASYNC_NAMESPACE_NAMES:
        assert getattr(client, name) is getattr(client, name)
    point = await client.mouse.move(10, 20)
    assert (point.x, point.y) == (10, 20)

    await client.aclose()
    assert http_client.is_closed


@pytest.mark.asyncio
async def test_async_daemon_client_waits_until_ready(monkeypatch) -> None:
    responses = iter(({"ready": False}, {"ready": True}))
    client = AsyncDaemonClient("https://daemon.example")

    async def get_json(path: str, **_kwargs: object) -> dict[str, bool]:
        assert path == "/readyz"
        return next(responses)

    monkeypatch.setattr(client, "get_json", get_json)
    await client.wait_until_ready(timeout=1.0, interval=0.001)
    await client.aclose()


@pytest.mark.asyncio
async def test_async_daemon_client_readiness_timeout_is_sanitized(monkeypatch) -> None:
    client = AsyncDaemonClient("https://daemon.example")

    async def get_json(path: str, **_kwargs: object) -> dict[str, object]:
        assert path == "/readyz"
        return {"ready": False, "secret": "must-not-appear"}

    monkeypatch.setattr(client, "get_json", get_json)
    with pytest.raises(TimeoutError, match="daemon did not become ready") as exc:
        await client.wait_until_ready(timeout=0.001, interval=0.001)

    await client.aclose()
    assert "must-not-appear" not in str(exc.value)


@pytest.mark.asyncio
async def test_async_daemon_client_closes_child_connections(monkeypatch) -> None:
    closed: list[str] = []
    client = AsyncDaemonClient("https://daemon.example")
    hot_session = client.hot_session()
    observation = client.observation_stream()

    async def close_hot_session() -> None:
        closed.append("hot-session")

    async def close_observation() -> None:
        closed.append("observation")

    monkeypatch.setattr(hot_session, "aclose", close_hot_session)
    monkeypatch.setattr(observation, "aclose", close_observation)
    async def close_transport() -> None:
        closed.append("transport")

    monkeypatch.setattr(client.transport, "aclose", close_transport)
    await client.aclose()

    assert set(closed[:2]) == {"hot-session", "observation"}
    assert closed[2] == "transport"
    with pytest.raises(RuntimeError, match="closing or closed"):
        client.hot_session()


@pytest.mark.asyncio
async def test_async_daemon_client_close_finishes_before_propagating_cancellation(
    monkeypatch,
) -> None:
    close_started = asyncio.Event()
    permit_close = asyncio.Event()
    closed = asyncio.Event()
    client = AsyncDaemonClient("https://daemon.example")

    async def close_transport() -> None:
        close_started.set()
        await permit_close.wait()
        closed.set()

    monkeypatch.setattr(client.transport, "aclose", close_transport)
    task = asyncio.create_task(client.aclose())
    await close_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    permit_close.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


@pytest.mark.asyncio
async def test_async_daemon_client_does_not_retain_discarded_children() -> None:
    client = AsyncDaemonClient("https://daemon.example")
    client.hot_session()
    client.observation_stream()
    gc.collect()

    assert not tuple(client._children)
    await client.aclose()


@pytest.mark.asyncio
async def test_async_daemon_client_rejects_non_positive_readiness_interval() -> None:
    client = AsyncDaemonClient("https://daemon.example")
    with pytest.raises(ValueError, match="interval must be a positive finite number"):
        await client.wait_until_ready(interval=0)
    await client.aclose()


@pytest.mark.asyncio
async def test_async_daemon_client_local_defaults_to_loopback() -> None:
    client = AsyncDaemonClient.local(token="dev")
    assert client.base_url == "http://127.0.0.1:8080"
    assert client.transport.token is not None
    await client.aclose()


@pytest.mark.asyncio
async def test_async_http_transport_preserves_domain_errors() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth":
            return httpx.Response(401, request=request)
        return httpx.Response(
            409,
            json={"message": "conflict", "code": "state_conflict", "details": {"safe": True}},
            request=request,
        )

    http_client = httpx.AsyncClient(
        base_url="https://daemon.example",
        transport=httpx.MockTransport(handler),
    )
    async with AsyncHTTPTransport("https://daemon.example", client=http_client) as transport:
        with pytest.raises(AuthenticationError):
            await transport.request("GET", "/auth")
        with pytest.raises(DaemonHTTPError) as exc:
            await transport.request("POST", "/conflict")

    assert exc.value.status_code == 409
    assert exc.value.code == "state_conflict"
    assert exc.value.details == {"safe": True}


def test_async_namespaces_match_sync_public_methods_and_parameters() -> None:
    for async_name in namespaces.__all__:
        if not async_name.startswith("Async"):
            continue
        sync_name = async_name.removeprefix("Async")
        if not hasattr(namespaces, sync_name):
            continue
        sync_class = getattr(namespaces, sync_name)
        async_class = getattr(namespaces, async_name)
        sync_methods = {
            name: method
            for name, method in inspect.getmembers(sync_class, inspect.isfunction)
            if not name.startswith("_")
        }
        async_methods = {
            name: method
            for name, method in inspect.getmembers(async_class, inspect.isfunction)
            if not name.startswith("_")
        }
        assert async_methods.keys() == sync_methods.keys(), async_name
        for method_name, sync_method in sync_methods.items():
            async_method = async_methods[method_name]
            assert inspect.iscoroutinefunction(async_method), f"{async_name}.{method_name}"
            assert _parameters_without_self(async_method) == _parameters_without_self(sync_method)
            assert (
                inspect.signature(async_method).return_annotation
                == inspect.signature(sync_method).return_annotation
            )


@pytest.mark.asyncio
async def test_async_hot_session_serializes_exchanges_and_keeps_request_ids_distinct() -> None:
    websocket = _FakeHotWebSocket(auto_reply=True)
    transport = AsyncHotSessionTransport("https://daemon.example", websocket=websocket)
    client = AsyncHotSessionClient(transport)

    first, second = await asyncio.gather(client.ping(), client.ping())
    await client.aclose()

    assert first == {"request_id": "1"}
    assert second == {"request_id": "2"}
    assert websocket.max_receivers == 1
    assert websocket.closed


@pytest.mark.asyncio
async def test_async_hot_session_injects_metadata_when_connection_opens(monkeypatch) -> None:
    websocket = _FakeHotWebSocket(auto_reply=True)
    connect_kwargs: dict[str, Any] = {}

    async def fake_connect(*_args: Any, **kwargs: Any) -> _FakeHotWebSocket:
        connect_kwargs.update(kwargs)
        return websocket

    monkeypatch.setattr(
        "modal_computer_use.transports.async_hot_session.connect",
        fake_connect,
    )
    transport = AsyncHotSessionTransport(
        "https://daemon.example",
        token="test-token",
        _metadata_headers=lambda: {"X-Computer-Use-Fence": "lease-1"},
    )

    await transport.ping()
    await transport.aclose()

    assert connect_kwargs["additional_headers"] == {
        "Authorization": "Bearer test-token",
        "X-Computer-Use-Fence": "lease-1",
    }


@pytest.mark.asyncio
async def test_async_hot_session_cancellation_poison_closes_without_replay() -> None:
    websocket = _FakeHotWebSocket(auto_reply=False)
    transport = AsyncHotSessionTransport("https://daemon.example", websocket=websocket)

    task = asyncio.create_task(transport.request("run_actions", {"actions": []}))
    await websocket.mutation_sent.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with pytest.raises(DaemonHTTPError) as exc:
        await transport.request("run_actions", {"actions": []})
    assert exc.value.code == "hot_session_poisoned"
    assert len(websocket.sent) == 1
    assert websocket.closed


@pytest.mark.asyncio
async def test_distinct_async_hot_clients_progress_and_fail_independently() -> None:
    blocked_websocket = _FakeHotWebSocket(auto_reply=False)
    healthy_websocket = _FakeHotWebSocket(auto_reply=True)
    blocked = AsyncHotSessionClient(
        AsyncHotSessionTransport("https://blocked.example", websocket=blocked_websocket)
    )
    healthy = AsyncHotSessionClient(
        AsyncHotSessionTransport("https://healthy.example", websocket=healthy_websocket)
    )

    uncertain_mutation = asyncio.create_task(
        blocked.run_actions([{"type": "click", "x": 1, "y": 2}])
    )
    await blocked_websocket.mutation_sent.wait()
    assert await asyncio.wait_for(healthy.ping(), timeout=1.0) == {"request_id": "1"}

    uncertain_mutation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await uncertain_mutation
    assert blocked_websocket.closed
    assert not healthy_websocket.closed
    assert await asyncio.wait_for(healthy.ping(), timeout=1.0) == {"request_id": "2"}

    await healthy.aclose()
    assert healthy_websocket.closed


@pytest.mark.asyncio
async def test_async_observation_client_uses_one_receiver_and_correlates_action_frame() -> None:
    websocket = _FakeObservationWebSocket()
    transport = AsyncObservationStreamTransport(
        "https://daemon.example",
        websocket=websocket,
    )
    client = AsyncObservationClient(
        transport,
        frame_encoding="json-binary",
    )

    initial = await client.start(drain_initial_frame=True)
    result = await client._experimental_act_until_visual_change(
        actions=[{"type": "click", "x": 1, "y": 2}],
    )
    await client.aclose()

    assert initial is not None and initial.payload == b"initial"
    assert result.frame.payload == b"changed"
    assert result.action_id == result.frame.metadata["id"]
    assert result.frame.metadata["causal_frame"] is True
    assert websocket.max_receivers == 1
    assert websocket.closed


@pytest.mark.asyncio
async def test_async_observation_receiver_failure_is_terminal_and_fails_fast() -> None:
    websocket = _FakeObservationWebSocket()
    transport = AsyncObservationStreamTransport(
        "https://daemon.example",
        websocket=websocket,
        timeout=5.0,
    )
    await transport.start({"frame_encoding": "json-binary"})
    await transport.receive_frame()

    websocket.fail_receive(RuntimeError("synthetic receive failure"))
    receiver = transport._receiver_task
    assert receiver is not None
    await asyncio.wait_for(receiver, timeout=1.0)

    with pytest.raises(DaemonHTTPError) as exc:
        await asyncio.wait_for(transport.pause(), timeout=1.0)
    assert exc.value.code == "observation_stream_failed"
    assert websocket.closed


@pytest.mark.asyncio
async def test_async_observation_mutation_cancellation_poison_closes_without_replay() -> None:
    websocket = _FakeObservationWebSocket(block_mutation=True)
    transport = AsyncObservationStreamTransport(
        "https://daemon.example",
        websocket=websocket,
    )
    await transport.start({"frame_encoding": "json-binary"})
    await transport.receive_frame()

    task = asyncio.create_task(
        transport.run_actions_capture({"actions": [{"type": "click", "x": 1, "y": 2}]})
    )
    await websocket.mutation_sent.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with pytest.raises(DaemonHTTPError) as exc:
        await transport.run_actions_capture({"actions": []})
    assert exc.value.code == "observation_stream_poisoned"
    assert [message["op"] for message in websocket.sent].count("run_actions_capture") == 1
    assert websocket.closed


def _parameters_without_self(method: Any) -> list[tuple[str, inspect._ParameterKind, Any]]:
    parameters = list(inspect.signature(method).parameters.values())[1:]
    return [(parameter.name, parameter.kind, parameter.default) for parameter in parameters]


class _FakeHotWebSocket:
    def __init__(self, *, auto_reply: bool) -> None:
        self.auto_reply = auto_reply
        self.incoming: asyncio.Queue[str | bytes] = asyncio.Queue()
        self.incoming.put_nowait(json.dumps({"type": "ready"}))
        self.sent: list[dict[str, Any]] = []
        self.mutation_sent = asyncio.Event()
        self.closed = False
        self.receivers = 0
        self.max_receivers = 0

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        if message["op"] == "run_actions":
            self.mutation_sent.set()
        if self.auto_reply:
            self.incoming.put_nowait(
                json.dumps(
                    {
                        "id": message["id"],
                        "type": "result",
                        "result": {"request_id": message["id"]},
                    }
                )
            )

    async def recv(self) -> str | bytes:
        self.receivers += 1
        self.max_receivers = max(self.max_receivers, self.receivers)
        try:
            return await self.incoming.get()
        finally:
            self.receivers -= 1

    async def close(self) -> None:
        self.closed = True


class _FakeObservationWebSocket:
    def __init__(self, *, block_mutation: bool = False) -> None:
        self.block_mutation = block_mutation
        self.incoming: asyncio.Queue[str | bytes | BaseException] = asyncio.Queue()
        self.incoming.put_nowait(json.dumps({"type": "ready"}))
        self.sent: list[dict[str, Any]] = []
        self.mutation_sent = asyncio.Event()
        self.closed = False
        self.receivers = 0
        self.max_receivers = 0
        self._sequence = 0

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        request_id = message["id"]
        op = message["op"]
        if op == "start":
            self.incoming.put_nowait(json.dumps({"type": "started", "id": request_id}))
            self._put_frame(request_id=None, payload=b"initial", causal=False)
        elif op in {"run_actions_capture", "run_actions_observe_change"}:
            self.mutation_sent.set()
            if not self.block_mutation:
                self._put_frame(request_id=request_id, payload=b"changed", causal=True)
        else:
            self.incoming.put_nowait(json.dumps({"type": "result", "id": request_id}))

    def _put_frame(self, *, request_id: str | None, payload: bytes, causal: bool) -> None:
        self._sequence += 1
        metadata: dict[str, Any] = {
            "type": "frame",
            "seq": self._sequence,
            "kind": "full",
            "width": 1,
            "height": 1,
            "format": "png",
        }
        if request_id is not None:
            metadata.update(
                {
                    "id": request_id,
                    "action_id": request_id,
                    "causal_frame": causal,
                    "change_detected": True,
                    "action_result": {"ok": True},
                }
            )
        self.incoming.put_nowait(json.dumps(metadata))
        self.incoming.put_nowait(payload)

    async def recv(self) -> str | bytes:
        self.receivers += 1
        self.max_receivers = max(self.max_receivers, self.receivers)
        try:
            item = await self.incoming.get()
            if isinstance(item, BaseException):
                raise item
            return item
        finally:
            self.receivers -= 1

    def fail_receive(self, error: BaseException) -> None:
        self.incoming.put_nowait(error)

    async def close(self) -> None:
        self.closed = True
