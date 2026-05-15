from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from json import JSONDecodeError
from pathlib import Path
from urllib.parse import unquote

from .errors import ArtifactPathError, BudgetExceededError
from .models import ArtifactInfo, ArtifactSyncResult

CONTROL_PATHS = {
    "manifest.ndjson",
    "traces/actions.ndjson",
}
CONTROL_SEGMENTS = {".control", "_control", ".modal-computer-use", ".secrets", "logs"}


def normalize_artifact_path(path: str, *, allow_empty: bool = False, public: bool = True) -> str:
    if path is None:
        raise ArtifactPathError("artifact path is required")
    raw = str(path).replace("\\", "/")
    previous = None
    decoded = raw
    for _ in range(3):
        if decoded == previous:
            break
        previous = decoded
        decoded = unquote(decoded)
    if decoded.startswith("/") or decoded.startswith("~"):
        raise ArtifactPathError("absolute artifact paths are not allowed")
    decoded = decoded.strip("/")
    if not decoded:
        if allow_empty:
            return ""
        raise ArtifactPathError("artifact path must be relative and non-empty")
    if "\x00" in decoded or any(ord(char) < 32 for char in decoded):
        raise ArtifactPathError("artifact path contains control characters")
    parts = [part for part in decoded.split("/") if part]
    if any(part in (".", "..") for part in parts):
        raise ArtifactPathError("artifact path traversal is not allowed")
    if public:
        lowered = "/".join(parts).lower()
        if lowered in CONTROL_PATHS or any(part.lower() in CONTROL_SEGMENTS for part in parts):
            raise ArtifactPathError("artifact control paths are not public")
    return "/".join(parts)


