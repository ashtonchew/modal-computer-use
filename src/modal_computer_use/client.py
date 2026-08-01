from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
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
        _token_resolver: Callable[[], str] | None = None,
        _mutation_executor: Callable[[Callable[[Mapping[str, str]], Any]], Any] | None = None,
    ) -> None:
        self.transport = transport or HTTPTransport(
            base_url,
            token=token,
            timeout=timeout,
            http2=http2,
            _token_resolver=_token_resolver,
        )
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
        **kwargs: Any,
    ) -> Any:
        if not mutation or self._mutation_executor is None:
            return self.transport.request(method, path, headers=headers, **kwargs)

        def dispatch(metadata: Mapping[str, str]) -> Any:
            return self.transport.request(
                method,
                path,
                headers={**dict(headers or {}), **metadata},
                **kwargs,
            )

        return self._mutation_executor(dispatch)


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
        _mutation_executor: Callable[
            [Callable[[Mapping[str, str]], Awaitable[Any]]], Awaitable[Any]
        ]
        | None = None,
    ) -> None:
        self.transport = transport or AsyncHTTPTransport(
            base_url,
            token=token,
            timeout=timeout,
            http2=http2,
            _metadata_headers=_metadata_headers,
        )
        self._mutation_executor = _mutation_executor

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
        **kwargs: Any,
    ) -> Any:
        if not mutation or self._mutation_executor is None:
            return await self.transport.request(method, path, headers=headers, **kwargs)

        async def dispatch(metadata: Mapping[str, str]) -> Any:
            return await self.transport.request(
                method,
                path,
                headers={**dict(headers or {}), **metadata},
                **kwargs,
            )

        return await self._mutation_executor(dispatch)
