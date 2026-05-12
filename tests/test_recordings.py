from __future__ import annotations

import subprocess

from modal_computer_use.daemon.desktop import recordings as recordings_module
from modal_computer_use.daemon.desktop.recordings import RecordingRegistry
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.models import Recording


def test_recording_stop_updates_file_metadata_and_download(test_client) -> None:
    started = Recording.model_validate(
        test_client.post("/v1/recordings", json={"name": "demo", "fps": 5}).json()
    )

    stopped = Recording.model_validate(
        test_client.post(f"/v1/recordings/{started.id}/stop").json()
    )

    assert stopped.status == "stopped"
    assert stopped.size_bytes > 0
    assert stopped.sha256
    assert stopped.duration_seconds is not None
    download = test_client.get(f"/v1/recordings/{started.id}/download")
    assert download.status_code == 200
    assert download.content.startswith(b"mock recording")


def test_recording_delete_removes_metadata(test_client) -> None:
    started = test_client.post("/v1/recordings", json={}).json()

    assert test_client.delete(f"/v1/recordings/{started['id']}").json() == {"ok": True}
    assert test_client.get(f"/v1/recordings/{started['id']}").status_code == 404


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, value: bytes) -> None:
        self.writes.append(value)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _StubbornProcess:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.signals: list[int] = []
        self.killed = False
        self._poll: int | None = None
        self._waits = 0

    def poll(self) -> int | None:
        return self._poll

    def wait(self, timeout: float | None = None) -> int:
        self._waits += 1
        if self._waits <= 2:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        self._poll = -9
        return self._poll

    def send_signal(self, value: int) -> None:
        self.signals.append(value)

    def kill(self) -> None:
        self.killed = True


def test_failed_recording_stop_reports_error_and_ffmpeg_tail(tmp_path, monkeypatch) -> None:
    process = _StubbornProcess()

    def fake_popen(*args, **kwargs):
        kwargs["stderr"].write(b"first line\nlast ffmpeg error\n")
        return process

    monkeypatch.setattr(recordings_module.shutil, "which", lambda _tool: "/usr/bin/ffmpeg")
    registry = RecordingRegistry(
        DaemonSettings(
            backend="x11",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        ),
        popen_factory=fake_popen,
    )

    started = registry.start(name="broken")
    stopped = registry.stop(started.id)

    assert stopped.status == "failed"
    assert stopped.error == "ffmpeg did not stop after SIGTERM; killed process"
    assert stopped.stderr_tail == ["first line", "last ffmpeg error"]
    assert process.stdin.writes == [b"q\n"]
    assert process.stdin.closed is True
    assert process.killed is True


def test_recording_delete_removes_stderr_file(tmp_path, monkeypatch) -> None:
    class ExitedProcess:
        stdin = None

        def poll(self) -> int:
            return 0

        def terminate(self) -> None:
            raise AssertionError("already exited")

    def fake_popen(*args, **kwargs):
        kwargs["stderr"].write(b"ffmpeg startup\n")
        return ExitedProcess()

    monkeypatch.setattr(recordings_module.shutil, "which", lambda _tool: "/usr/bin/ffmpeg")
    registry = RecordingRegistry(
        DaemonSettings(
            backend="x11",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        ),
        popen_factory=fake_popen,
    )

    started = registry.start()
    stderr_path = started.stderr_path

    assert stderr_path is not None
    assert (tmp_path / "recordings" / f"{started.id}.ffmpeg.stderr.log").exists()
    registry.delete(started.id)
    assert not (tmp_path / "recordings" / f"{started.id}.ffmpeg.stderr.log").exists()
