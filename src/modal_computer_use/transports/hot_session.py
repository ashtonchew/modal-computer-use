from __future__ import annotations

import itertools
import json
import threading
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

from modal_computer_use.errors import AuthenticationError, DaemonHTTPError
from modal_computer_use.transports.metadata import MetadataHeaders, resolve_metadata_headers
from modal_computer_use.transports.websocket_url import daemon_websocket_url

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
class HotSessionBinaryResult:
    payload: bytes
    headers: Mapping[str, str]
    result: dict[str, Any] | None
    content_type: str | None


class HotSessionTransport:
    """Persistent daemon control channel for latency-sensitive primitive loops."""

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
        self._lock = threading.RLock()
        self._closed = False
        self._poisoned = False
        self._framing_tainted = False
        self._websocket = websocket or self._connect(timeout=timeout)
        try:
            self._receive_ready(self._websocket, timeout=self.timeout)
        except BaseException:
            self._discard_connection()
            raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            websocket, self._websocket = self._websocket, None
            self._framing_tainted = False
            if websocket is not None:
                with suppress(Exception):
                    websocket.close()

    def ping(self) -> dict[str, Any]:
        return self.request("ping", {})

    def request(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        if op in self._MUTATING_OPS and self._mutation_executor is not None:
            return self._mutation_executor(
                lambda metadata: self._request(op, payload, metadata=metadata)
            )
        return self._request(op, payload)

    def _request(
        self,
        op: str,
        payload: dict[str, Any],
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._ensure_usable()
            self._ensure_connected()
            message: dict[str, Any] | None = None
            sent = False
            try:
                request_id, encoded = self._encode_request(op, payload, metadata=metadata)
                sent = True
                message = self._send(request_id, encoded)
                if message.get("type") == "error" and not _is_daemon_error(message):
                    self._raise_hot_error(message)
                if message.get("type") not in {"error", "result"}:
                    raise DaemonHTTPError(
                        "unexpected hot session response",
                        code="hot_session_protocol_error",
                    )
            except BaseException:
                if sent:
                    if op in self._MUTATING_OPS and not _is_daemon_error(message):
                        self._poison_and_close()
                    else:
                        self._taint_and_close()
                raise
            assert message is not None
        if message.get("type") == "error":
            self._raise_hot_error(message)
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    def request_binary(self, op: str, payload: dict[str, Any]) -> HotSessionBinaryResult:
        if op in self._MUTATING_OPS and self._mutation_executor is not None:
            return self._mutation_executor(
                lambda metadata: self._request_binary(op, payload, metadata=metadata)
            )
        return self._request_binary(op, payload)

    def _request_binary(
        self,
        op: str,
        payload: dict[str, Any],
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> HotSessionBinaryResult:
        with self._lock:
            self._ensure_usable()
            self._ensure_connected()
            message: dict[str, Any] | None = None
            frame: object | None = None
            sent = False
            try:
                request_id, encoded = self._encode_request(op, payload, metadata=metadata)
                sent = True
                message = self._send(request_id, encoded)
                if message.get("type") == "error":
                    if not _is_daemon_error(message):
                        self._raise_hot_error(message)
                elif message.get("type") != "binary":
                    raise DaemonHTTPError(
                        "unexpected hot session response",
                        code="hot_session_protocol_error",
                    )
                else:
                    frame = self._websocket.recv(timeout=self.timeout)
                    if not isinstance(frame, bytes):
                        raise DaemonHTTPError(
                            "hot session binary payload missing",
                            code="hot_session_protocol_error",
                        )
            except BaseException:
                if sent:
                    if op in self._MUTATING_OPS and not _is_daemon_error(message):
                        self._poison_and_close()
                    else:
                        self._taint_and_close()
                raise
            assert message is not None
            if message.get("type") == "error":
                self._raise_hot_error(message)
        assert isinstance(frame, bytes)
        headers = message.get("headers")
        result = message.get("result")
        return HotSessionBinaryResult(
            payload=frame,
            headers=headers if isinstance(headers, dict) else {},
            result=result if isinstance(result, dict) else None,
            content_type=message.get("content_type")
            if isinstance(message.get("content_type"), str)
            else None,
        )

    def _ensure_usable(self) -> None:
        if self._poisoned:
            raise DaemonHTTPError(
                "hot session is unusable after an uncertain mutation",
                code="hot_session_poisoned",
            )
        if self._closed:
            raise DaemonHTTPError("hot session is closed", code="hot_session_closed")

    def _ensure_connected(self) -> ClientConnection:
        if self._websocket is not None and not self._framing_tainted:
            return self._websocket
        if self._websocket is not None:
            self._discard_connection()
        websocket = self._connect(timeout=self.timeout)
        self._websocket = websocket
        try:
            self._receive_ready(websocket, timeout=self.timeout)
        except BaseException:
            self._discard_connection()
            raise
        self._framing_tainted = False
        return websocket

    @staticmethod
    def _receive_ready(websocket: ClientConnection, *, timeout: float) -> None:
        try:
            ready = websocket.recv(timeout=timeout)
        except ConnectionClosed as exc:
            if getattr(exc, "rcvd", None) is not None and getattr(exc.rcvd, "code", None) == 1008:
                raise AuthenticationError("hot session authentication failed") from exc
            raise
        if not isinstance(ready, str):
            raise DaemonHTTPError(
                "hot session did not return a ready frame",
                code="hot_session_failed",
            )

    def _discard_connection(self) -> None:
        self._framing_tainted = True
        websocket, self._websocket = self._websocket, None
        if websocket is not None:
            with suppress(Exception):
                websocket.close()

    def _taint_and_close(self) -> None:
        self._discard_connection()

    def _poison_and_close(self) -> None:
        self._poisoned = True
        self._closed = True
        self._discard_connection()

    def _connect(self, *, timeout: float) -> ClientConnection:
        headers = resolve_metadata_headers(self._metadata_headers)
        if self.token:
            headers.setdefault("Authorization", f"Bearer {self.token}")
        try:
            return connect(
                _websocket_url(self.base_url, "/v1/session/hot"),
                additional_headers=headers or None,
                open_timeout=timeout,
                max_size=8 * 1024 * 1024,
                compression=None,
            )
        except ConnectionClosed as exc:
            if getattr(exc, "rcvd", None) is not None and getattr(exc.rcvd, "code", None) == 1008:
                raise AuthenticationError("hot session authentication failed") from exc
            raise

    def _encode_request(
        self,
        op: str,
        payload: dict[str, Any],
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> tuple[str, str]:
        request_id = str(next(self._ids))
        message_payload: dict[str, Any] = {
            "id": request_id,
            "op": op,
            "payload": payload,
        }
        if metadata is not None and _OPERATION_SEQUENCE_HEADER in metadata:
            message_payload["sequence"] = metadata[_OPERATION_SEQUENCE_HEADER]
        return request_id, json.dumps(message_payload)

    def _send(self, request_id: str, encoded: str) -> dict[str, Any]:
        websocket = self._websocket
        if websocket is None:
            raise DaemonHTTPError("hot session is not connected", code="hot_session_closed")
        websocket.send(encoded)
        raw = websocket.recv(timeout=self.timeout)
        if not isinstance(raw, str):
            raise DaemonHTTPError("unexpected hot session frame", code="hot_session_protocol_error")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("id") != request_id:
            raise DaemonHTTPError(
                "unexpected hot session response id",
                code="hot_session_protocol_error",
            )
        return data

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


def _websocket_url(base_url: str, path: str) -> str:
    return daemon_websocket_url(base_url, path)
