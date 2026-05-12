from __future__ import annotations

from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, TypeAdapter

from .transports import HTTPTransport

T = TypeVar("T", bound=BaseModel)


class DaemonClient:
    """Synchronous HTTP facade for the computer-use daemon."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        transport: HTTPTransport | None = None,
    ) -> None:
        self.transport = transport or HTTPTransport(base_url, token=token, timeout=timeout)

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

    def model(self, model: type[T], method: str, path: str, **kwargs: Any) -> T:
        payload = self.transport.request(method, path, **kwargs).json()
        return model.model_validate(payload)

    def model_list(self, model: type[T], method: str, path: str, **kwargs: Any) -> list[T]:
        payload = self.transport.request(method, path, **kwargs).json()
        return TypeAdapter(list[model]).validate_python(payload)  # type: ignore[valid-type]

    def download(self, path: str, local_path: str | Path) -> Path:
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.get_bytes(path))
        return target
