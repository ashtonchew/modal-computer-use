from __future__ import annotations

import json
import struct
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO
from time import perf_counter
from typing import Any

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

from modal_computer_use.errors import AuthenticationError, DaemonHTTPError
from modal_computer_use.transports.websocket_url import daemon_websocket_url

FRAME_ENVELOPE_MAGIC = b"MCUO\x01"


@dataclass(frozen=True)
class ObservationFrame:
    payload: bytes | None
    metadata: dict[str, Any]
    transport_timing: dict[str, Any] | None = None

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
        if self.kind not in {"patch", "patches"}:
            return self.payload
        if previous_payload is None:
            raise DaemonHTTPError(
                "observation patch requires a previous frame",
                code="observation_stream_protocol_error",
            )
        if self.kind == "patches":
            return _apply_patch_bundle(
                previous_payload,
                self.payload,
                image_format=self.metadata.get("format"),
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
        self._pending_frames: deque[ObservationFrame] = deque()
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
        transport_timing = bool(payload.get("transport_timing"))
        self.start(payload)
        while True:
            try:
                yield self.receive_frame(transport_timing=transport_timing)
            except StopIteration:
                return

    def start(self, payload: dict[str, Any]) -> None:
        request_id = self._send("start", payload)
        started = self._recv_json()
        if started.get("type") == "error":
            self._raise_observation_error(started)
        if started.get("type") != "started" or started.get("id") != request_id:
            raise DaemonHTTPError(
                "unexpected observation stream start response",
                code="observation_stream_protocol_error",
            )

    def stop(self) -> None:
        self._send("stop", {})

    def pause(self) -> None:
        self._send("pause", {})

    def resume(self) -> None:
        self._send("resume", {})

    def request_frame(self) -> None:
        self._send("capture_now", {})

    def run_actions_capture(self, payload: dict[str, Any]) -> None:
        self._send("run_actions_capture", payload)

    def run_actions_observe_change(self, payload: dict[str, Any]) -> None:
        self._send("run_actions_observe_change", payload)

    def run_actions_observe_change_and_recv(
        self,
        payload: dict[str, Any],
        *,
        transport_timing: bool = False,
    ) -> ObservationFrame:
        request_id = self._send("run_actions_observe_change", payload)
        while True:
            frame = (
                self._recv_frame_with_timing()
                if transport_timing
                else self._receive_frame(transport_timing=False)
            )
            if frame.metadata.get("id") == request_id:
                return frame
            self._buffer_frame(frame)

    def configure(self, payload: dict[str, Any]) -> None:
        self._send("configure", payload)

    def transport_probe(
        self,
        *,
        size_bytes: int,
        frame_encoding: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"size_bytes": size_bytes}
        if frame_encoding is not None:
            payload["frame_encoding"] = frame_encoding
        request_id = self._send("transport_probe", payload)
        wait_metadata_started = perf_counter()
        message = self._websocket.recv(timeout=self.timeout)
        wait_metadata_ms = _elapsed_ms(wait_metadata_started)
        parse_metadata_started = perf_counter()
        if isinstance(message, bytes):
            data, frame = _decode_frame_envelope(message)
        else:
            if not isinstance(message, str):
                raise DaemonHTTPError(
                    "unexpected observation transport probe response",
                    code="observation_stream_protocol_error",
                )
            data = json.loads(message)
            frame = None
        parse_metadata_ms = _elapsed_ms(parse_metadata_started)
        if not isinstance(data, dict):
            raise DaemonHTTPError(
                "unexpected observation transport probe response",
                code="observation_stream_protocol_error",
            )
        if data.get("type") == "error":
            self._raise_observation_error(data)
        if data.get("type") != "transport_probe" or data.get("id") != request_id:
            raise DaemonHTTPError(
                "unexpected observation transport probe response",
                code="observation_stream_protocol_error",
            )
        if frame is None:
            wait_payload_started = perf_counter()
            frame = self._websocket.recv(timeout=self.timeout)
            wait_payload_ms = _elapsed_ms(wait_payload_started)
            if not isinstance(frame, bytes):
                raise DaemonHTTPError(
                    "observation transport probe payload missing",
                    code="observation_stream_protocol_error",
                )
        else:
            wait_payload_ms = 0.0
        wait_timing_started = perf_counter()
        timing = self._recv_transport_timing(expected_seq=None)
        wait_timing_ms = _elapsed_ms(wait_timing_started)
        if timing.get("id") != request_id:
            raise DaemonHTTPError(
                "observation transport probe timing id mismatch",
                code="observation_stream_protocol_error",
            )
        return {
            "size_bytes": len(frame),
            "requested_size_bytes": size_bytes,
            "frame_encoding": data.get("frame_encoding"),
            "server_emit_timing_ms": timing.get("server_emit_timing_ms"),
            "client_receive_timing_ms": {
                "wait_metadata_ms": wait_metadata_ms,
                "parse_metadata_ms": parse_metadata_ms,
                "wait_payload_ms": wait_payload_ms,
                "wait_transport_timing_ms": wait_timing_ms,
                "receive_total_ms": wait_metadata_ms
                + parse_metadata_ms
                + wait_payload_ms
                + wait_timing_ms,
            },
        }

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

    def recv_frame_with_timing(self) -> ObservationFrame:
        """Receive one frame and split client-side transport timing.

        This low-level helper is intended for benchmarks. Normal callers should
        iterate through ``frames()``.
        """
        if self._pending_frames:
            return self._pending_frames.popleft()
        return self._recv_frame_with_timing()

    def _recv_frame_with_timing(self) -> ObservationFrame:
        wait_metadata_started = perf_counter()
        message = self._websocket.recv(timeout=self.timeout)
        wait_metadata_ms = _elapsed_ms(wait_metadata_started)
        parse_metadata_started = perf_counter()
        if isinstance(message, bytes):
            data, envelope_payload = _decode_frame_envelope(message)
            envelope_timing = data.get("server_emit_timing_ms")
            parse_metadata_ms = _elapsed_ms(parse_metadata_started)
            if data.get("type") == "error":
                self._raise_observation_error(data)
            if data.get("type") == "stopped":
                raise StopIteration
            if data.get("type") not in {"frame", "unchanged"}:
                raise DaemonHTTPError(
                    "unexpected observation stream frame",
                    code="observation_stream_protocol_error",
                )
            wait_timing_ms = 0.0
            if not isinstance(envelope_timing, dict):
                wait_timing_started = perf_counter()
                timing = self._recv_transport_timing(expected_seq=data.get("seq"))
                wait_timing_ms = _elapsed_ms(wait_timing_started)
            else:
                timing = {"server_emit_timing_ms": envelope_timing}
            return self._construct_timed_frame(
                payload=None if data.get("type") == "unchanged" else envelope_payload,
                metadata=data,
                transport_timing=timing,
                wait_metadata_ms=wait_metadata_ms,
                parse_metadata_ms=parse_metadata_ms,
                wait_payload_ms=0.0,
                wait_transport_timing_ms=wait_timing_ms,
            )
        if not isinstance(message, str):
            raise DaemonHTTPError(
                "unexpected observation stream frame",
                code="observation_stream_protocol_error",
            )
        data = json.loads(message)
        parse_metadata_ms = _elapsed_ms(parse_metadata_started)
        if not isinstance(data, dict):
            raise DaemonHTTPError(
                "unexpected observation stream frame",
                code="observation_stream_protocol_error",
            )
        if data.get("type") == "error":
            self._raise_observation_error(data)
        if data.get("type") == "stopped":
            raise StopIteration
        if data.get("type") == "unchanged":
            wait_timing_started = perf_counter()
            transport_timing = self._recv_transport_timing(expected_seq=data.get("seq"))
            wait_timing_ms = _elapsed_ms(wait_timing_started)
            return self._construct_timed_frame(
                payload=None,
                metadata=data,
                transport_timing=transport_timing,
                wait_metadata_ms=wait_metadata_ms,
                parse_metadata_ms=parse_metadata_ms,
                wait_payload_ms=0.0,
                wait_transport_timing_ms=wait_timing_ms,
            )
        if data.get("type") != "frame":
            raise DaemonHTTPError(
                "unexpected observation stream frame",
                code="observation_stream_protocol_error",
            )
        wait_payload_started = perf_counter()
        frame = self._websocket.recv(timeout=self.timeout)
        wait_payload_ms = _elapsed_ms(wait_payload_started)
        if not isinstance(frame, bytes):
            raise DaemonHTTPError(
                "observation binary payload missing",
                code="observation_stream_protocol_error",
            )
        wait_timing_started = perf_counter()
        transport_timing = self._recv_transport_timing(expected_seq=data.get("seq"))
        wait_timing_ms = _elapsed_ms(wait_timing_started)
        return self._construct_timed_frame(
            payload=frame,
            metadata=data,
            transport_timing=transport_timing,
            wait_metadata_ms=wait_metadata_ms,
            parse_metadata_ms=parse_metadata_ms,
            wait_payload_ms=wait_payload_ms,
            wait_transport_timing_ms=wait_timing_ms,
        )

    def receive_frame(self, *, transport_timing: bool = False) -> ObservationFrame:
        if self._pending_frames:
            return self._pending_frames.popleft()
        return self._receive_frame(transport_timing=transport_timing)

    def _receive_frame(self, *, transport_timing: bool = False) -> ObservationFrame:
        message = self._websocket.recv(timeout=self.timeout)
        if isinstance(message, bytes):
            data, frame = _decode_frame_envelope(message)
        else:
            data = json.loads(message)
            frame = None
        if not isinstance(data, dict):
            raise DaemonHTTPError(
                "unexpected observation stream frame",
                code="observation_stream_protocol_error",
            )
        if data.get("type") == "error":
            self._raise_observation_error(data)
        if data.get("type") == "stopped":
            raise StopIteration
        if data.get("type") == "unchanged":
            timing = self._frame_transport_timing(data, transport_timing=transport_timing)
            return ObservationFrame(payload=None, metadata=data, transport_timing=timing)
        if data.get("type") != "frame":
            raise DaemonHTTPError(
                "unexpected observation stream frame",
                code="observation_stream_protocol_error",
            )
        if frame is None:
            frame = self._websocket.recv(timeout=self.timeout)
            if not isinstance(frame, bytes):
                raise DaemonHTTPError(
                    "observation binary payload missing",
                    code="observation_stream_protocol_error",
                )
        timing = self._frame_transport_timing(data, transport_timing=transport_timing)
        return ObservationFrame(payload=frame, metadata=data, transport_timing=timing)

    def _buffer_frame(self, frame: ObservationFrame) -> None:
        self._pending_frames.append(frame)

    def _construct_timed_frame(
        self,
        *,
        payload: bytes | None,
        metadata: dict[str, Any],
        transport_timing: dict[str, Any],
        wait_metadata_ms: float,
        parse_metadata_ms: float,
        wait_payload_ms: float,
        wait_transport_timing_ms: float,
    ) -> ObservationFrame:
        construct_started = perf_counter()
        result = ObservationFrame(
            payload=payload,
            metadata=metadata,
            transport_timing={
                **transport_timing,
                "client_receive_timing_ms": {
                    "wait_metadata_ms": wait_metadata_ms,
                    "parse_metadata_ms": parse_metadata_ms,
                    "wait_payload_ms": wait_payload_ms,
                    "wait_transport_timing_ms": wait_transport_timing_ms,
                    "frame_construct_ms": 0.0,
                    "receive_total_ms": wait_metadata_ms
                    + parse_metadata_ms
                    + wait_payload_ms
                    + wait_transport_timing_ms,
                },
            },
        )
        construct_ms = _elapsed_ms(construct_started)
        timing = result.transport_timing or {}
        client_timing = timing.get("client_receive_timing_ms")
        if isinstance(client_timing, dict):
            client_timing["frame_construct_ms"] = construct_ms
            client_timing["receive_total_ms"] += construct_ms
        return result

    def _frame_transport_timing(
        self,
        metadata: dict[str, Any],
        *,
        transport_timing: bool,
    ) -> dict[str, Any] | None:
        if not transport_timing:
            return None
        inline_timing = metadata.get("server_emit_timing_ms")
        if isinstance(inline_timing, dict):
            return {
                "type": "transport_timing",
                "stream_id": metadata.get("stream_id"),
                "seq": metadata.get("seq"),
                "trigger": metadata.get("trigger"),
                "server_emit_timing_ms": inline_timing,
            }
        return self._recv_transport_timing(expected_seq=metadata.get("seq"))

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

    def _recv_transport_timing(self, *, expected_seq: Any) -> dict[str, Any]:
        message = self._websocket.recv(timeout=self.timeout)
        if not isinstance(message, str):
            raise DaemonHTTPError(
                "observation transport timing missing",
                code="observation_stream_protocol_error",
            )
        data = json.loads(message)
        if not isinstance(data, dict) or data.get("type") != "transport_timing":
            raise DaemonHTTPError(
                "unexpected observation transport timing frame",
                code="observation_stream_protocol_error",
            )
        if data.get("seq") != expected_seq:
            raise DaemonHTTPError(
                "observation transport timing seq mismatch",
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
    return daemon_websocket_url(base_url, path)


def _elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1000


def _decode_frame_envelope(message: bytes) -> tuple[dict[str, Any], bytes]:
    header_size = len(FRAME_ENVELOPE_MAGIC) + 4
    if len(message) < header_size or not message.startswith(FRAME_ENVELOPE_MAGIC):
        raise DaemonHTTPError(
            "invalid observation binary envelope",
            code="observation_stream_protocol_error",
        )
    metadata_size = struct.unpack(">I", message[len(FRAME_ENVELOPE_MAGIC) : header_size])[0]
    metadata_start = header_size
    metadata_end = metadata_start + metadata_size
    if metadata_end > len(message):
        raise DaemonHTTPError(
            "truncated observation binary envelope",
            code="observation_stream_protocol_error",
        )
    data = json.loads(message[metadata_start:metadata_end])
    if not isinstance(data, dict):
        raise DaemonHTTPError(
            "invalid observation binary envelope metadata",
            code="observation_stream_protocol_error",
        )
    return data, message[metadata_end:]


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


def _apply_patch_bundle(
    previous_payload: bytes,
    patch_payload: bytes,
    *,
    image_format: Any,
) -> bytes:
    from PIL import Image

    if len(patch_payload) < 4:
        raise DaemonHTTPError(
            "observation patch bundle missing manifest",
            code="observation_stream_protocol_error",
        )
    manifest_len = struct.unpack(">I", patch_payload[:4])[0]
    manifest_end = 4 + manifest_len
    if manifest_end > len(patch_payload):
        raise DaemonHTTPError(
            "observation patch bundle manifest is truncated",
            code="observation_stream_protocol_error",
        )
    manifest = json.loads(patch_payload[4:manifest_end])
    patches = manifest.get("patches") if isinstance(manifest, dict) else None
    if not isinstance(patches, list):
        raise DaemonHTTPError(
            "observation patch bundle manifest is invalid",
            code="observation_stream_protocol_error",
        )
    base = Image.open(BytesIO(previous_payload)).convert("RGB")
    offset = manifest_end
    for patch in patches:
        if not isinstance(patch, dict):
            raise DaemonHTTPError(
                "observation patch bundle entry is invalid",
                code="observation_stream_protocol_error",
            )
        x = patch.get("x")
        y = patch.get("y")
        size = patch.get("size_bytes")
        if not isinstance(x, int) or not isinstance(y, int) or not isinstance(size, int):
            raise DaemonHTTPError(
                "observation patch bundle entry has invalid coordinates",
                code="observation_stream_protocol_error",
            )
        chunk = patch_payload[offset : offset + size]
        if len(chunk) != size:
            raise DaemonHTTPError(
                "observation patch bundle payload is truncated",
                code="observation_stream_protocol_error",
            )
        offset += size
        base.paste(Image.open(BytesIO(chunk)).convert("RGB"), (x, y))
    output = BytesIO()
    fmt = "JPEG" if image_format == "jpeg" else str(image_format or "png").upper()
    save_kwargs: dict[str, Any] = {"quality": 90}
    if fmt == "WEBP":
        save_kwargs["method"] = 0
    base.save(output, format=fmt, **save_kwargs)
    return output.getvalue()
