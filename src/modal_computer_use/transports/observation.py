from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

from modal_computer_use.errors import AuthenticationError, DaemonHTTPError


@dataclass(frozen=True)
class ObservationFrame:
    payload: bytes | None
    metadata: dict[str, Any]

    @property
    def seq(self) -> int:
        value = self.metadata.get("seq")
        return value if isinstance(value, int) else 0

    @property
    def unchanged(self) -> bool:
        return bool(self.metadata.get("unchanged"))

    @property
    def kind(self) -> str:
        value = self.metadata.get("kind")
        return value if isinstance(value, str) else "unknown"

    def compose(self, previous_payload: bytes | None = None) -> bytes | None:
        """Return a full image payload by applying a patch frame to a previous image."""
        if self.payload is None:
            return previous_payload
        if self.kind != "patch":
            return self.payload
        if previous_payload is None:
            raise DaemonHTTPError(
                "observation patch requires a previous frame",
                code="observation_stream_protocol_error",
            )
        rect = self.metadata.get("dirty_rect")
        if not isinstance(rect, dict):
            raise DaemonHTTPError(
                "observation patch missing dirty rect",
                code="observation_stream_protocol_error",
            )
        return _apply_patch(
            previous_payload,
            self.payload,
            rect=rect,
            image_format=self.metadata.get("format"),
        )


class ObservationStreamTransport:
    """Persistent daemon observation channel for server-pushed frames."""

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
        self._next_id = 1
        self._websocket = websocket or self._connect(timeout=timeout)
        try:
            ready = self._websocket.recv(timeout=timeout)
        except ConnectionClosed as exc:
            if getattr(exc, "rcvd", None) is not None and getattr(exc.rcvd, "code", None) == 1008:
                raise AuthenticationError("observation stream authentication failed") from exc
            raise
        if not isinstance(ready, str):
            raise DaemonHTTPError(
                "observation stream did not return a ready frame",
                code="observation_stream_failed",
            )

    def close(self) -> None:
        self._websocket.close()

    def frames(self, payload: dict[str, Any]) -> Iterator[ObservationFrame]:
        request_id = self._send("start", payload)
        started = self._recv_json()
        if started.get("type") == "error":
            self._raise_observation_error(started)
        if started.get("type") != "started" or started.get("id") != request_id:
            raise DaemonHTTPError(
                "unexpected observation stream start response",
                code="observation_stream_protocol_error",
            )
        while True:
            message = self._websocket.recv(timeout=self.timeout)
            if isinstance(message, bytes):
                raise DaemonHTTPError(
                    "observation frame metadata missing",
                    code="observation_stream_protocol_error",
                )
            data = json.loads(message)
            if not isinstance(data, dict):
                raise DaemonHTTPError(
                    "unexpected observation stream frame",
                    code="observation_stream_protocol_error",
                )
            if data.get("type") == "error":
                self._raise_observation_error(data)
            if data.get("type") == "stopped":
                return
            if data.get("type") == "unchanged":
                yield ObservationFrame(payload=None, metadata=data)
                continue
            if data.get("type") != "frame":
                raise DaemonHTTPError(
                    "unexpected observation stream frame",
                    code="observation_stream_protocol_error",
                )
            frame = self._websocket.recv(timeout=self.timeout)
            if not isinstance(frame, bytes):
                raise DaemonHTTPError(
                    "observation binary payload missing",
                    code="observation_stream_protocol_error",
                )
            yield ObservationFrame(payload=frame, metadata=data)

    def stop(self) -> None:
        self._send("stop", {})

    def pause(self) -> None:
        self._send("pause", {})

    def resume(self) -> None:
        self._send("resume", {})

    def request_frame(self) -> None:
        self._send("capture_now", {})

    def configure(self, payload: dict[str, Any]) -> None:
        self._send("configure", payload)

    def _connect(self, *, timeout: float) -> ClientConnection:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        try:
            return connect(
                _websocket_url(self.base_url, "/v1/observations/stream"),
                additional_headers=headers,
                open_timeout=timeout,
                max_size=8 * 1024 * 1024,
                compression=None,
            )
        except ConnectionClosed as exc:
            if getattr(exc, "rcvd", None) is not None and getattr(exc.rcvd, "code", None) == 1008:
                raise AuthenticationError("observation stream authentication failed") from exc
            raise

    def _send(self, op: str, payload: dict[str, Any]) -> str:
        request_id = str(self._next_id)
        self._next_id += 1
        self._websocket.send(json.dumps({"id": request_id, "op": op, "payload": payload}))
        return request_id

    def _recv_json(self) -> dict[str, Any]:
        message = self._websocket.recv(timeout=self.timeout)
        if not isinstance(message, str):
            raise DaemonHTTPError(
                "unexpected observation stream frame",
                code="observation_stream_protocol_error",
            )
        data = json.loads(message)
        if not isinstance(data, dict):
            raise DaemonHTTPError(
                "unexpected observation stream frame",
                code="observation_stream_protocol_error",
            )
        return data

    @staticmethod
    def _raise_observation_error(message: dict[str, Any]) -> None:
        error = message.get("error")
        if not isinstance(error, dict):
            raise DaemonHTTPError(
                "observation stream request failed",
                code="observation_stream_error",
            )
        raise DaemonHTTPError(
            str(error.get("message") or "observation stream request failed"),
            code=error.get("code")
            if isinstance(error.get("code"), str)
            else "observation_stream_error",
            details=error.get("details") if isinstance(error.get("details"), dict) else None,
        )


def _websocket_url(base_url: str, path: str) -> str:
    parts = urlsplit(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def _apply_patch(
    previous_payload: bytes,
    patch_payload: bytes,
    *,
    rect: dict[str, Any],
    image_format: Any,
) -> bytes:
    from PIL import Image

    x = rect.get("x")
    y = rect.get("y")
    if not isinstance(x, int) or not isinstance(y, int):
        raise DaemonHTTPError(
            "observation patch has invalid dirty rect",
            code="observation_stream_protocol_error",
        )
    base = Image.open(BytesIO(previous_payload)).convert("RGB")
    patch = Image.open(BytesIO(patch_payload)).convert("RGB")
    base.paste(patch, (x, y))
    output = BytesIO()
    fmt = "JPEG" if image_format == "jpeg" else str(image_format or "png").upper()
    save_kwargs: dict[str, Any] = {"quality": 90}
    if fmt == "WEBP":
        save_kwargs["method"] = 0
    base.save(output, format=fmt, **save_kwargs)
    return output.getvalue()
