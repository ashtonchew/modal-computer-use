from __future__ import annotations

import asyncio
import hashlib
import tempfile
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from modal_computer_use.artifacts import normalize_artifact_path
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.routes.execution import budget_policy, run_idle_only_mutation
from modal_computer_use.daemon.routes.validation import mutation_lock
from modal_computer_use.errors import ArtifactPathError
from modal_computer_use.models import ArtifactInfo, ArtifactSyncResult

router = APIRouter(prefix="/v1/artifacts")


@router.get("")
async def list_artifacts(request: Request, prefix: str = "") -> list[ArtifactInfo]:
    return request.app.state.artifacts.list(prefix)


@router.get("/manifest")
async def manifest(request: Request, prefix: str = "") -> list[ArtifactInfo]:
    return request.app.state.artifacts.manifest(prefix)


@router.post("/sync")
async def sync(request: Request) -> ArtifactSyncResult:
    async def operation() -> ArtifactSyncResult:
        with request.app.state.tracer.span("daemon.artifact.sync"):
            return await asyncio.to_thread(request.app.state.artifacts.sync)

    return await run_idle_only_mutation(request, operation, semantic_data={})


@router.get("/{path:path}")
async def read_artifact(path: str, request: Request) -> FileResponse:
    target = request.app.state.artifacts.resolve(path)
    if not target.is_file():
        raise FileNotFoundError(path)
    return FileResponse(target, media_type="application/octet-stream")


@router.put("/{path:path}")
async def write_artifact(path: str, request: Request) -> ArtifactInfo:
    try:
        public_path = normalize_artifact_path(path)
        content_length = request.headers.get("content-length")
        budget_policy(request).enforce_artifact_write(public_path, 0)
        if content_length is not None:
            try:
                incoming_size = int(content_length)
            except ValueError as exc:
                raise DaemonError(
                    "invalid Content-Length header",
                    status_code=400,
                    code="invalid_content_length",
                ) from exc
            budget_policy(request).enforce_artifact_write(public_path, incoming_size)
        store = request.app.state.artifacts
        target = store.resolve(public_path)
        _ensure_writable_artifact_target(target)
        temp_dir = store.resolve(".control/uploads", allow_empty=False, public=False)
        temp_dir.mkdir(parents=True, exist_ok=True)
        total = 0
        content_digest = hashlib.sha256()
        with request.app.state.tracer.span(
            "daemon.artifact.write",
            {"artifact.has_content_length": content_length is not None},
        ):
            with tempfile.NamedTemporaryFile(dir=temp_dir, delete=False) as handle:
                temp_path = Path(handle.name)
                try:
                    async for chunk in request.stream():
                        if not chunk:
                            continue
                        total += len(chunk)
                        budget_policy(request).enforce_artifact_write(public_path, total)
                        content_digest.update(chunk)
                        handle.write(chunk)
                except Exception:
                    temp_path.unlink(missing_ok=True)
                    raise
            try:
                async with mutation_lock(
                    request,
                    semantic_data={
                        "path": public_path,
                        "size_bytes": total,
                        "content_sha256": content_digest.hexdigest(),
                    },
                ):
                    target = store.resolve(public_path)
                    _ensure_writable_artifact_target(target)
                    _reject_active_recording_target(request, public_path)
                    budget_policy(request).enforce_artifact_write(public_path, total)
                    store._enforce_write_budget(target, total)
                    manifest_existed = store.manifest_path.exists()
                    manifest_size = (
                        store.manifest_path.stat().st_size if manifest_existed else 0
                    )
                    commit = None
                    try:
                        commit = store.commit_staged_upload(
                            temp_path,
                            public_path,
                            size_bytes=total,
                            sha256=content_digest.hexdigest(),
                        )
                        info = commit.info
                        content_type = request.headers.get("content-type")
                        if content_type:
                            info.content_type = content_type
                        store.append_manifest(info)
                        budget_policy(request).enforce("artifacts")
                    except Exception:
                        if commit is not None:
                            commit.rollback()
                        _restore_manifest(
                            store.manifest_path,
                            existed=manifest_existed,
                            size=manifest_size,
                        )
                        raise
                    else:
                        assert commit is not None
                        commit.finalize()
                        budget_policy(request).touch_activity()
                        return info
            finally:
                temp_path.unlink(missing_ok=True)
    except ArtifactPathError:
        raise


def _ensure_writable_artifact_target(target: Path) -> None:
    if target.exists() and target.is_dir():
        raise DaemonError(
            "artifact path conflicts with an existing directory",
            status_code=409,
            code="artifact_path_conflict",
        )
    parent = target.parent
    if parent.exists() and not parent.is_dir():
        raise DaemonError(
            "artifact parent path conflicts with an existing file",
            status_code=409,
            code="artifact_path_conflict",
        )


def _restore_manifest(path: Path, *, existed: bool, size: int) -> None:
    if not existed:
        path.unlink(missing_ok=True)
        return
    if not path.exists():
        return
    with path.open("r+b") as handle:
        handle.truncate(size)


def _reject_active_recording_target(request: Request, public_path: str) -> None:
    if request.app.state.recordings.is_active_artifact_path(public_path):
        raise DaemonError(
            "artifact is owned by an active recording",
            status_code=409,
            code="artifact_in_use",
        )


@router.delete("/{path:path}")
async def delete_artifact(path: str, request: Request) -> dict[str, bool]:
    async def operation() -> dict[str, bool]:
        public_path = normalize_artifact_path(path)
        _reject_active_recording_target(request, public_path)
        request.app.state.artifacts.delete(public_path)
        return {"ok": True}

    return await run_idle_only_mutation(
        request,
        operation,
        semantic_data={"path": path},
    )
