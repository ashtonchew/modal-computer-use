from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from modal_computer_use.errors import AuthenticationError, DaemonHTTPError


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
        request_headers = dict(headers or {})
        if self.token:
            request_headers.setdefault("Authorization", f"Bearer {self.token}")
        response = self._client.request(
            method,
            path,
            json=json,
            params=params,
            content=content,
            headers=request_headers,
        )
        if response.status_code == 401:
            raise AuthenticationError("daemon authentication failed")
        if response.status_code >= 400:
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
        return response
