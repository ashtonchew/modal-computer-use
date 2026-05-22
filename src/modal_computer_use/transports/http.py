from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from modal_computer_use.errors import AuthenticationError, DaemonHTTPError
from modal_computer_use.observability import get_tracer


class HTTPTransport:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        http2: bool = False,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.last_http_version: str | None = None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            http2=http2,
        )
        self._tracer = get_tracer(name="modal_computer_use.sdk")

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
