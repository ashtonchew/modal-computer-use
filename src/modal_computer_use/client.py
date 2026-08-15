from __future__ import annotations

import asyncio
import math
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from pydantic import BaseModel, TypeAdapter

from .transports import AsyncHTTPTransport, HTTPTransport
from .transports.metadata import MetadataHeaders

if TYPE_CHECKING:
    from .hot_session import AsyncHotSessionClient
    from .latency import SessionStartupTiming
    from .observations import AsyncObservationClient
    from .steps import ComputerStepResult

T = TypeVar("T", bound=BaseModel)


class DaemonClient:
    """Synchronous HTTP facade for the computer-use daemon."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        http2: bool = False,
        transport: HTTPTransport | None = None,
        _token_resolver: Callable[[], str] | None = None,
        _mutation_executor: Callable[[Callable[[Mapping[str, str]], Any]], Any] | None = None,
        _owner_proof: str | None = None,
    ) -> None:
        self.transport = transport or HTTPTransport(
            base_url,
            token=token,
            timeout=timeout,
            http2=http2,
            _token_resolver=_token_resolver,
            owner_proof=_owner_proof,
        )
        if _owner_proof is not None and transport is not None:
            transport.owner_proof = _owner_proof
        self._mutation_executor = _mutation_executor

    @property
    def base_url(self) -> str:
        return self.transport.base_url

    def close(self) -> None:
        self.transport.close()

    def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return self.transport.request("GET", path, params=params).json()

    def post_json(
        self,
        path: str,
        *,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        _mutation: bool = False,
    ) -> Any:
        response = self._request("POST", path, json=json, headers=headers, mutation=_mutation)
        if not response.content:
            return None
        return response.json()

    def put_json(
        self,
        path: str,
        *,
        json: Any | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        _mutation: bool = False,
    ) -> Any:
        response = self._request(
            "PUT",
            path,
            json=json,
            content=content,
            headers=headers,
            mutation=_mutation,
        )
        if not response.content:
            return None
        return response.json()

    def delete_json(self, path: str, *, _mutation: bool = False) -> Any:
        response = self._request("DELETE", path, mutation=_mutation)
        if not response.content:
            return None
        return response.json()

    def get_bytes(self, path: str, *, params: dict[str, Any] | None = None) -> bytes:
        return self.transport.request("GET", path, params=params).content

    def post_bytes(
        self,
        path: str,
        *,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        _mutation: bool = False,
    ) -> bytes:
        return self._request("POST", path, json=json, headers=headers, mutation=_mutation).content

    def post_bytes_with_headers(
        self,
        path: str,
        *,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        _mutation: bool = False,
    ) -> tuple[bytes, Mapping[str, str]]:
        response = self._request("POST", path, json=json, headers=headers, mutation=_mutation)
        return response.content, response.headers

    def _post_step(self, payload: Mapping[str, Any]) -> ComputerStepResult:
        """Execute one fused action-to-observation request.

        The response parser is deliberately passed into ``_request`` so a
        borrowed lease coordinator observes a fully decoded result inside its
        serialized mutation callback.
        """

        from .steps import (
            MAX_STEP_ENVELOPE_BYTES,
            STEP_MEDIA_TYPE,
            StepEnvelopeError,
            decode_step_envelope,
        )
        from .steps.request import validate_step_screenshot_options

        def parse(response: Any) -> ComputerStepResult:
            content_type = response.headers.get("content-type")
            if content_type != STEP_MEDIA_TYPE or "content-encoding" in response.headers:
                raise StepEnvelopeError()
            result = decode_step_envelope(response.content)
            validate_step_screenshot_options(result, payload)
            return result

        return self._request(
            "POST",
            "/v1/steps",
            json=dict(payload),
            headers={
                "Accept": STEP_MEDIA_TYPE,
                "Accept-Encoding": "identity",
                "Cache-Control": "no-store",
            },
            mutation=True,
            _response_parser=parse,
            _max_response_bytes=MAX_STEP_ENVELOPE_BYTES,
        )

    def model(self, model: type[T], method: str, path: str, **kwargs: Any) -> T:
        payload = self.transport.request(method, path, **kwargs).json()
        return model.model_validate(payload)

    def model_list(self, model: type[T], method: str, path: str, **kwargs: Any) -> list[T]:
        payload = self.transport.request(method, path, **kwargs).json()
        return TypeAdapter(list[model]).validate_python(payload)

    def download(self, path: str, local_path: str | Path) -> Path:
        return self.transport.stream_download(path, local_path)

    def _request(
        self,
        method: str,
        path: str,
        *,
        mutation: bool = False,
        headers: Mapping[str, str] | None = None,
        _response_parser: Callable[[Any], Any] | None = None,
        _max_response_bytes: int | None = None,
        **kwargs: Any,
    ) -> Any:
        def send(request_headers: Mapping[str, str] | None) -> Any:
            if _max_response_bytes is not None:
                return self.transport.request_bounded(
                    method,
                    path,
                    headers=request_headers,
                    max_bytes=_max_response_bytes,
                    **kwargs,
                )
            return self.transport.request(method, path, headers=request_headers, **kwargs)

        if not mutation or self._mutation_executor is None:
            response = send(headers)
            return _response_parser(response) if _response_parser is not None else response

        def dispatch(metadata: Mapping[str, str]) -> Any:
            response = send({**dict(headers or {}), **metadata})
            return _response_parser(response) if _response_parser is not None else response

        return self._mutation_executor(dispatch)


class AsyncDaemonClient:
    """Native async interface to an existing computer-use daemon.

    Closing this client closes only the HTTP and WebSocket connections it
    created. It never stops the daemon or terminates a Modal Sandbox.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        http2: bool = False,
        transport: AsyncHTTPTransport | None = None,
        _metadata_headers: MetadataHeaders | None = None,
        _mutation_executor: Callable[
            [Callable[[Mapping[str, str]], Awaitable[Any]]], Awaitable[Any]
        ]
        | None = None,
        _owner_proof: str | None = None,
    ) -> None:
        self.transport = transport or AsyncHTTPTransport(
            base_url,
            token=token,
            timeout=timeout,
            http2=http2,
            _metadata_headers=_metadata_headers,
            owner_proof=_owner_proof,
        )
        if _owner_proof is not None and transport is not None:
            transport.owner_proof = _owner_proof
        self._mutation_executor = _mutation_executor
        self._children: weakref.WeakSet[AsyncHotSessionClient | AsyncObservationClient] = (
            weakref.WeakSet()
        )
        self._close_task: asyncio.Task[None] | None = None

        # Import locally because namespace base classes refer back to this client.
        from .namespaces import (
            AsyncActionsNamespace,
            AsyncAppsNamespace,
            AsyncArtifactsNamespace,
            AsyncBrowserNamespace,
            AsyncClipboardNamespace,
            AsyncCommandsNamespace,
            AsyncDebugNamespace,
            AsyncDisplayNamespace,
            AsyncInputNamespace,
            AsyncKeyboardNamespace,
            AsyncLifecycleNamespace,
            AsyncMouseNamespace,
            AsyncProcessesNamespace,
            AsyncRecordingsNamespace,
            AsyncScreenshotsNamespace,
            AsyncSessionNamespace,
            AsyncWindowsNamespace,
        )

        self.lifecycle = AsyncLifecycleNamespace(self)
        self.mouse = AsyncMouseNamespace(self)
        self.keyboard = AsyncKeyboardNamespace(self)
        self.clipboard = AsyncClipboardNamespace(self)
        self.screenshots = AsyncScreenshotsNamespace(self)
        self.recordings = AsyncRecordingsNamespace(self)
        self.display = AsyncDisplayNamespace(self)
        self.windows = AsyncWindowsNamespace(self)
        self.processes = AsyncProcessesNamespace(self)
        self.actions = AsyncActionsNamespace(self)
        self.input = AsyncInputNamespace(self)
        self.artifacts = AsyncArtifactsNamespace(self)
        self.browser = AsyncBrowserNamespace(self)
        self.apps = AsyncAppsNamespace(self)
        self.commands = AsyncCommandsNamespace(self)
        self.debug = AsyncDebugNamespace(self)
        self.session = AsyncSessionNamespace(self)

    @classmethod
    def local(
        cls,
        *,
        base_url: str = "http://127.0.0.1:8080",
        token: str | None = None,
        timeout: float = 30.0,
        http2: bool = False,
    ) -> AsyncDaemonClient:
        """Connect to a daemon that is already running on this machine."""
        return cls(base_url, token=token, timeout=timeout, http2=http2)

    @property
    def base_url(self) -> str:
        return self.transport.base_url

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_connections())
        cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError as cleanup_cancelled:
                if self._close_task.cancelled():
                    raise
                cancellation = cancellation or cleanup_cancelled
                continue
            if cancellation is not None:
                raise cancellation
            return

    async def _close_connections(self) -> None:
        failures: list[Exception] = []
        for child in tuple(self._children):
            if child._closed:
                continue
            try:
                await child.aclose()
            except Exception as exc:
                failures.append(exc)
        self._children.clear()
        try:
            await self.transport.aclose()
        except Exception as exc:
            failures.append(exc)
        if failures:
            failure = failures[0]
            for extra in failures[1:]:
                failure.add_note(f"additional connection cleanup failed ({type(extra).__name__})")
            raise failure

    async def __aenter__(self) -> AsyncDaemonClient:
        return self

    async def __aexit__(
        self,
        _exc_type: object,
        exc: object,
        _traceback: object,
    ) -> None:
        try:
            await self.aclose()
        except Exception as cleanup_exc:
            if isinstance(exc, BaseException):
                exc.add_note(
                    "daemon connection cleanup also failed "
                    f"({type(cleanup_exc).__name__})"
                )
                return
            raise

    async def wait_until_ready(self, timeout: float = 120.0, interval: float = 0.25) -> None:
        """Wait until the connected daemon reports that it can accept work."""
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive finite number")
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not math.isfinite(interval)
            or interval <= 0
        ):
            raise ValueError("interval must be a positive finite number")

        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while True:
            try:
                payload = await self.get_json("/readyz")
                last_error = None
                if isinstance(payload, dict) and payload.get("ready") is True:
                    return
            except Exception as exc:
                last_error = exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timeout_error = TimeoutError(
                    f"daemon did not become ready before timeout ({timeout:g}s)"
                )
                if last_error is not None:
                    timeout_error.add_note(
                        f"last readiness check failed ({type(last_error).__name__})"
                    )
                raise timeout_error
            await asyncio.sleep(min(interval, remaining))

    def hot_session(self, *, timeout: float = 30.0) -> AsyncHotSessionClient:
        """Open a persistent action connection owned by this client."""
        if self._close_task is not None:
            raise RuntimeError("daemon client is closing or closed")
        from .hot_session import AsyncHotSessionClient
        from .transports import AsyncHotSessionTransport

        child = AsyncHotSessionClient(
            AsyncHotSessionTransport(
                self.base_url,
                token=self.transport.token,
                timeout=timeout,
            )
        )
        self._children.add(child)
        return child

    def observation_stream(
        self,
        *,
        options: dict[str, Any] | None = None,
        fps: float = 5.0,
        max_frames: int | None = None,
        idle_timeout_ms: int | None = None,
        send_unchanged: bool = False,
        delivery: Literal["latest", "reliable"] | None = None,
        delta_mode: Literal["auto", "off"] | None = None,
        delta_max_ratio: float | None = None,
        keyframe_interval: int | None = None,
        tile_size: int | None = None,
        max_patch_rects: int | None = None,
        multi_rect_min_savings: float | None = None,
        frame_encoding: Literal["json-binary", "binary-envelope"] | None = "binary-envelope",
        timeout: float = 30.0,
        timing: SessionStartupTiming | None = None,
    ) -> AsyncObservationClient:
        """Open an observation stream owned by this client."""
        if self._close_task is not None:
            raise RuntimeError("daemon client is closing or closed")
        from .observations import AsyncObservationClient
        from .transports import AsyncObservationStreamTransport

        child = AsyncObservationClient(
            AsyncObservationStreamTransport(
                self.base_url,
                token=self.transport.token,
                timeout=timeout,
            ),
            options=options,
            fps=fps,
            max_frames=max_frames,
            idle_timeout_ms=idle_timeout_ms,
            send_unchanged=send_unchanged,
            delivery=delivery,
            delta_mode=delta_mode,
            delta_max_ratio=delta_max_ratio,
            keyframe_interval=keyframe_interval,
            tile_size=tile_size,
            max_patch_rects=max_patch_rects,
            multi_rect_min_savings=multi_rect_min_savings,
            frame_encoding=frame_encoding,
            startup_timing=timing,
        )
        self._children.add(child)
        return child

    async def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return (await self.transport.request("GET", path, params=params)).json()

    async def post_json(
        self,
        path: str,
        *,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        _mutation: bool = False,
    ) -> Any:
        response = await self._request("POST", path, json=json, headers=headers, mutation=_mutation)
        return response.json() if response.content else None

    async def put_json(
        self,
        path: str,
        *,
        json: Any | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        _mutation: bool = False,
    ) -> Any:
        response = await self._request(
            "PUT",
            path,
            json=json,
            content=content,
            headers=headers,
            mutation=_mutation,
        )
        return response.json() if response.content else None

    async def delete_json(self, path: str, *, _mutation: bool = False) -> Any:
        response = await self._request("DELETE", path, mutation=_mutation)
        return response.json() if response.content else None

    async def get_bytes(self, path: str, *, params: dict[str, Any] | None = None) -> bytes:
        return (await self.transport.request("GET", path, params=params)).content

    async def post_bytes(
        self,
        path: str,
        *,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        _mutation: bool = False,
    ) -> bytes:
        return (
            await self._request("POST", path, json=json, headers=headers, mutation=_mutation)
        ).content

    async def post_bytes_with_headers(
        self,
        path: str,
        *,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        _mutation: bool = False,
    ) -> tuple[bytes, Mapping[str, str]]:
        response = await self._request("POST", path, json=json, headers=headers, mutation=_mutation)
        return response.content, response.headers

    async def _post_step(self, payload: Mapping[str, Any]) -> ComputerStepResult:
        """Execute one fused action-to-observation request."""

        from .steps import (
            MAX_STEP_ENVELOPE_BYTES,
            STEP_MEDIA_TYPE,
            StepEnvelopeError,
            decode_step_envelope,
        )
        from .steps.request import validate_step_screenshot_options

        def parse(response: Any) -> ComputerStepResult:
            content_type = response.headers.get("content-type")
            if content_type != STEP_MEDIA_TYPE or "content-encoding" in response.headers:
                raise StepEnvelopeError()
            result = decode_step_envelope(response.content)
            validate_step_screenshot_options(result, payload)
            return result

        return await self._request(
            "POST",
            "/v1/steps",
            json=dict(payload),
            headers={
                "Accept": STEP_MEDIA_TYPE,
                "Accept-Encoding": "identity",
                "Cache-Control": "no-store",
            },
            mutation=True,
            _response_parser=parse,
            _max_response_bytes=MAX_STEP_ENVELOPE_BYTES,
        )

    async def model(self, model: type[T], method: str, path: str, **kwargs: Any) -> T:
        payload = (await self.transport.request(method, path, **kwargs)).json()
        return model.model_validate(payload)

    async def model_list(self, model: type[T], method: str, path: str, **kwargs: Any) -> list[T]:
        payload = (await self.transport.request(method, path, **kwargs)).json()
        return TypeAdapter(list[model]).validate_python(payload)

    async def download(self, path: str, local_path: str | Path) -> Path:
        return await self.transport.stream_download(path, local_path)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        mutation: bool = False,
        headers: Mapping[str, str] | None = None,
        _response_parser: Callable[[Any], Any] | None = None,
        _max_response_bytes: int | None = None,
        **kwargs: Any,
    ) -> Any:
        async def send(request_headers: Mapping[str, str] | None) -> Any:
            if _max_response_bytes is not None:
                return await self.transport.request_bounded(
                    method,
                    path,
                    headers=request_headers,
                    max_bytes=_max_response_bytes,
                    **kwargs,
                )
            return await self.transport.request(method, path, headers=request_headers, **kwargs)

        if not mutation or self._mutation_executor is None:
            response = await send(headers)
            return _response_parser(response) if _response_parser is not None else response

        async def dispatch(metadata: Mapping[str, str]) -> Any:
            response = await send({**dict(headers or {}), **metadata})
            return _response_parser(response) if _response_parser is not None else response

        return await self._mutation_executor(dispatch)
