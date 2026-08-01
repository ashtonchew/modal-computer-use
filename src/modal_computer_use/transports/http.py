from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlsplit

import httpx

from modal_computer_use.errors import AuthenticationError, DaemonHTTPError
from modal_computer_use.observability import get_tracer

from .metadata import MetadataHeaders, resolve_metadata_headers


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
    ) -> None:
        if token is not None and _token_resolver is not None:
            raise ValueError("token and _token_resolver are mutually exclusive")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._token_resolver = _token_resolver
        self._token_lock = Lock()
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
        request_headers = self._request_headers(headers)
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
                headers=self._request_headers(None),
            ) as response,
        ):
            self.last_http_version = response.http_version
            span.set_attribute("http.status_code", response.status_code)
            error_code = _error_code(response)
            if error_code:
                span.set_attribute("error.code", error_code)
            self._raise_for_status(response)
            with output.open("wb") as handle:
                for chunk in response.iter_bytes():
                    if chunk:
                        handle.write(chunk)
        return output

    def _request_headers(self, headers: Mapping[str, str] | None) -> dict[str, str]:
        request_headers = dict(headers or {})
        if self.token:
            request_headers.setdefault("Authorization", f"Bearer {self.token}")
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
            )


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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
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
        request_headers = self._request_headers(headers)
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
                headers=self._request_headers(None),
            ) as response:
                self.last_http_version = response.http_version
                span.set_attribute("http.status_code", response.status_code)
                await self._raise_for_status(response)
                async with await anyio.open_file(output, "wb") as handle:
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            await handle.write(chunk)
        return output

    def _request_headers(self, headers: Mapping[str, str] | None) -> dict[str, str]:
        request_headers = dict(headers or {})
        if self.token:
            request_headers.setdefault("Authorization", f"Bearer {self.token}")
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
        )
