from __future__ import annotations

import itertools
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

from modal_computer_use.errors import AuthenticationError, DaemonHTTPError
from modal_computer_use.transports.websocket_url import daemon_websocket_url


@dataclass(frozen=True)
class HotSessionBinaryResult:
    payload: bytes
    headers: Mapping[str, str]
    result: dict[str, Any] | None
    content_type: str | None


class HotSessionTransport:
    """Persistent daemon control channel for latency-sensitive primitive loops."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        websocket: ClientConnection | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._ids = itertools.count(1)
        self._lock = threading.RLock()
        self._websocket = websocket or self._connect(timeout=timeout)
        try:
            ready = self._websocket.recv(timeout=timeout)
        except ConnectionClosed as exc:
            if getattr(exc, "rcvd", None) is not None and getattr(exc.rcvd, "code", None) == 1008:
                raise AuthenticationError("hot session authentication failed") from exc
            raise
        if not isinstance(ready, str):
            raise DaemonHTTPError(
                "hot session did not return a ready frame",
                code="hot_session_failed",
            )

    def close(self) -> None:
        self._websocket.close()

    def ping(self) -> dict[str, Any]:
        return self.request("ping", {})

    def request(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            message = self._send(op, payload)
        if message.get("type") == "error":
            self._raise_hot_error(message)
        if message.get("type") != "result":
            raise DaemonHTTPError(
                "unexpected hot session response",
                code="hot_session_protocol_error",
            )
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    def request_binary(self, op: str, payload: dict[str, Any]) -> HotSessionBinaryResult:
        with self._lock:
            message = self._send(op, payload)
            if message.get("type") == "error":
                self._raise_hot_error(message)
            if message.get("type") != "binary":
                raise DaemonHTTPError(
                    "unexpected hot session response",
                    code="hot_session_protocol_error",
                )
            frame = self._websocket.recv(timeout=self.timeout)
        if not isinstance(frame, bytes):
            raise DaemonHTTPError(
                "hot session binary payload missing",
                code="hot_session_protocol_error",
            )
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

    def _connect(self, *, timeout: float) -> ClientConnection:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        try:
            return connect(
                _websocket_url(self.base_url, "/v1/session/hot"),
                additional_headers=headers,
                open_timeout=timeout,
                max_size=8 * 1024 * 1024,
                compression=None,
            )
        except ConnectionClosed as exc:
            if getattr(exc, "rcvd", None) is not None and getattr(exc.rcvd, "code", None) == 1008:
                raise AuthenticationError("hot session authentication failed") from exc
            raise

    def _send(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(next(self._ids))
        self._websocket.send(json.dumps({"id": request_id, "op": op, "payload": payload}))
        message = self._websocket.recv(timeout=self.timeout)
        if not isinstance(message, str):
            raise DaemonHTTPError("unexpected hot session frame", code="hot_session_protocol_error")
        data = json.loads(message)
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
