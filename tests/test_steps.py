from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from modal_computer_use.borrowed import AsyncBorrowedComputer, BorrowedComputer
from modal_computer_use.client import AsyncDaemonClient, DaemonClient
from modal_computer_use.errors import (
    ActionOutcomeUnknownError,
    ActionValidationError,
    FrameValidationError,
    OperationResultUnavailableError,
    SessionLeaseLostError,
    SessionRecoveryRequiredError,
)
from modal_computer_use.models import (
    ActionBatchResult,
    ActionItemResult,
    CoordinateSpace,
    Screenshot,
    ScreenshotOptions,
)
from modal_computer_use.session_lease import AsyncSessionLeaseCoordinator, SessionLeaseCoordinator
from modal_computer_use.steps import (
    STEP_ENVELOPE_MAGIC,
    STEP_ENVELOPE_PREFIX,
    STEP_MEDIA_TYPE,
    ComputerStepResult,
    ComputerStepTiming,
    StepEnvelopeError,
    decode_step_envelope,
    encode_step_envelope,
)
from modal_computer_use.transports import AsyncHTTPTransport, HTTPTransport


def _screenshot(data: bytes, *, cursor_visible: bool = False) -> Screenshot:
    return Screenshot(
        format="png",
        width=1,
        height=1,
        size_bytes=len(data),
        bytes=data,
        sha256=hashlib.sha256(data).hexdigest(),
        captured_at=datetime(2026, 8, 8, 12, 30, tzinfo=UTC),
        coordinate_space=CoordinateSpace(
            desktop_width=1,
            desktop_height=1,
            image_width=1,
            image_height=1,
        ),
        cursor_visible=cursor_visible,
    )


def _actions(nested: Screenshot) -> ActionBatchResult:
    return ActionBatchResult(
        ok=True,
        results=[
            ActionItemResult(
                index=0,
                type="screenshot",
                ok=True,
                output={"screenshot": nested.model_dump(mode="python")},
            )
        ],
    )


def test_step_envelope_roundtrip_keeps_nested_and_final_screenshot_bytes() -> None:
    nested = _screenshot(b"nested-png")
    final = _screenshot(b"final-png", cursor_visible=True)
    encoded = encode_step_envelope(
        actions=_actions(nested),
        screenshot=final,
        timing=ComputerStepTiming(daemon_ms=3.5, action_ms=1.0, screenshot_ms=2.5),
    )

    assert encoded.startswith(STEP_ENVELOPE_MAGIC)
    result = decode_step_envelope(encoded)

    assert isinstance(result, ComputerStepResult)
    nested_payload = result.actions.results[0].output["screenshot"]
    assert isinstance(nested_payload, dict)
    assert nested_payload["bytes"] == b"nested-png"
    assert result.screenshot.as_bytes() == b"final-png"
    assert result.screenshot.cursor_visible is True
    assert result.timing.daemon_ms == 3.5
    assert result.timing.action_ms == 1.0


def test_step_envelope_rejects_truncated_or_tampered_payload_without_echoing_input() -> None:
    encoded = encode_step_envelope(
        actions=ActionBatchResult(ok=True, results=[]),
        screenshot=_screenshot(b"frame"),
        timing=ComputerStepTiming(daemon_ms=1.0),
    )
    tampered = bytearray(encoded)
    tampered[-1] ^= 0x01

    with pytest.raises(StepEnvelopeError, match="invalid computer step envelope") as exc_info:
        decode_step_envelope(bytes(tampered))
    assert "frame" not in str(exc_info.value)
    with pytest.raises(StepEnvelopeError, match="invalid computer step envelope"):
        decode_step_envelope(encoded[:-1])


