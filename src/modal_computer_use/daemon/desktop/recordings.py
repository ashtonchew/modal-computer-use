from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.models import Recording


class RecordingRegistry:
    def __init__(
        self,
        settings: DaemonSettings,
        *,
        popen_factory: Callable[..., subprocess.Popen[bytes]] | None = None,
    ) -> None:
        self.settings = settings
        self._popen_factory = popen_factory or subprocess.Popen
        self._recordings: dict[str, Recording] = {}
        self._processes: dict[str, Any] = {}
        self._stderr_paths: dict[str, Path] = {}
        self.settings.recordings_dir.mkdir(parents=True, exist_ok=True)

    def start(self, *, name: str | None = None, fps: int = 12, format: str = "mp4") -> Recording:
        if format != "mp4":
            raise DaemonError(
                "only mp4 recordings are currently supported",
                code="unsupported_format",
            )
        recording_id = f"rec_{uuid4().hex[:12]}"
        path = self.settings.recordings_dir / f"{recording_id}.{format}"
        stderr_path = self.settings.recordings_dir / f"{recording_id}.ffmpeg.stderr.log"
        rec = Recording(
            id=recording_id,
            name=name,
            status="recording",
            format=format,
            fps=fps,
            path=str(path),
            artifact_uri=f"artifact://recordings/{recording_id}.{format}",
            size_bytes=0,
            stderr_path=str(stderr_path),
            started_at=datetime.now(UTC),
        )
        if self.settings.backend != "mock":
            ffmpeg_path = shutil.which("ffmpeg")
            if ffmpeg_path is None:
                raise DaemonError("ffmpeg is not installed", code="recording_start_failed")
            env = dict(os.environ)
            env["DISPLAY"] = self.settings.display
            try:
                with stderr_path.open("ab") as stderr_handle:
                    self._processes[recording_id] = self._popen_factory(
                        [
                            ffmpeg_path,
                            "-y",
                            "-hide_banner",
                            "-loglevel",
                            "warning",
                            "-nostats",
                            "-stdin",
                            "-video_size",
                            f"{self.settings.desktop_width}x{self.settings.desktop_height}",
                            "-framerate",
                            str(fps),
                            "-f",
                            "x11grab",
                            "-i",
                            self.settings.display,
                            "-pix_fmt",
                            "yuv420p",
                            str(path),
                        ],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=stderr_handle,
                        env=env,
                        start_new_session=True,
                    )
            except OSError as exc:
                raise DaemonError(
                    "failed to start ffmpeg recording",
                    code="recording_start_failed",
                    details={"error": str(exc), "stderr_path": str(stderr_path)},
                ) from exc
            self._stderr_paths[recording_id] = stderr_path
        self._recordings[recording_id] = rec
        return rec

    def stop(self, recording_id: str) -> Recording:
        rec = self.get(recording_id)
        if rec.status != "recording":
            return rec
        path = Path(rec.path)
        process = self._processes.pop(recording_id, None)
        error = None
        if process is not None and process.poll() is None:
            if process.stdin is not None:
                try:
                    process.stdin.write(b"q\n")
                    process.stdin.flush()
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    with suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=5)
                    error = "ffmpeg did not stop after SIGTERM; killed process"
        elif self.settings.backend == "mock" and not path.exists():
            path.write_bytes(f"mock recording {recording_id}\n".encode())
        if process is not None and process.poll() not in (0, None):
            error = error or f"ffmpeg exited with status {process.poll()}"
        stopped_at = datetime.now(UTC)
        size_bytes = path.stat().st_size if path.exists() else 0
        digest = _sha256_file(path) if path.exists() else None
        status = "stopped" if size_bytes > 0 else "failed"
        if status == "failed":
            error = error or "recording file was not created"
        stderr_path = (
            Path(rec.stderr_path) if rec.stderr_path else self._stderr_paths.get(recording_id)
        )
        stderr_tail = _tail_file(stderr_path) if stderr_path is not None else []
        stopped = rec.model_copy(
            update={
                "status": status,
                "stopped_at": stopped_at,
                "duration_seconds": (stopped_at - rec.started_at).total_seconds(),
                "size_bytes": size_bytes,
                "sha256": digest,
                "stderr_tail": stderr_tail,
                "error": error,
            }
        )
        self._recordings[recording_id] = stopped
        return stopped

    def list(self) -> list[Recording]:
        return list(self._recordings.values())

    def get(self, recording_id: str) -> Recording:
        try:
            return self._recordings[recording_id]
        except KeyError as exc:
            raise FileNotFoundError(recording_id) from exc

    def delete(self, recording_id: str) -> None:
        rec = self.get(recording_id)
        process = self._processes.pop(recording_id, None)
        if process is not None and process.poll() is None:
            process.terminate()
        Path(rec.path).unlink(missing_ok=True)
        if rec.stderr_path:
            Path(rec.stderr_path).unlink(missing_ok=True)
        self._stderr_paths.pop(recording_id, None)
        del self._recordings[recording_id]

    def total_size_bytes(self) -> int:
        total = 0
        for rec in self._recordings.values():
            path = Path(rec.path)
            total += path.stat().st_size if path.exists() else rec.size_bytes
        return total

    def total_duration_seconds(self) -> float:
        total = 0.0
        now = datetime.now(UTC)
        for rec in self._recordings.values():
            if rec.duration_seconds is not None:
                total += rec.duration_seconds
            elif rec.status == "recording":
                total += (now - rec.started_at).total_seconds()
        return total


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tail_file(path: Path | None, *, max_lines: int = 20, max_chars: int = 4096) -> list[str]:
    if path is None or not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")[-max_chars:]
    return text.splitlines()[-max_lines:]
