from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

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
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = client or httpx.Client(base_url=self.base_url, timeout=timeout)
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
                "http.route": path,
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
            span.set_attribute("http.status_code", response.status_code)
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
                    "http.route": path,
                },
            ) as span,
            self._client.stream(
                "GET",
                path,
                headers=self._request_headers(None),
            ) as response,
        ):
            span.set_attribute("http.status_code", response.status_code)
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