class ArtifactStore:
    def __init__(
        self,
        root: str | Path,
        *,
        persistent: bool = False,
        persistent_verified: bool | None = None,
        max_total_bytes: int | None = None,
        sync_runner: Callable[[str], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.root = Path(root)
        self.persistent = persistent
        self.persistent_verified = (
            persistent if persistent_verified is None else persistent_verified
        )
        self.max_total_bytes = max_total_bytes
        self._sync_runner = sync_runner or _run_mountpoint_sync
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.ndjson"

    def resolve(self, path: str, *, allow_empty: bool = False, public: bool = True) -> Path:
        relative = normalize_artifact_path(path, allow_empty=allow_empty, public=public)
        self._reject_symlink_components(relative)
        candidate = (self.root / relative).resolve()
        root = self.root.resolve()
        try:
            common = os.path.commonpath([str(root), str(candidate)])
        except ValueError as exc:
            raise ArtifactPathError("artifact path escapes root") from exc
        if common != str(root):
            raise ArtifactPathError("artifact path escapes root")
        return candidate

    def _reject_symlink_components(self, relative: str) -> None:
        if not relative:
            return
        current = self.root
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink():
                raise ArtifactPathError("artifact symlinks are not public")
            if not current.exists():
                return

    def _info(
        self,
        path: Path,
        *,
        public_path: str,
        created_by_call_id: str | None = None,
        retention_class: str = "ephemeral",
    ) -> ArtifactInfo:
        stat = path.stat()
        kind = "directory" if path.is_dir() else "file"
        digest = None
        size = None
        content_type = None
        if path.is_file():
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            size = len(data)
            content_type = mimetypes.guess_type(path.name)[0]
        return ArtifactInfo(
            path=public_path,
            uri=f"artifact://{public_path}",
            kind=kind,  # type: ignore[arg-type]
            size_bytes=size,
            content_type=content_type,
            sha256=digest,
            created_at=datetime.fromtimestamp(stat.st_ctime, tz=UTC),
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            created_by_call_id=created_by_call_id,
            retention_class=retention_class,  # type: ignore[arg-type]
        )

    def write_bytes(
        self,
        path: str,
        data: bytes,
        *,
        content_type: str | None = None,
        created_by_call_id: str | None = None,
        retention_class: str = "ephemeral",
        append_manifest: bool = True,
    ) -> ArtifactInfo:
        relative = normalize_artifact_path(path)
        target = self.resolve(relative)
        self._enforce_write_budget(target, len(data))
        parent = target.parent.resolve()
        root = self.root.resolve()
        if os.path.commonpath([str(root), str(parent)]) != str(root):
            raise ArtifactPathError("artifact parent escapes root")
        parent.mkdir(parents=True, exist_ok=True)
        self._reject_symlink_components(relative)
        with tempfile.NamedTemporaryFile(dir=parent, delete=False) as handle:
            temp_path = Path(handle.name)
            try:
                handle.write(data)
            except Exception:
                temp_path.unlink(missing_ok=True)
                raise
        try:
            self._reject_symlink_components(relative)
            temp_path.replace(target)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        info = self._info(
            target,
            public_path=relative,
            created_by_call_id=created_by_call_id,
            retention_class=retention_class,
        )
        if content_type:
            info.content_type = content_type
        if append_manifest:
            self.append_manifest(info)
        return info

    def write_file(
        self,
        path: str,
        source: str | Path,
        *,
        content_type: str | None = None,
        created_by_call_id: str | None = None,
        retention_class: str = "ephemeral",
        append_manifest: bool = True,
    ) -> ArtifactInfo:
        relative = normalize_artifact_path(path)
        target = self.resolve(relative)
        source_path = Path(source)
        self._enforce_write_budget(target, source_path.stat().st_size)
        parent = target.parent.resolve()
        root = self.root.resolve()
        if os.path.commonpath([str(root), str(parent)]) != str(root):
            raise ArtifactPathError("artifact parent escapes root")
        parent.mkdir(parents=True, exist_ok=True)
        self._reject_symlink_components(relative)
        with tempfile.NamedTemporaryFile(dir=parent, delete=False) as handle:
            temp_path = Path(handle.name)
        try:
            shutil.copyfile(source_path, temp_path)
            self._reject_symlink_components(relative)
            temp_path.replace(target)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        info = self._info(
            target,
            public_path=relative,
            created_by_call_id=created_by_call_id,
            retention_class=retention_class,
        )
        if content_type:
            info.content_type = content_type
        if append_manifest:
            self.append_manifest(info)
        return info

    def read_bytes(self, path: str) -> bytes:
        target = self.resolve(path)
        if not target.is_file():
            raise FileNotFoundError(path)
        return target.read_bytes()

    def delete(self, path: str) -> None:
        target = self.resolve(path)
        if not target.exists():
            raise FileNotFoundError(path)
        if target.is_dir():
            target.rmdir()
        else:
            target.unlink()

    def list(self, prefix: str = "") -> list[ArtifactInfo]:
        safe_prefix = normalize_artifact_path(prefix, allow_empty=True, public=True)
        base = self.resolve(safe_prefix, allow_empty=True, public=False)
        if not base.exists():
            return []
        root = self.root.resolve()
        paths = [base] if base.is_file() else [item for item in base.rglob("*") if item.exists()]
        infos: list[ArtifactInfo] = []
        for item in sorted(paths):
            if item.is_symlink():
                raise ArtifactPathError("artifact symlinks are not public")
            resolved = item.resolve()
            common = os.path.commonpath([str(root), str(resolved)])
            if common != str(root):
                raise ArtifactPathError("artifact symlink escapes are not allowed")
            relative = os.path.relpath(str(resolved), str(root)).replace(os.sep, "/")
            try:
                public_path = normalize_artifact_path(relative, public=True)
            except ArtifactPathError:
                continue
            infos.append(self._info(item, public_path=public_path))
        return infos

    def total_public_bytes(self) -> int:
        root = self.root.resolve()
        total = 0
        if not self.root.exists():
            return total
        for item in self.root.rglob("*"):
            if item.is_symlink():
                continue
            try:
                resolved = item.resolve()
                if os.path.commonpath([str(root), str(resolved)]) != str(root):
                    continue
            except (OSError, ValueError):
                continue
            if not item.is_file():
                continue
            relative = os.path.relpath(str(resolved), str(root)).replace(os.sep, "/")
            try:
                normalize_artifact_path(relative, public=True)
            except ArtifactPathError:
                continue
            total += item.stat().st_size
        return total

    def _enforce_write_budget(self, target: Path, incoming_size: int) -> None:
        if self.max_total_bytes is None:
            return
        root = self.root.resolve()
        existing_total = 0
        if self.root.exists():
            for item in self.root.rglob("*"):
                try:
                    resolved = item.resolve()
                    if os.path.commonpath([str(root), str(resolved)]) != str(root):
                        continue
                except (OSError, ValueError):
                    continue
                if item.is_file():
                    relative = os.path.relpath(str(resolved), str(root)).replace(os.sep, "/")
                    try:
                        normalize_artifact_path(relative, public=True)
                    except ArtifactPathError:
                        continue
                    existing_total += item.stat().st_size
        existing_target_size = target.stat().st_size if target.is_file() else 0
        if existing_total - existing_target_size + incoming_size > self.max_total_bytes:
            raise BudgetExceededError("artifact byte budget exceeded")

    def append_manifest(self, info: ArtifactInfo) -> None:
        payload = {"ts": datetime.now(UTC).isoformat(), **info.model_dump(mode="json")}
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with self.manifest_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, separators=(",", ":")) + "\n")

    def manifest(self, prefix: str = "") -> list[ArtifactInfo]:
        if not self.manifest_path.exists():
            return []
        safe_prefix = normalize_artifact_path(prefix, allow_empty=True, public=True)
        entries: list[ArtifactInfo] = []
        for line in self.manifest_path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            try:
                data = json.loads(line)
            except JSONDecodeError:
                continue
            data.pop("ts", None)
            path = data.get("path", "")
            try:
                public_path = normalize_artifact_path(path, public=True)
            except ArtifactPathError:
                continue
            if data.get("uri") != f"artifact://{public_path}":
                continue
            data["path"] = public_path
            data["uri"] = f"artifact://{public_path}"
            if (
                safe_prefix
                and public_path != safe_prefix
                and not public_path.startswith(f"{safe_prefix}/")
            ):
                continue
            try:
                entries.append(ArtifactInfo.model_validate(data))
            except ValueError:
                continue
        return entries

    def sync(self) -> ArtifactSyncResult:
        if self.persistent:
            if not self.persistent_verified:
                return ArtifactSyncResult(
                    ok=False,
                    persistent=True,
                    synced_paths=[],
                    message=(
                        "persistent artifact sync requested without a verified Modal Volume "
                        "mount for the artifact root"
                    ),
                )
            result = self._sync_runner(str(self.root))
            if result.returncode != 0:
                return ArtifactSyncResult(
                    ok=False,
                    persistent=True,
                    synced_paths=[],
                    message="Modal Volume v2 sync failed inside the sandbox",
                )
            return ArtifactSyncResult(
                ok=True,
                persistent=True,
                synced_paths=["artifact-root"],
                message="Modal Volume v2 mountpoint synced",
            )
        return ArtifactSyncResult(
            ok=True,
            persistent=self.persistent,
            synced_paths=[],
            message="artifact sync is a no-op without configured Modal Volume semantics",
        )


def _run_mountpoint_sync(path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed executable with daemon-owned mountpoint.
        ["/bin/sync", path],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
