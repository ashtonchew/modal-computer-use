from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from modal_computer_use.artifacts import normalize_artifact_path
from modal_computer_use.models import ArtifactInfo, ArtifactSyncResult

from .base import AsyncNamespace, Namespace


def _artifact_url(path: str) -> str:
    safe = normalize_artifact_path(path)
    return "/v1/artifacts/" + quote(safe, safe="/")


class ArtifactsNamespace(Namespace):
    def list(self, prefix: str = "") -> list[ArtifactInfo]:
        payload = self._client.get_json("/v1/artifacts", params={"prefix": prefix})
        return [ArtifactInfo.model_validate(item) for item in payload]

    def read_bytes(self, path: str) -> bytes:
        return self._client.get_bytes(_artifact_url(path))

    def write_bytes(
        self,
        path: str,
        data: bytes,
        content_type: str | None = None,
    ) -> ArtifactInfo:
        headers = {"Content-Type": content_type or "application/octet-stream"}
        return ArtifactInfo.model_validate(
            self._client.put_json(
                _artifact_url(path), content=data, headers=headers, _mutation=True
            )
        )

    def download(self, path: str, local_path: str | Path) -> Path:
        return self._client.download(_artifact_url(path), local_path)

    def upload(self, local_path: str | Path, path: str) -> ArtifactInfo:
        source = Path(local_path)
        return self.write_bytes(path, source.read_bytes())

    def delete(self, path: str) -> None:
        self._client.delete_json(_artifact_url(path), _mutation=True)

    def manifest(self, prefix: str = "") -> list[ArtifactInfo]:
        payload = self._client.get_json("/v1/artifacts/manifest", params={"prefix": prefix})
        return [ArtifactInfo.model_validate(item) for item in payload]

    def sync(self) -> ArtifactSyncResult:
        return ArtifactSyncResult.model_validate(
            self._client.post_json("/v1/artifacts/sync", _mutation=True)
        )


class AsyncArtifactsNamespace(AsyncNamespace):
    async def list(self, prefix: str = "") -> list[ArtifactInfo]:
        payload = await self._client.get_json("/v1/artifacts", params={"prefix": prefix})
        return [ArtifactInfo.model_validate(item) for item in payload]

    async def read_bytes(self, path: str) -> bytes:
        return await self._client.get_bytes(_artifact_url(path))

    async def write_bytes(
        self,
        path: str,
        data: bytes,
        content_type: str | None = None,
    ) -> ArtifactInfo:
        headers = {"Content-Type": content_type or "application/octet-stream"}
        return ArtifactInfo.model_validate(
            await self._client.put_json(
                _artifact_url(path), content=data, headers=headers, _mutation=True
            )
        )

    async def download(self, path: str, local_path: str | Path) -> Path:
        return await self._client.download(_artifact_url(path), local_path)

    async def upload(self, local_path: str | Path, path: str) -> ArtifactInfo:
        import anyio

        data = await anyio.Path(local_path).read_bytes()
        return await self.write_bytes(path, data)

    async def delete(self, path: str) -> None:
        await self._client.delete_json(_artifact_url(path), _mutation=True)

    async def manifest(self, prefix: str = "") -> list[ArtifactInfo]:
        payload = await self._client.get_json("/v1/artifacts/manifest", params={"prefix": prefix})
        return [ArtifactInfo.model_validate(item) for item in payload]

    async def sync(self) -> ArtifactSyncResult:
        return ArtifactSyncResult.model_validate(
            await self._client.post_json("/v1/artifacts/sync", _mutation=True)
        )
