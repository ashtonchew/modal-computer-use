from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from modal_computer_use.errors import AuthenticationError, DaemonHTTPError

from .metadata import MetadataHeaders, resolve_metadata_headers
from .websocket_url import daemon_websocket_url

_OPERATION_SEQUENCE_HEADER = "x-computer-use-operation-sequence"


def _is_daemon_error(message: dict[str, Any] | None) -> bool:
    if message is None or message.get("type") != "error":
        return False
    error = message.get("error")
    return bool(
        isinstance(error, dict)
        and isinstance(error.get("code"), str)
        and isinstance(error.get("message"), str)
    )


@dataclass(frozen=True)
class AsyncHotSessionBinaryResult:
    payload: bytes
    headers: Mapping[str, str]
    result: dict[str, Any] | None
    content_type: str | None


class AsyncHotSessionTransport:
    """Lazy native-async transport for the persistent hot-session protocol."""

    _MUTATING_OPS = frozenset({"run_actions", "run_raw_screenshot"})

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        websocket: ClientConnection | None = None,
        _metadata_headers: MetadataHeaders | None = None,
        _mutation_executor: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._metadata_headers = _metadata_headers
        self._mutation_executor = _mutation_executor
        self._ids = itertools.count(1)
        self._connect_lock = asyncio.Lock()
        self._exchange_lock = asyncio.Lock()
        self._websocket = websocket
        self._ready = False
        self._closed = False
        self._poisoned = False
        self._framing_tainted = False
        self._close_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> AsyncHotSessionTransport:
        await self._ensure_connected()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._closed = True
        websocket, self._websocket = self._websocket, None
        self._ready = False
        self._framing_tainted = False
        if websocket is not None:
            await websocket.close()

    async def ping(self) -> dict[str, Any]:
        return await self.request("ping", {})

    async def request(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        if op in self._MUTATING_OPS and self._mutation_executor is not None:
            return await self._mutation_executor(
                lambda metadata: self._request(op, payload, metadata=metadata)
            )
        return await self._request(op, payload)

    async def _request(
        self,
        op: str,
        payload: dict[str, Any],
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        async with self._exchange_lock:
            message = await self._exchange(op, payload, metadata=metadata)
            if message.get("type") == "error":
                self._raise_hot_error(message)
        if message.get("type") != "result":
            raise DaemonHTTPError(
                "unexpected hot session response",
                code="hot_session_protocol_error",
            )
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    async def request_binary(self, op: str, payload: dict[str, Any]) -> AsyncHotSessionBinaryResult:
        if op in self._MUTATING_OPS and self._mutation_executor is not None:
            return await self._mutation_executor(
                lambda metadata: self._request_binary(op, payload, metadata=metadata)
            )
        return await self._request_binary(op, payload)

    async def _request_binary(
        self,
        op: str,
        payload: dict[str, Any],
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> AsyncHotSessionBinaryResult:
        async with self._exchange_lock:
            message = await self._exchange(
                op, payload, receive_binary=True, metadata=metadata
            )
            if message.get("type") == "error":
                self._raise_hot_error(message)
            if message.get("type") != "binary":
                raise DaemonHTTPError(
                    "unexpected hot session response",
                    code="hot_session_protocol_error",
                )
            frame = message.pop("_binary_payload", None)
        if not isinstance(frame, bytes):
            raise DaemonHTTPError(
                "hot session binary payload missing",
                code="hot_session_protocol_error",
            )
        headers = message.get("headers")
        result = message.get("result")
        return AsyncHotSessionBinaryResult(
            payload=frame,
            headers=headers if isinstance(headers, dict) else {},
            result=result if isinstance(result, dict) else None,
            content_type=message.get("content_type")
            if isinstance(message.get("content_type"), str)
            else None,
        )

    async def _ensure_connected(self) -> ClientConnection:
        if self._poisoned:
            raise DaemonHTTPError(
                "hot session is unusable after an uncertain mutation",
                code="hot_session_poisoned",
            )
        if self._closed:
            raise DaemonHTTPError("hot session is closed", code="hot_session_closed")
        if self._websocket is not None and self._ready and not self._framing_tainted:
            return self._websocket
        async with self._connect_lock:
            if self._websocket is not None and self._framing_tainted:
                await self._discard_connection()
            if self._websocket is None:
                self._websocket = await self._connect()
            if not self._ready:
                try:
                    ready = await asyncio.wait_for(self._websocket.recv(), timeout=self.timeout)
                except ConnectionClosed as exc:
                    await self._discard_connection()
                    if (
                        getattr(exc, "rcvd", None) is not None
                        and getattr(exc.rcvd, "code", None) == 1008
                    ):
                        raise AuthenticationError("hot session authentication failed") from exc
                    raise
                except BaseException:
                    await self._discard_connection()
                    raise
                if not isinstance(ready, str):
                    await self._discard_connection()
                    raise DaemonHTTPError(
                        "hot session did not return a ready frame",
                        code="hot_session_failed",
                    )
                self._ready = True
                self._framing_tainted = False
        assert self._websocket is not None
        return self._websocket

    async def _connect(self) -> ClientConnection:
        headers = resolve_metadata_headers(self._metadata_headers)
        if self.token:
            headers.setdefault("Authorization", f"Bearer {self.token}")
        try:
            return await connect(
                daemon_websocket_url(self.base_url, "/v1/session/hot"),
                additional_headers=headers or None,
                open_timeout=self.timeout,
                max_size=8 * 1024 * 1024,
                compression=None,
            )
        except ConnectionClosed as exc:
            if getattr(exc, "rcvd", None) is not None and getattr(exc.rcvd, "code", None) == 1008:
                raise AuthenticationError("hot session authentication failed") from exc
            raise

    async def _exchange(
        self,
        op: str,
        payload: dict[str, Any],
        *,
        receive_binary: bool = False,
        metadata: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        websocket = await self._ensure_connected()
        request_id = str(next(self._ids))
        mutating = op in self._MUTATING_OPS
        sent = False
        try:
            message = {"id": request_id, "op": op, "payload": payload}
            if metadata is not None and _OPERATION_SEQUENCE_HEADER in metadata:
                message["sequence"] = metadata[_OPERATION_SEQUENCE_HEADER]
            encoded = json.dumps(message)
            sent = True
            await websocket.send(encoded)
            raw = await asyncio.wait_for(websocket.recv(), timeout=self.timeout)
            if not isinstance(raw, str):
                raise DaemonHTTPError(
                    "unexpected hot session frame",
                    code="hot_session_protocol_error",
                )
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("id") != request_id:
                raise DaemonHTTPError(
                    "unexpected hot session response id",
                    code="hot_session_protocol_error",
                )
            if data.get("type") == "error" and not _is_daemon_error(data):
                self._raise_hot_error(data)
            expected_type = "binary" if receive_binary else "result"
            if data.get("type") not in {"error", expected_type}:
                raise DaemonHTTPError(
                    "unexpected hot session response",
                    code="hot_session_protocol_error",
                )
            if receive_binary and data.get("type") == "binary":
                frame = await asyncio.wait_for(websocket.recv(), timeout=self.timeout)
                if not isinstance(frame, bytes):
                    raise DaemonHTTPError(
                        "hot session binary payload missing",
                        code="hot_session_protocol_error",
                    )
                data["_binary_payload"] = frame
            return data
        except asyncio.CancelledError:
            if sent:
                if mutating:
                    await self._poison_and_close()
                else:
                    await self._taint_and_close()
            raise
        except Exception:
            if sent:
                if mutating:
                    await self._poison_and_close()
                else:
                    await self._taint_and_close()
            raise

    async def _taint_and_close(self) -> None:
        await self._discard_connection()

    async def _discard_connection(self) -> None:
        self._framing_tainted = True
        websocket, self._websocket = self._websocket, None
        self._ready = False
        if websocket is None:
            return
        close_task = asyncio.create_task(websocket.close())
        self._close_task = close_task
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.shield(close_task)

    async def _poison_and_close(self) -> None:
        self._poisoned = True
        await self._discard_connection()

    @staticmethod
    def _raise_hot_error(message: dict[str, Any]) -> None:
        error = message.get("error")
        if not isinstance(error, dict):
            raise DaemonHTTPError("hot session request failed", code="hot_session_error")
        raise DaemonHTTPError(
            str(error.get("message") or "hot session request failed"),
            code=error.get("code") if isinstance(error.get("code"), str) else "hot_session_error",
            details=error.get("details") if isinstance(error.get("details"), dict) else None,
        )
