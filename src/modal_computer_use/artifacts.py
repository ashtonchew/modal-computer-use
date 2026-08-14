from __future__ import annotations

import contextlib
import hashlib
import json
import mimetypes
import os
import secrets
import stat
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
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
    if any(ord(char) < 32 or ord(char) == 127 for char in decoded):
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
        known_size_bytes: int | None = None,
        known_sha256: str | None = None,
        stat_result: os.stat_result | None = None,
    ) -> ArtifactInfo:
        metadata = stat_result or path.stat()
        kind = "directory" if stat.S_ISDIR(metadata.st_mode) else "file"
        digest = None
        size = None
        content_type = None
        if stat.S_ISREG(metadata.st_mode):
            if known_size_bytes is not None and known_sha256 is not None:
                size = known_size_bytes
                digest = known_sha256
            else:
                data = path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                size = len(data)
            content_type = mimetypes.guess_type(path.name)[0]
        return ArtifactInfo(
            path=public_path,
            uri=f"artifact://{public_path}",
            kind=kind,
            size_bytes=size,
            content_type=content_type,
            sha256=digest,
            created_at=datetime.fromtimestamp(metadata.st_ctime, tz=UTC),
            modified_at=datetime.fromtimestamp(metadata.st_mtime, tz=UTC),
            created_by_call_id=created_by_call_id,
            retention_class=retention_class,
        )

    def _open_directory_chain(self, relative: str, *, create: bool) -> int:
        """Open a directory below the artifact root without following links.

        Every component is opened relative to the descriptor for its parent.  A
        caller can therefore retain the returned descriptor across a pathname
        rename or symlink replacement without changing the directory it refers
        to.  Missing components are created only when explicitly requested.
        """
        nofollow = getattr(os, "O_NOFOLLOW", None)
        directory = getattr(os, "O_DIRECTORY", None)
        supports_dir_fd = getattr(os, "supports_dir_fd", ())
        required_dir_fd = (os.open, os.stat, os.mkdir, os.rename, os.unlink)
        if (
            nofollow is None
            or directory is None
            or not all(function in supports_dir_fd for function in required_dir_fd)
        ):
            raise ArtifactPathError("descriptor-relative artifact commits are unsupported")
        flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
        parts = tuple(Path(relative).parts) if relative else ()
        fd: int | None = None
        try:
            fd = os.open(self.root, flags)
            for part in parts:
                try:
                    child = os.open(part, flags, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise ArtifactPathError("artifact path changed during commit") from None
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(part, mode=0o755, dir_fd=fd)
                    child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            assert fd is not None
            result = fd
            fd = None
            return result
        except ArtifactPathError:
            raise
        except (NotImplementedError, OSError, TypeError, ValueError) as exc:
            raise ArtifactPathError("artifact path changed during commit") from exc
        finally:
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)

    def _relative_to_root(self, path: Path) -> str:
        root = Path(os.path.abspath(self.root))
        candidate = Path(os.path.abspath(path))
        try:
            relative = candidate.relative_to(root)
        except ValueError as exc:
            raise ArtifactPathError("staged artifact is outside the artifact root") from exc
        return "/".join(relative.parts)

    def commit_staged_upload(
        self,
        staged_path: Path,
        relative: str,
        *,
        size_bytes: int,
        sha256: str,
    ) -> _StagedArtifactCommit:
        """Atomically install a staged upload using descriptor-relative paths.

        The returned commit keeps both parent descriptors open until the caller
        either finalizes or rolls back.  This makes rollback safe even when an
        attacker replaces a pathname component after validation.
        """
        public_path = normalize_artifact_path(relative)
        target_parts = Path(public_path).parts
        if not target_parts:
            raise ArtifactPathError("artifact path must be relative and non-empty")
        target_name = target_parts[-1]
        target_parent = "/".join(target_parts[:-1])
        staged_relative = self._relative_to_root(staged_path)
        staged_parts = Path(staged_relative).parts
        if not staged_parts:
            raise ArtifactPathError("staged artifact path is invalid")
        staged_name = staged_parts[-1]
        staged_parent = "/".join(staged_parts[:-1])
        target_fd = self._open_directory_chain(target_parent, create=True)
        staged_fd: int | None = None
        backup_name: str | None = None
        target_handle: int | None = None
        installed = False
        try:
            staged_fd = self._open_directory_chain(staged_parent, create=False)
            staged_stat = os.stat(staged_name, dir_fd=staged_fd, follow_symlinks=False)
            if not stat.S_ISREG(staged_stat.st_mode):
                raise ArtifactPathError("staged artifact is not a regular file")
            try:
                target_stat = os.stat(target_name, dir_fd=target_fd, follow_symlinks=False)
            except FileNotFoundError:
                target_stat = None
            if target_stat is not None:
                if stat.S_ISLNK(target_stat.st_mode):
                    raise ArtifactPathError("artifact symlinks are not public")
                if stat.S_ISDIR(target_stat.st_mode):
                    raise ArtifactPathError("artifact path conflicts with an existing directory")
                if not stat.S_ISREG(target_stat.st_mode):
                    raise ArtifactPathError("artifact target is not a regular file")
                backup_name = f".backup-{secrets.token_hex(16)}"
                os.replace(
                    target_name,
                    backup_name,
                    src_dir_fd=target_fd,
                    dst_dir_fd=staged_fd,
                )
            os.replace(
                staged_name,
                target_name,
                src_dir_fd=staged_fd,
                dst_dir_fd=target_fd,
            )
            installed = True
            target_handle = os.open(
                target_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=target_fd,
            )
            target_stat = os.fstat(target_handle)
            info = self._info(
                self.root / public_path,
                public_path=public_path,
                known_size_bytes=size_bytes,
                known_sha256=sha256,
                stat_result=target_stat,
            )
            return _StagedArtifactCommit(
                target_fd=target_fd,
                staged_fd=staged_fd,
                target_name=target_name,
                backup_name=backup_name,
                info=info,
                target_handle=target_handle,
            )
        except BaseException:
            if backup_name is not None and staged_fd is not None:
                with contextlib.suppress(OSError):
                    os.replace(
                        backup_name,
                        target_name,
                        src_dir_fd=staged_fd,
                        dst_dir_fd=target_fd,
                    )
            elif installed:
                with contextlib.suppress(OSError):
                    os.unlink(target_name, dir_fd=target_fd)
            with contextlib.suppress(OSError):
                os.unlink(staged_name, dir_fd=staged_fd) if staged_fd is not None else None
            if target_handle is not None:
                with contextlib.suppress(OSError):
                    os.close(target_handle)
            if staged_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(staged_fd)
            with contextlib.suppress(OSError):
                os.close(target_fd)
            raise

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
            known_size_bytes=len(data),
            known_sha256=hashlib.sha256(data).hexdigest(),
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


@dataclass(slots=True)
class _StagedArtifactCommit:
    target_fd: int
    staged_fd: int
    target_name: str
    backup_name: str | None
    info: ArtifactInfo
    target_handle: int

    def rollback(self) -> None:
        try:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.target_name, dir_fd=self.target_fd)
            if self.backup_name is not None:
                os.replace(
                    self.backup_name,
                    self.target_name,
                    src_dir_fd=self.staged_fd,
                    dst_dir_fd=self.target_fd,
                )
        finally:
            self._close()

    def finalize(self) -> None:
        try:
            if self.backup_name is not None:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.backup_name, dir_fd=self.staged_fd)
        finally:
            self._close()

    def _close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self.target_handle)
        with contextlib.suppress(OSError):
            os.close(self.staged_fd)
        with contextlib.suppress(OSError):
            os.close(self.target_fd)


def _run_mountpoint_sync(path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed executable with daemon-owned mountpoint.
        ["/bin/sync", path],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
