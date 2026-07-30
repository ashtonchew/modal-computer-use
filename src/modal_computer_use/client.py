from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, TypeAdapter

from .transports import AsyncHTTPTransport, HTTPTransport
from .transports.metadata import MetadataHeaders

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
    ) -> None:
        self.transport = transport or HTTPTransport(
            base_url,
            token=token,
            timeout=timeout,
            http2=http2,
        )

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
    ) -> Any:
        response = self.transport.request("POST", path, json=json, headers=headers)
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
    ) -> Any:
        response = self.transport.request("PUT", path, json=json, content=content, headers=headers)
        if not response.content:
            return None
        return response.json()

    def delete_json(self, path: str) -> Any:
        response = self.transport.request("DELETE", path)
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
    ) -> bytes:
        return self.transport.request("POST", path, json=json, headers=headers).content

    def post_bytes_with_headers(
        self,
        path: str,
        *,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[bytes, Mapping[str, str]]:
        response = self.transport.request("POST", path, json=json, headers=headers)
        return response.content, response.headers

    def model(self, model: type[T], method: str, path: str, **kwargs: Any) -> T:
        payload = self.transport.request(method, path, **kwargs).json()
        return model.model_validate(payload)

    def model_list(self, model: type[T], method: str, path: str, **kwargs: Any) -> list[T]:
        payload = self.transport.request(method, path, **kwargs).json()
        return TypeAdapter(list[model]).validate_python(payload)  # type: ignore[valid-type]

    def download(self, path: str, local_path: str | Path) -> Path:
        return self.transport.stream_download(path, local_path)


class AsyncDaemonClient:
    """Native async HTTP facade for the computer-use daemon."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        http2: bool = False,
        transport: AsyncHTTPTransport | None = None,
        _metadata_headers: MetadataHeaders | None = None,
    ) -> None:
        self.transport = transport or AsyncHTTPTransport(
            base_url,
            token=token,
            timeout=timeout,
            http2=http2,
            _metadata_headers=_metadata_headers,
        )

    @property
    def base_url(self) -> str:
        return self.transport.base_url

    async def aclose(self) -> None:
        await self.transport.aclose()

    async def __aenter__(self) -> AsyncDaemonClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return (await self.transport.request("GET", path, params=params)).json()

    async def post_json(
        self,
        path: str,
        *,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        response = await self.transport.request("POST", path, json=json, headers=headers)
        return response.json() if response.content else None

    async def put_json(
        self,
        path: str,
        *,
        json: Any | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        response = await self.transport.request(
            "PUT", path, json=json, content=content, headers=headers
        )
        return response.json() if response.content else None

    async def delete_json(self, path: str) -> Any:
        response = await self.transport.request("DELETE", path)
        return response.json() if response.content else None

    async def get_bytes(self, path: str, *, params: dict[str, Any] | None = None) -> bytes:
        return (await self.transport.request("GET", path, params=params)).content

    async def post_bytes(
        self,
        path: str,
        *,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        return (await self.transport.request("POST", path, json=json, headers=headers)).content

    async def post_bytes_with_headers(
        self,
        path: str,
        *,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[bytes, Mapping[str, str]]:
        response = await self.transport.request("POST", path, json=json, headers=headers)
        return response.content, response.headers

    async def model(self, model: type[T], method: str, path: str, **kwargs: Any) -> T:
        payload = (await self.transport.request(method, path, **kwargs)).json()
        return model.model_validate(payload)

    async def model_list(self, model: type[T], method: str, path: str, **kwargs: Any) -> list[T]:
        payload = (await self.transport.request(method, path, **kwargs)).json()
        return TypeAdapter(list[model]).validate_python(payload)  # type: ignore[valid-type]

    async def download(self, path: str, local_path: str | Path) -> Path:
        return await self.transport.stream_download(path, local_path)
