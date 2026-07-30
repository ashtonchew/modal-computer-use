from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from contextlib import suppress
from time import perf_counter
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from modal_computer_use.errors import AuthenticationError, DaemonHTTPError

from .metadata import MetadataHeaders, resolve_metadata_headers
from .observation import (
    FrameEncoding,
    ObservationFrame,
    _decode_frame_envelope,
    _frame_encoding_from_payload,
)
from .websocket_url import daemon_websocket_url

_STOP = object()


class AsyncObservationStreamTransport:
    """Lazy async observation channel with a single WebSocket receiver."""

    _MUTATING_OPS = frozenset({"run_actions_capture", "run_actions_observe_change"})

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        websocket: ClientConnection | None = None,
        connect_attempts: int = 1,
        connect_backoff_seconds: float = 0.25,
        _metadata_headers: MetadataHeaders | None = None,
    ) -> None:
        if connect_attempts < 1:
            raise ValueError("connect_attempts must be at least 1")
        if connect_backoff_seconds < 0:
            raise ValueError("connect_backoff_seconds must be non-negative")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.connect_attempts = connect_attempts
        self.connect_backoff_seconds = connect_backoff_seconds
        self.setup_attempts = 0
        self.setup_elapsed_ms = 0.0
        self.setup_retry_errors: list[dict[str, str]] = []
        self._metadata_headers = _metadata_headers
        self._websocket = websocket
        self._ready = False
        self._closed = False
        self._poisoned = False
        self._receiver_error: DaemonHTTPError | None = None
        self._next_id = 1
        self._frame_encoding: FrameEncoding = "json-binary"
        self._transport_timing = False
        self._connect_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._consumer_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._pending_frames: deque[ObservationFrame] = deque()
        self._frame_queue: asyncio.Queue[ObservationFrame | BaseException | object] = (
            asyncio.Queue()
        )
        self._receiver_task: asyncio.Task[None] | None = None
        self._probe_parts: dict[str, tuple[dict[str, Any], bytes]] = {}
        self._timed_frames: dict[Any, ObservationFrame] = {}

    async def __aenter__(self) -> AsyncObservationStreamTransport:
        await self._ensure_connected()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._closed = True
        receiver, self._receiver_task = self._receiver_task, None
        if receiver is not None and receiver is not asyncio.current_task():
            receiver.cancel()
            with suppress(asyncio.CancelledError):
                await receiver
        websocket, self._websocket = self._websocket, None
        self._ready = False
        if websocket is not None:
            await websocket.close()
        self._fail_pending(
            DaemonHTTPError("observation stream is closed", code="observation_stream_closed")
        )

    async def frames(self, payload: dict[str, Any]) -> AsyncIterator[ObservationFrame]:
        await self.start(payload)
        async with self._consumer_lock:
            while True:
                try:
                    yield await self._receive_queued_frame()
                except StopAsyncIteration:
                    return

    async def start(self, payload: dict[str, Any]) -> None:
        self._frame_encoding = _frame_encoding_from_payload(payload)
        self._transport_timing = bool(payload.get("transport_timing"))
        result = await self._request("start", payload)
        if not isinstance(result, dict) or result.get("type") != "started":
            raise DaemonHTTPError(
                "unexpected observation stream start response",
                code="observation_stream_protocol_error",
            )

    async def stop(self) -> None:
        await self._request("stop", {})

    async def pause(self) -> None:
        await self._request("pause", {})

    async def resume(self) -> None:
        await self._request("resume", {})

    async def request_frame(self) -> None:
        frame = await self._request("capture_now", {})
        if isinstance(frame, ObservationFrame):
            await self._frame_queue.put(frame)

    async def run_actions_capture(self, payload: dict[str, Any]) -> None:
        frame = await self._request("run_actions_capture", payload)
        if isinstance(frame, ObservationFrame):
            await self._frame_queue.put(frame)

    async def run_actions_observe_change(self, payload: dict[str, Any]) -> None:
        frame = await self._request("run_actions_observe_change", payload)
        if isinstance(frame, ObservationFrame):
            await self._frame_queue.put(frame)

    async def run_actions_observe_change_and_recv(
        self,
        payload: dict[str, Any],
        *,
        transport_timing: bool = False,
    ) -> ObservationFrame:
        del transport_timing  # Receiver attaches server timing according to stream configuration.
        frame = await self._request("run_actions_observe_change", payload)
        if not isinstance(frame, ObservationFrame):
            raise DaemonHTTPError(
                "observation action did not return a frame",
                code="observation_stream_protocol_error",
            )
        return frame

    async def configure(self, payload: dict[str, Any]) -> None:
        await self._request("configure", payload)

    async def transport_probe(
        self,
        *,
        size_bytes: int,
        frame_encoding: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"size_bytes": size_bytes}
        if frame_encoding is not None:
            payload["frame_encoding"] = frame_encoding
        result = await self._request("transport_probe", payload)
        if not isinstance(result, dict):
            raise DaemonHTTPError(
                "unexpected observation transport probe response",
                code="observation_stream_protocol_error",
            )
        return result

    async def recv_frame_with_timing(self) -> ObservationFrame:
        return await self.receive_frame(transport_timing=True)

    async def receive_frame(self, *, transport_timing: bool = False) -> ObservationFrame:
        del transport_timing
        async with self._consumer_lock:
            return await self._receive_queued_frame()

    async def _receive_queued_frame(self) -> ObservationFrame:
        if self._pending_frames:
            return self._pending_frames.popleft()
        item = await self._frame_queue.get()
        if item is _STOP:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, ObservationFrame)
        return item

    async def _request(self, op: str, payload: dict[str, Any]) -> Any:
        websocket = await self._ensure_connected()
        loop = asyncio.get_running_loop()
        request_id = str(self._next_id)
        self._next_id += 1
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        mutating = op in self._MUTATING_OPS
        possibly_sent = False
        try:
            async with self._send_lock:
                possibly_sent = mutating
                await websocket.send(json.dumps({"id": request_id, "op": op, "payload": payload}))
            return await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            if possibly_sent:
                await self._poison_and_close()
            raise
        except Exception:
            self._pending.pop(request_id, None)
            if possibly_sent:
                await self._poison_and_close()
            raise

    async def _ensure_connected(self) -> ClientConnection:
        if self._receiver_error is not None:
            raise self._receiver_error
        if self._poisoned:
            raise DaemonHTTPError(
                "observation stream is unusable after an uncertain mutation",
                code="observation_stream_poisoned",
            )
        if self._closed:
            raise DaemonHTTPError(
                "observation stream is closed",
                code="observation_stream_closed",
            )
        if self._websocket is not None and self._ready:
            return self._websocket
        async with self._connect_lock:
            if self._websocket is None:
                self._websocket = await self._connect_with_retries()
            if not self._ready:
                try:
                    ready = await asyncio.wait_for(self._websocket.recv(), timeout=self.timeout)
                except ConnectionClosed as exc:
                    if (
                        getattr(exc, "rcvd", None) is not None
                        and getattr(exc.rcvd, "code", None) == 1008
                    ):
                        raise AuthenticationError(
                            "observation stream authentication failed"
                        ) from exc
                    raise
                if not isinstance(ready, str):
                    raise DaemonHTTPError(
                        "observation stream did not return a ready frame",
                        code="observation_stream_failed",
                    )
                self._ready = True
                self._receiver_task = asyncio.create_task(self._receiver_loop())
        assert self._websocket is not None
        return self._websocket

    async def _connect_with_retries(self) -> ClientConnection:
        started = perf_counter()
        retry_errors: list[dict[str, str]] = []
        for attempt in range(1, self.connect_attempts + 1):
            try:
                websocket = await self._connect()
                self.setup_attempts = attempt
                self.setup_elapsed_ms = (perf_counter() - started) * 1000
                self.setup_retry_errors = retry_errors
                return websocket
            except Exception as exc:
                self.setup_attempts = attempt
                self.setup_elapsed_ms = (perf_counter() - started) * 1000
                self.setup_retry_errors = retry_errors
                if not _retryable_setup_error(exc) or attempt >= self.connect_attempts:
                    raise
                retry_errors.append({"type": type(exc).__name__})
                await asyncio.sleep(self.connect_backoff_seconds * attempt)
        raise DaemonHTTPError("observation stream setup failed", code="observation_stream_failed")

    async def _connect(self) -> ClientConnection:
        headers = resolve_metadata_headers(self._metadata_headers)
        if self.token:
            headers.setdefault("Authorization", f"Bearer {self.token}")
        return await connect(
            daemon_websocket_url(self.base_url, "/v1/observations/stream"),
            additional_headers=headers or None,
            open_timeout=self.timeout,
            max_size=8 * 1024 * 1024,
            compression=None,
        )

    async def _receiver_loop(self) -> None:
        assert self._websocket is not None
        try:
            while True:
                raw = await self._websocket.recv()
                await self._dispatch_incoming(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = DaemonHTTPError(
                "observation stream receiver failed",
                code="observation_stream_failed",
                details={"type": type(exc).__name__},
            )
            self._receiver_error = error
            self._ready = False
            websocket, self._websocket = self._websocket, None
            self._fail_pending(error)
            await self._frame_queue.put(error)
            if websocket is not None:
                with suppress(Exception):
                    await websocket.close()

    async def _dispatch_incoming(self, raw: str | bytes) -> None:
        payload: bytes | None = None
        if isinstance(raw, bytes):
            data, payload = _decode_frame_envelope(raw)
        else:
            data = json.loads(raw)
        if not isinstance(data, dict):
            raise DaemonHTTPError(
                "unexpected observation stream frame",
                code="observation_stream_protocol_error",
            )
        kind = data.get("type")
        if kind == "error":
            error = _observation_error(data)
            self._resolve(data.get("id"), error=error)
            return
        if kind == "transport_probe":
            if payload is None:
                assert self._websocket is not None
                raw_payload = await self._websocket.recv()
                if not isinstance(raw_payload, bytes):
                    raise DaemonHTTPError(
                        "observation transport probe payload missing",
                        code="observation_stream_protocol_error",
                    )
                payload = raw_payload
            request_id = data.get("id")
            if isinstance(request_id, str):
                self._probe_parts[request_id] = (data, payload)
            return
        if kind == "transport_timing":
            request_id = data.get("id")
            if isinstance(request_id, str) and request_id in self._probe_parts:
                metadata, probe_payload = self._probe_parts.pop(request_id)
                self._resolve(
                    request_id,
                    value={
                        "size_bytes": len(probe_payload),
                        "requested_size_bytes": metadata.get("size_bytes"),
                        "frame_encoding": metadata.get("frame_encoding"),
                        "server_emit_timing_ms": data.get("server_emit_timing_ms"),
                    },
                )
                return
            frame = self._timed_frames.pop(data.get("seq"), None)
            if frame is not None:
                frame = ObservationFrame(
                    payload=frame.payload,
                    metadata=frame.metadata,
                    transport_timing=data,
                )
                await self._deliver_frame(frame)
            return
        if kind in {"frame", "unchanged"}:
            if kind == "frame" and payload is None:
                assert self._websocket is not None
                raw_payload = await self._websocket.recv()
                if not isinstance(raw_payload, bytes):
                    raise DaemonHTTPError(
                        "observation binary payload missing",
                        code="observation_stream_protocol_error",
                    )
                payload = raw_payload
            frame = ObservationFrame(
                payload=None if kind == "unchanged" else payload,
                metadata=data,
                transport_timing={"server_emit_timing_ms": data["server_emit_timing_ms"]}
                if isinstance(data.get("server_emit_timing_ms"), dict)
                else None,
            )
            if self._transport_timing and frame.transport_timing is None:
                self._timed_frames[data.get("seq")] = frame
            else:
                await self._deliver_frame(frame)
            return
        if kind == "stopped":
            await self._frame_queue.put(_STOP)
            return
        request_id = data.get("id")
        if isinstance(request_id, str):
            self._resolve(request_id, value=data)
            return
        raise DaemonHTTPError(
            "unexpected observation stream message",
            code="observation_stream_protocol_error",
        )

    async def _deliver_frame(self, frame: ObservationFrame) -> None:
        request_id = frame.metadata.get("id")
        if isinstance(request_id, str) and request_id in self._pending:
            self._resolve(request_id, value=frame)
        else:
            await self._frame_queue.put(frame)

    def _resolve(
        self,
        request_id: Any,
        *,
        value: Any = None,
        error: BaseException | None = None,
    ) -> None:
        if not isinstance(request_id, str):
            return
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(value)

    def _fail_pending(self, error: BaseException) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(error)

    async def _poison_and_close(self) -> None:
        self._poisoned = True
        close_task = asyncio.create_task(self.aclose())
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.shield(close_task)


def _observation_error(message: dict[str, Any]) -> DaemonHTTPError:
    error = message.get("error")
    if not isinstance(error, dict):
        return DaemonHTTPError(
            "observation stream request failed",
            code="observation_stream_error",
        )
    return DaemonHTTPError(
        str(error.get("message") or "observation stream request failed"),
        code=error.get("code")
        if isinstance(error.get("code"), str)
        else "observation_stream_error",
        details=error.get("details") if isinstance(error.get("details"), dict) else None,
    )


def _retryable_setup_error(exc: Exception) -> bool:
    if isinstance(exc, AuthenticationError | DaemonHTTPError):
        return False
    return isinstance(exc, TimeoutError | OSError | ConnectionError)
