from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlsplit

import httpx

from modal_computer_use.errors import AuthenticationError, DaemonHTTPError, FrameValidationError
from modal_computer_use.observability import get_tracer

from .metadata import MetadataHeaders, resolve_metadata_headers

_OWNER_PROOF_HEADER = "X-Computer-Use-Owner-Proof"


class HTTPTransport:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        http2: bool = False,
        client: httpx.Client | None = None,
        _metadata_headers: MetadataHeaders | None = None,
        _token_resolver: Callable[[], str] | None = None,
        owner_proof: str | None = None,
    ) -> None:
        if token is not None and _token_resolver is not None:
            raise ValueError("token and _token_resolver are mutually exclusive")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._token_resolver = _token_resolver
        self._token_lock = Lock()
        self._owner_proof = owner_proof
        self.last_http_version: str | None = None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            http2=http2,
        )
        self._metadata_headers = _metadata_headers
        self._tracer = get_tracer(name="modal_computer_use.sdk")

    @property
    def token(self) -> str | None:
        if self._token is not None or self._token_resolver is None:
            return self._token
        with self._token_lock:
            if self._token is None and self._token_resolver is not None:
                self._token = self._token_resolver()
                self._token_resolver = None
        return self._token

    def close(self) -> None:
        self._client.close()

    @property
    def owner_proof(self) -> str | None:
        return self._owner_proof

    @owner_proof.setter
    def owner_proof(self, value: str | None) -> None:
        self._owner_proof = value

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        request_headers = self._request_headers(headers, path=path)
        with self._tracer.span(
            "sdk.request",
            {
                "http.method": method,
                "http.route": _route_path(path),
            },
        ) as span:
            response = self._client.request(
                method,
                path,
                json=json,
                params=params,
                content=content,
                headers=request_headers,
            )
            self.last_http_version = response.http_version
            span.set_attribute("http.status_code", response.status_code)
            error_code = _error_code(response)
            if error_code:
                span.set_attribute("error.code", error_code)
        self._raise_for_status(response)
        return response

    def request_bounded(
        self,
        method: str,
        path: str,
        *,
        max_bytes: int,
        json: Any | None = None,
        params: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Read one response under a fixed bound before exposing its body."""

        if isinstance(max_bytes, bool) or max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        request_headers = self._request_headers(headers, path=path)
        with (
            self._tracer.span(
                "sdk.request",
                {"http.method": method, "http.route": _route_path(path)},
            ) as span,
            self._client.stream(
                method,
                path,
                json=json,
                params=params,
                content=content,
                headers=request_headers,
            ) as streamed,
        ):
            self.last_http_version = streamed.http_version
            span.set_attribute("http.status_code", streamed.status_code)
            _validate_declared_length(streamed.headers, max_bytes=max_bytes)
            body = bytearray()
            for chunk in streamed.iter_raw():
                if len(body) + len(chunk) > max_bytes:
                    raise FrameValidationError("daemon response exceeded its size limit")
                body.extend(chunk)
            response = httpx.Response(
                streamed.status_code,
                headers=streamed.headers,
                content=bytes(body),
                request=streamed.request,
                extensions=streamed.extensions,
            )
            error_code = _error_code(response)
            if error_code:
                span.set_attribute("error.code", error_code)
        self._raise_for_status(response)
        return response

    def stream_download(self, path: str, target: str | Path) -> Path:
        output = Path(target)
        output.parent.mkdir(parents=True, exist_ok=True)
        with (
            self._tracer.span(
                "sdk.download",
                {
                    "http.method": "GET",
                    "http.route": _route_path(path),
                },
            ) as span,
            self._client.stream(
                "GET",
                path,
                headers=self._request_headers(None, path=path),
            ) as response,
        ):
            self.last_http_version = response.http_version
            span.set_attribute("http.status_code", response.status_code)
            error_code = _error_code(response)
            if error_code:
                span.set_attribute("error.code", error_code)
            self._raise_for_status(response)
            temporary = _create_download_temp(output)
            try:
                with temporary.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        if chunk:
                            handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, output)
            except BaseException:
                _remove_download_temp(temporary)
                raise
        return output

    def _request_headers(
        self,
        headers: Mapping[str, str] | None,
        *,
        path: str,
    ) -> dict[str, str]:
        request_headers = dict(headers or {})
        if self.token:
            request_headers.setdefault("Authorization", f"Bearer {self.token}")
        if self._owner_proof and _owner_proof_route(path):
            request_headers.setdefault(_OWNER_PROOF_HEADER, self._owner_proof)
        request_headers.update(resolve_metadata_headers(self._metadata_headers))
        return request_headers

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code == 401:
            raise AuthenticationError("daemon authentication failed")
        if response.status_code >= 400:
            with suppress(Exception):
                response.read()
            try:
                payload = response.json()
            except ValueError:
                payload = {"message": response.text}
            raise DaemonHTTPError(
                payload.get("message") or payload.get("detail") or response.text,
                status_code=response.status_code,
                code=payload.get("code"),
                details=payload.get("details")
                if isinstance(payload.get("details"), dict)
                else None,
                retry_after_seconds=_retry_after_seconds(response),
            )


def _create_download_temp(output: Path) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    os.close(descriptor)
    return Path(name)


def _remove_download_temp(path: Path) -> None:
    with suppress(FileNotFoundError):
        path.unlink()


def _fsync_download_temp(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _route_path(path: str) -> str:
    route = urlsplit(path).path or "/"
    if route.startswith("/v1/artifacts/") and route not in {
        "/v1/artifacts/manifest",
        "/v1/artifacts/sync",
    }:
        return "/v1/artifacts/{path:path}"
    return route


def _error_code(response: httpx.Response) -> str | None:
    if response.status_code < 400:
        return None
    with suppress(Exception):
        response.read()
    try:
        payload = response.json()
    except ValueError:
        return None
    code = payload.get("code")
    return code if isinstance(code, str) else None


def _owner_proof_route(path: str) -> bool:
    route = urlsplit(path).path
    if route in {
        "/v1/computer/start",
        "/v1/computer/stop",
        "/v1/computer/restart",
    }:
        return True
    return route.startswith("/v1/processes/") and route.endswith("/restart")


class AsyncHTTPTransport:
    """Native async HTTP transport with one connection-pooled client."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        http2: bool = False,
        client: httpx.AsyncClient | None = None,
        _metadata_headers: MetadataHeaders | None = None,
        owner_proof: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._owner_proof = owner_proof
        self.last_http_version: str | None = None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            http2=http2,
        )
        self._metadata_headers = _metadata_headers
        self._tracer = get_tracer(name="modal_computer_use.sdk")

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def owner_proof(self) -> str | None:
        return self._owner_proof

    @owner_proof.setter
    def owner_proof(self, value: str | None) -> None:
        self._owner_proof = value

    async def __aenter__(self) -> AsyncHTTPTransport:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        request_headers = self._request_headers(headers, path=path)
        with self._tracer.span(
            "sdk.request",
            {
                "http.method": method,
                "http.route": _route_path(path),
            },
        ) as span:
            response = await self._client.request(
                method,
                path,
                json=json,
                params=params,
                content=content,
                headers=request_headers,
            )
            self.last_http_version = response.http_version
            span.set_attribute("http.status_code", response.status_code)
            error_code = _error_code(response)
            if error_code:
                span.set_attribute("error.code", error_code)
        await self._raise_for_status(response)
        return response

    async def request_bounded(
        self,
        method: str,
        path: str,
        *,
        max_bytes: int,
        json: Any | None = None,
        params: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Read one async response under a fixed bound before exposing it."""

        if isinstance(max_bytes, bool) or max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        request_headers = self._request_headers(headers, path=path)
        with self._tracer.span(
            "sdk.request",
            {"http.method": method, "http.route": _route_path(path)},
        ) as span:
            async with self._client.stream(
                method,
                path,
                json=json,
                params=params,
                content=content,
                headers=request_headers,
            ) as streamed:
                self.last_http_version = streamed.http_version
                span.set_attribute("http.status_code", streamed.status_code)
                _validate_declared_length(streamed.headers, max_bytes=max_bytes)
                body = bytearray()
                async for chunk in streamed.aiter_raw():
                    if len(body) + len(chunk) > max_bytes:
                        raise FrameValidationError("daemon response exceeded its size limit")
                    body.extend(chunk)
                response = httpx.Response(
                    streamed.status_code,
                    headers=streamed.headers,
                    content=bytes(body),
                    request=streamed.request,
                    extensions=streamed.extensions,
                )
                error_code = _error_code(response)
                if error_code:
                    span.set_attribute("error.code", error_code)
        await self._raise_for_status(response)
        return response

    async def stream_download(self, path: str, target: str | Path) -> Path:
        import anyio

        output = Path(target)
        await anyio.to_thread.run_sync(lambda: output.parent.mkdir(parents=True, exist_ok=True))
        with self._tracer.span(
            "sdk.download",
            {
                "http.method": "GET",
                "http.route": _route_path(path),
            },
        ) as span:
            async with self._client.stream(
                "GET",
                path,
                headers=self._request_headers(None, path=path),
            ) as response:
                self.last_http_version = response.http_version
                span.set_attribute("http.status_code", response.status_code)
                await self._raise_for_status(response)
                temporary = _create_download_temp(output)
                try:
                    async with await anyio.open_file(temporary, "wb") as handle:
                        async for chunk in response.aiter_bytes():
                            if chunk:
                                await handle.write(chunk)
                    await anyio.to_thread.run_sync(_fsync_download_temp, temporary)
                    await anyio.to_thread.run_sync(os.replace, temporary, output)
                except BaseException:
                    with anyio.CancelScope(shield=True):
                        await anyio.to_thread.run_sync(_remove_download_temp, temporary)
                    raise
        return output

    def _request_headers(
        self,
        headers: Mapping[str, str] | None,
        *,
        path: str,
    ) -> dict[str, str]:
        request_headers = dict(headers or {})
        if self.token:
            request_headers.setdefault("Authorization", f"Bearer {self.token}")
        if self._owner_proof and _owner_proof_route(path):
            request_headers.setdefault(_OWNER_PROOF_HEADER, self._owner_proof)
        request_headers.update(resolve_metadata_headers(self._metadata_headers))
        return request_headers

    @staticmethod
    async def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code == 401:
            raise AuthenticationError("daemon authentication failed")
        if response.status_code < 400:
            return
        with suppress(Exception):
            await response.aread()
        try:
            payload = response.json()
        except ValueError:
            payload = {"message": response.text}
        raise DaemonHTTPError(
            payload.get("message") or payload.get("detail") or response.text,
            status_code=response.status_code,
            code=payload.get("code"),
            details=payload.get("details") if isinstance(payload.get("details"), dict) else None,
            retry_after_seconds=_retry_after_seconds(response),
        )


def _retry_after_seconds(response: httpx.Response) -> int | None:
    value = response.headers.get("Retry-After")
    if value is None or not value.isascii() or not value.isdigit():
        return None
    parsed = int(value)
    return parsed if 0 <= parsed <= 2_147_483_647 else None


def _validate_declared_length(headers: Mapping[str, str], *, max_bytes: int) -> None:
    declared = headers.get("content-length")
    if declared is None:
        return
    try:
        length = int(declared)
    except ValueError as exc:
        raise FrameValidationError("daemon response has an invalid size") from exc
    if length < 0 or length > max_bytes:
        raise FrameValidationError("daemon response exceeded its size limit")