def test_step_envelope_rejects_duplicate_keys_non_finite_values_and_empty_segments() -> None:
    duplicate = b'{"protocol":"computer-use.step.v1","protocol":"computer-use.step.v1"}'
    duplicate_envelope = (
        STEP_ENVELOPE_MAGIC
        + STEP_ENVELOPE_PREFIX.pack(1, len(duplicate), 0, 0)
        + duplicate
    )
    non_finite = json.dumps(
        {
            "protocol": "computer-use.step.v1",
            "actions": {},
            "screenshot": {},
            "timing": {"daemon_ms": "NaN"},
            "segments": [],
        },
        separators=(",", ":"),
    ).replace('"NaN"', "NaN").encode()
    non_finite_envelope = (
        STEP_ENVELOPE_MAGIC
        + STEP_ENVELOPE_PREFIX.pack(1, len(non_finite), 0, 0)
        + non_finite
    )
    empty_segment = json.dumps(
        {
            "protocol": "computer-use.step.v1",
            "actions": {},
            "screenshot": {
                "format": "png",
                "width": 1,
                "height": 1,
                "size_bytes": 0,
                "sha256": hashlib.sha256(b"").hexdigest(),
                "coordinate_space": {
                    "desktop_width": 1,
                    "desktop_height": 1,
                    "image_width": 1,
                    "image_height": 1,
                },
                "__step_segment__": 0,
            },
            "timing": {"daemon_ms": 0.0},
            "segments": [
                {
                    "kind": "screenshot",
                    "length": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                }
            ],
        },
        separators=(",", ":"),
    ).encode()
    empty_segment_envelope = (
        STEP_ENVELOPE_MAGIC
        + STEP_ENVELOPE_PREFIX.pack(1, len(empty_segment), 1, 0)
        + empty_segment
    )

    for malformed in (duplicate_envelope, non_finite_envelope, empty_segment_envelope):
        with pytest.raises(StepEnvelopeError, match="invalid computer step envelope"):
            decode_step_envelope(malformed)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("captured_at", "2026-08-08T12:30:00"),
        (
            "coordinate_space",
            {
                "desktop_width": 1,
                "desktop_height": 1,
                "image_width": 2,
                "image_height": 1,
                "scale_x": 1.0,
                "scale_y": 1.0,
            },
        ),
        ("cursor_position", {"x": 1, "y": 0}),
    ],
)
def test_step_envelope_rejects_inconsistent_screenshot_metadata(
    field: str,
    value: object,
) -> None:
    encoded = encode_step_envelope(
        actions=ActionBatchResult(ok=True, results=[]),
        screenshot=_screenshot(b"frame"),
        timing=ComputerStepTiming(daemon_ms=1.0),
    )
    prefix_size = len(STEP_ENVELOPE_MAGIC) + STEP_ENVELOPE_PREFIX.size
    _, manifest_length, segment_count, payload_length = STEP_ENVELOPE_PREFIX.unpack(
        encoded[len(STEP_ENVELOPE_MAGIC) : prefix_size]
    )
    manifest_end = prefix_size + manifest_length
    manifest = json.loads(encoded[prefix_size:manifest_end])
    manifest["screenshot"][field] = value
    manifest_bytes = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    tampered = (
        STEP_ENVELOPE_MAGIC
        + STEP_ENVELOPE_PREFIX.pack(
            1,
            len(manifest_bytes),
            segment_count,
            payload_length,
        )
        + manifest_bytes
        + encoded[manifest_end:]
    )

    with pytest.raises(StepEnvelopeError, match="invalid computer step envelope"):
        decode_step_envelope(tampered)


class _SyncTransport:
    base_url = "https://daemon.invalid"
    token = "secret-token"  # noqa: S105

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"method": method, "path": path, **kwargs})
        return httpx.Response(
            200,
            headers={"content-type": STEP_MEDIA_TYPE},
            content=self.payload,
        )

    def request_bounded(
        self,
        method: str,
        path: str,
        *,
        max_bytes: int,
        **kwargs: Any,
    ) -> httpx.Response:
        self.calls.append({"bounded_max_bytes": max_bytes})
        return self.request(method, path, **kwargs)

    def close(self) -> None:
        return None


class _AsyncTransport:
    base_url = "https://daemon.invalid"
    token = "secret-token"  # noqa: S105

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"method": method, "path": path, **kwargs})
        return httpx.Response(
            200,
            headers={"content-type": STEP_MEDIA_TYPE},
            content=self.payload,
        )

    async def request_bounded(
        self,
        method: str,
        path: str,
        *,
        max_bytes: int,
        **kwargs: Any,
    ) -> httpx.Response:
        self.calls.append({"bounded_max_bytes": max_bytes})
        return await self.request(method, path, **kwargs)

    async def aclose(self) -> None:
        return None


class _SyncCoordinator:
    def __init__(self) -> None:
        self.parsed_inside_callback = False

    def ensure_open(self) -> None:
        return None

    def execute(self, request):
        result = request({"x-computer-use-operation-sequence": "0"})
        self.parsed_inside_callback = isinstance(result, ComputerStepResult)
        return result


class _AsyncCoordinator:
    def __init__(self) -> None:
        self.parsed_inside_callback = False

    def ensure_open(self) -> None:
        return None

    async def execute(self, request):
        result = await request({"x-computer-use-operation-sequence": "0"})
        self.parsed_inside_callback = isinstance(result, ComputerStepResult)
        return result


class _LeaseTransport:
    base_url = "https://daemon.invalid"
    token = "secret-token"  # noqa: S105
    timeout = 0.05

    def __init__(self, *, resolved_state: str) -> None:
        self.resolved_state = resolved_state
        self.calls: list[str] = []

    def request(self, _method: str, path: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(path)
        if path == "/v1/leases/acquire":
            return httpx.Response(
                200,
                headers={"x-computer-use-lease-token": "lease-secret"},
                json={
                    "lease_id": "lease-test",
                    "daemon_epoch": "epoch-test",
                    "fence": 4,
                    "ttl_seconds": 30.0,
                    "heartbeat_interval_seconds": 60.0,
                },
            )
        if path == "/v1/steps":
            return httpx.Response(
                200,
                headers={"content-type": STEP_MEDIA_TYPE},
                content=b"corrupt-step-envelope",
            )
        if path == "/v1/receipts/resolve":
            return httpx.Response(
                200,
                json={
                    "state": self.resolved_state,
                    "sequence": kwargs["json"]["sequence"],
                    "operation_kind": "computer.step",
                },
            )
        if path == "/v1/leases/release":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"ok": True})

    def request_bounded(
        self,
        method: str,
        path: str,
        *,
        max_bytes: int,
        **kwargs: Any,
    ) -> httpx.Response:
        assert max_bytes > 0
        return self.request(method, path, **kwargs)

    def close(self) -> None:
        return None


class _AsyncLeaseTransport(_LeaseTransport):
    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return _LeaseTransport.request(self, method, path, **kwargs)

    async def request_bounded(
        self,
        method: str,
        path: str,
        *,
        max_bytes: int,
        **kwargs: Any,
    ) -> httpx.Response:
        assert max_bytes > 0
        return await self.request(method, path, **kwargs)


class _HeartbeatTransport:
    def request(self, _method: str, _path: str, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    def close(self) -> None:
        return None


def _response_envelope() -> bytes:
    return encode_step_envelope(
        actions=ActionBatchResult(ok=True, results=[]),
        screenshot=_screenshot(b"frame"),
        timing=ComputerStepTiming(daemon_ms=4.0),
    )


def test_sync_borrowed_step_uses_one_placed_step_request_and_decodes_in_callback() -> None:
    transport = _SyncTransport(_response_envelope())
    coordinator = _SyncCoordinator()
    client = DaemonClient(
        transport.base_url,
        transport=transport,  # type: ignore[arg-type]
        _mutation_executor=coordinator.execute,
    )
    computer = BorrowedComputer(
        client,
        coordinator,  # type: ignore[arg-type]
        base_url=transport.base_url,
        token=transport.token,
        http2=False,
    )

    result = computer.step([{"type": "click", "x": 1, "y": 2}])

    assert result.screenshot.as_bytes() == b"frame"
    assert coordinator.parsed_inside_callback is True
    assert len(transport.calls) == 2
    assert transport.calls[0]["bounded_max_bytes"] > 0
    request = transport.calls[1]
    assert request["path"] == "/v1/steps"
    assert request["json"]["continue_on_error"] is False
    assert "screenshot_options" not in request["json"]
    assert request["headers"]["Accept"] == STEP_MEDIA_TYPE
    assert request["headers"]["Accept-Encoding"] == "identity"
    assert request["headers"]["Cache-Control"] == "no-store"
    assert "screenshot_after" not in request["json"]
    assert "idempotency_key" not in request["json"]
    assert "sequence" not in request["json"]


def test_sync_step_validates_actions_before_dispatch_and_rejects_inactive_borrow() -> None:
    transport = _SyncTransport(_response_envelope())
    coordinator = _SyncCoordinator()
    client = DaemonClient(
        transport.base_url,
        transport=transport,  # type: ignore[arg-type]
        _mutation_executor=coordinator.execute,
    )
    computer = BorrowedComputer(
        client,
        coordinator,  # type: ignore[arg-type]
        base_url=transport.base_url,
        token=transport.token,
        http2=False,
    )

    with pytest.raises(ActionValidationError):
        computer.step([{"type": "click", "x": -1, "y": 2}])
    assert transport.calls == []

    computer._invalidate()
    with pytest.raises(SessionLeaseLostError):
        computer.step([])


def test_sync_step_rejects_observation_metadata_mismatch_after_dispatch() -> None:
    transport = _SyncTransport(_response_envelope())
    coordinator = _SyncCoordinator()
    client = DaemonClient(
        transport.base_url,
        transport=transport,  # type: ignore[arg-type]
        _mutation_executor=coordinator.execute,
    )
    computer = BorrowedComputer(
        client,
        coordinator,  # type: ignore[arg-type]
        base_url=transport.base_url,
        token=transport.token,
        http2=False,
    )

    with pytest.raises(StepEnvelopeError, match="invalid computer step envelope"):
        computer.step(
            [],
            screenshot_options=ScreenshotOptions(format="jpeg"),
        )
    assert len(transport.calls) == 2


def test_sync_step_matches_daemon_scale_floor_and_rejects_mismatch_after_dispatch() -> None:
    transport = _SyncTransport(_response_envelope())
    coordinator = _SyncCoordinator()
    client = DaemonClient(
        transport.base_url,
        transport=transport,  # type: ignore[arg-type]
        _mutation_executor=coordinator.execute,
    )
    computer = BorrowedComputer(
        client,
        coordinator,  # type: ignore[arg-type]
        base_url=transport.base_url,
        token=transport.token,
        http2=False,
    )

    result = computer.step([], screenshot_options=ScreenshotOptions(scale=0.01))
    assert result.screenshot.width == 1
    assert result.screenshot.height == 1
    assert len(transport.calls) == 2

    with pytest.raises(StepEnvelopeError, match="invalid computer step envelope"):
        computer.step([], screenshot_options=ScreenshotOptions(scale=2.0))
    assert len(transport.calls) == 4

    with pytest.raises(ValueError, match="daemon processing"):
        computer.step([], screenshot_options=ScreenshotOptions(processing="client"))
    assert len(transport.calls) == 4


def test_sync_real_lease_marks_corrupt_step_result_unavailable_without_replay() -> None:
    transport = _LeaseTransport(resolved_state="COMPLETED")
    coordinator = SessionLeaseCoordinator(transport, run_id="run-step")
    coordinator.acquire()
    client = DaemonClient(
        transport.base_url,
        transport=transport,  # type: ignore[arg-type]
        _mutation_executor=coordinator.execute,
    )
    computer = BorrowedComputer(
        client,
        coordinator,
        base_url=transport.base_url,
        token=transport.token,
        http2=False,
    )

    with pytest.raises(OperationResultUnavailableError):
        computer.step([])
    assert transport.calls.count("/v1/steps") == 1
    assert coordinator.observe_after_result_loss(lambda: "observed") == "observed"
    with pytest.raises(ActionOutcomeUnknownError):
        computer.step([])
    assert transport.calls.count("/v1/steps") == 1
    coordinator.close()


def test_sync_real_lease_requires_recovery_when_corrupt_step_outcome_is_indeterminate() -> None:
    transport = _LeaseTransport(resolved_state="INDETERMINATE")
    coordinator = SessionLeaseCoordinator(transport, run_id="run-step")
    coordinator.acquire()
    client = DaemonClient(
        transport.base_url,
        transport=transport,  # type: ignore[arg-type]
        _mutation_executor=coordinator.execute,
    )
    computer = BorrowedComputer(
        client,
        coordinator,
        base_url=transport.base_url,
        token=transport.token,
        http2=False,
    )

    with pytest.raises(SessionRecoveryRequiredError):
        computer.step([])
    assert transport.calls.count("/v1/steps") == 1
    with pytest.raises(ActionOutcomeUnknownError):
        computer.step([])
    assert transport.calls.count("/v1/steps") == 1
    coordinator.close()


@pytest.mark.asyncio
async def test_async_real_lease_marks_corrupt_step_result_unavailable_without_replay() -> None:
    transport = _AsyncLeaseTransport(resolved_state="COMPLETED")
    coordinator = AsyncSessionLeaseCoordinator(
        transport,
        run_id="run-step",
        heartbeat_transport=_HeartbeatTransport(),
    )
    await coordinator.acquire()
    client = AsyncDaemonClient(
        transport.base_url,
        transport=transport,  # type: ignore[arg-type]
        _mutation_executor=coordinator.execute,
    )
    computer = AsyncBorrowedComputer(
        client,
        coordinator,
        base_url=transport.base_url,
        token=transport.token,
        http2=False,
    )

    with pytest.raises(OperationResultUnavailableError):
        await computer.step([])
    assert transport.calls.count("/v1/steps") == 1
    assert (
        await coordinator.observe_after_result_loss(lambda: _async_value("observed"))
        == "observed"
    )
    with pytest.raises(ActionOutcomeUnknownError):
        await computer.step([])
    assert transport.calls.count("/v1/steps") == 1
    await coordinator.aclose()


@pytest.mark.asyncio
async def test_async_real_lease_requires_recovery_when_corrupt_step_is_indeterminate() -> None:
    transport = _AsyncLeaseTransport(resolved_state="INDETERMINATE")
    coordinator = AsyncSessionLeaseCoordinator(
        transport,
        run_id="run-step",
        heartbeat_transport=_HeartbeatTransport(),
    )
    await coordinator.acquire()
    client = AsyncDaemonClient(
        transport.base_url,
        transport=transport,  # type: ignore[arg-type]
        _mutation_executor=coordinator.execute,
    )
    computer = AsyncBorrowedComputer(
        client,
        coordinator,
        base_url=transport.base_url,
        token=transport.token,
        http2=False,
    )

    with pytest.raises(SessionRecoveryRequiredError):
        await computer.step([])
    assert transport.calls.count("/v1/steps") == 1
    with pytest.raises(ActionOutcomeUnknownError):
        await computer.step([])
    assert transport.calls.count("/v1/steps") == 1
    await coordinator.aclose()


async def _async_value(value: str) -> str:
    return value


@pytest.mark.asyncio
async def test_async_borrowed_step_has_sync_semantics_and_decodes_in_callback() -> None:
    transport = _AsyncTransport(_response_envelope())
    coordinator = _AsyncCoordinator()
    client = AsyncDaemonClient(
        transport.base_url,
        transport=transport,  # type: ignore[arg-type]
        _mutation_executor=coordinator.execute,
    )
    computer = AsyncBorrowedComputer(
        client,
        coordinator,  # type: ignore[arg-type]
        base_url=transport.base_url,
        token=transport.token,
        http2=False,
    )

    result = await computer.step([{"type": "click", "x": 1, "y": 2}])

    assert result.screenshot.as_bytes() == b"frame"
    assert coordinator.parsed_inside_callback is True
    assert len(transport.calls) == 2
    assert transport.calls[0]["bounded_max_bytes"] > 0
    request = transport.calls[1]
    assert request["path"] == "/v1/steps"
    assert "screenshot_options" not in request["json"]
    assert request["headers"]["Accept"] == STEP_MEDIA_TYPE
    assert "source" not in request["json"]
    await client.aclose()


@pytest.mark.asyncio
async def test_async_step_matches_daemon_scale_floor() -> None:
    transport = _AsyncTransport(_response_envelope())
    coordinator = _AsyncCoordinator()
    client = AsyncDaemonClient(
        transport.base_url,
        transport=transport,  # type: ignore[arg-type]
        _mutation_executor=coordinator.execute,
    )
    computer = AsyncBorrowedComputer(
        client,
        coordinator,  # type: ignore[arg-type]
        base_url=transport.base_url,
        token=transport.token,
        http2=False,
    )

    result = await computer.step(
        [], screenshot_options=ScreenshotOptions(scale=0.01)
    )

    assert result.screenshot.width == 1
    assert result.screenshot.height == 1
    await client.aclose()


def test_sync_http_transport_rejects_a_response_above_the_bounded_read() -> None:
    client = httpx.Client(
        base_url="https://daemon.invalid",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"12345")
        ),
    )
    transport = HTTPTransport("https://daemon.invalid", client=client)

    with pytest.raises(FrameValidationError):
        transport.request_bounded("POST", "/v1/steps", max_bytes=4)
    transport.close()


def test_sync_http_download_preserves_existing_destination_on_midstream_failure(
    tmp_path: Path,
) -> None:
    target = tmp_path / "artifact.bin"
    target.write_bytes(b"old")
    client = httpx.Client(
        base_url="https://daemon.invalid",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                stream=_FailingSyncDownloadStream(),
                request=request,
            )
        ),
    )
    transport = HTTPTransport("https://daemon.invalid", client=client)
    before = set(tmp_path.iterdir())

    with pytest.raises(RuntimeError, match="midstream"):
        transport.stream_download("/download", target)

    assert target.read_bytes() == b"old"
    assert set(tmp_path.iterdir()) == before
    transport.close()


def test_sync_http_download_replaces_existing_destination_after_success(tmp_path: Path) -> None:
    target = tmp_path / "artifact.bin"
    target.write_bytes(b"old")
    client = httpx.Client(
        base_url="https://daemon.invalid",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                stream=_SuccessfulSyncDownloadStream(),
                request=request,
            )
        ),
    )
    transport = HTTPTransport("https://daemon.invalid", client=client)

    assert transport.stream_download("/download", target) == target
    assert target.read_bytes() == b"new"
    transport.close()


class _FailingSyncDownloadStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b"new"
        raise RuntimeError("midstream")


class _SuccessfulSyncDownloadStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b"new"


@pytest.mark.asyncio
async def test_async_http_transport_rejects_a_response_above_the_bounded_read() -> None:
    client = httpx.AsyncClient(
        base_url="https://daemon.invalid",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"12345")
        ),
    )
    transport = AsyncHTTPTransport("https://daemon.invalid", client=client)

    with pytest.raises(FrameValidationError):
        await transport.request_bounded("POST", "/v1/steps", max_bytes=4)
    await transport.aclose()
