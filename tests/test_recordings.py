from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from modal_computer_use.daemon.app import create_app
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
    assert stopped.stop_method == "mock"
    assert "-stdin" in stopped.ffmpeg_args
    download = test_client.get(f"/v1/recordings/{started.id}/download")
    assert download.status_code == 200
    assert download.content.startswith(b"mock recording")
    artifact = test_client.get(f"/v1/artifacts/recordings/{started.id}.mp4")
    assert artifact.status_code == 200
    assert artifact.content == download.content

    manifest = test_client.get("/v1/artifacts/manifest").json()
    recording_entries = [
        item for item in manifest if item["path"] == f"recordings/{started.id}.mp4"
    ]
    assert len(recording_entries) == 1
    assert recording_entries[0]["content_type"] == "video/mp4"


def test_recording_delete_removes_metadata(test_client) -> None:
    started = test_client.post("/v1/recordings", json={}).json()

    assert test_client.delete(f"/v1/recordings/{started['id']}").json() == {"ok": True}
    assert test_client.get(f"/v1/recordings/{started['id']}").status_code == 404


def test_recording_registry_shutdown_stops_all_active_recordings(tmp_path) -> None:
    registry = RecordingRegistry(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )
    first = registry.start()
    second = registry.start()

    registry.shutdown()

    assert registry.get(first.id).status == "stopped"
    assert registry.get(second.id).status == "stopped"


def test_daemon_lifespan_shuts_down_recordings(tmp_path, monkeypatch) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )
    called = False

    def shutdown() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(app.state.recordings, "shutdown", shutdown)

    with TestClient(app):
        pass

    assert called is True


def test_daemon_lifespan_closes_backend_before_stopping_display(tmp_path, monkeypatch) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )
    events: list[str] = []

    async def start() -> None:
        events.append("start")

    async def stop() -> None:
        events.append("stop")

    def close() -> None:
        events.append("close")

    monkeypatch.setattr(app.state.supervisor, "start", start)
    monkeypatch.setattr(app.state.supervisor, "stop", stop)
    monkeypatch.setattr(app.state.backend, "close", close)

    with TestClient(app):
        pass

    assert events[:1] == ["start"]
    assert events.index("close") < events.index("stop")


def test_recording_dashboard_requires_same_auth(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )

    with TestClient(app) as client:
        response = client.get("/recordings/ui")

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_recording_dashboard_renders_bounded_metadata_only(test_client) -> None:
    started = Recording.model_validate(
        test_client.post("/v1/recordings", json={"name": "demo", "fps": 5}).json()
    )
    stopped = Recording.model_validate(
        test_client.post(f"/v1/recordings/{started.id}/stop").json()
    )

    response = test_client.get("/recordings/ui")

    assert response.status_code == 200
    body = response.text
    assert "demo" in body
    assert str(stopped.size_bytes) in body
    assert stopped.sha256 in body
    assert "artifact://" not in body
    assert stopped.artifact_uri not in body
    assert f"/v1/recordings/{started.id}/download" in body
    assert f"/v1/recordings/{started.id}\">metadata" not in body
    assert f'href="/v1/recordings/{started.id}" data-method="DELETE"' in body
    assert stopped.path not in body
    assert "ffmpeg_args" not in body
    assert "mock recording" not in body
    assert "stderr_tail" not in body


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


class _BrokenWaitProcess:
    def __init__(self) -> None:
        self.stdin = None
        self.terminated = False
        self.killed = False
        self._waits = 0

    def poll(self) -> int | None:
        return -15 if self.terminated else None

    def wait(self, timeout: float | None = None) -> int:
        self._waits += 1
        if self._waits == 1:
            raise OSError("wait failed")
        return -15

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class _SlowLifecycleProcess:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self._return_code: int | None = None
        self.wait_thread_ids: list[int] = []

    def poll(self) -> int | None:
        return self._return_code

    def terminate(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.wait_thread_ids.append(threading.get_ident())
        time.sleep(0.08)
        self._return_code = 0
        return 0

    def kill(self) -> None:
        self._return_code = -9


@pytest.mark.parametrize("operation", ["stop", "delete"])
def test_recording_lifecycle_routes_do_not_block_event_loop(
    tmp_path, monkeypatch, operation: str
) -> None:
    process = _SlowLifecycleProcess()
    monkeypatch.setattr(recordings_module.shutil, "which", lambda _tool: "/usr/bin/ffmpeg")
    settings = DaemonSettings(
        backend="mock",
        artifacts_dir=tmp_path / "artifacts",
        recordings_dir=tmp_path / "recordings",
        runtime_dir=tmp_path / "runtime",
        local_token="dev",
    )
    app = create_app(settings)
    recording_settings = replace(settings, backend="x11")
    registry = RecordingRegistry(
        recording_settings,
        artifact_store=app.state.artifacts,
        popen_factory=lambda *args, **kwargs: process,
    )
    app.state.recordings = registry
    event_loop_thread_id = threading.get_ident()

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            started = registry.start()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                headers={"Authorization": "Bearer dev"},
            ) as client:
                response = await client.request(
                    "POST" if operation == "stop" else "DELETE",
                    f"/v1/recordings/{started.id}/stop"
                    if operation == "stop"
                    else f"/v1/recordings/{started.id}",
                )
                assert response.status_code == 200
                assert process.wait_thread_ids
                assert all(
                    thread_id != event_loop_thread_id
                    for thread_id in process.wait_thread_ids
                )

    asyncio.run(exercise())


def test_recording_start_budget_rollback_does_not_block_event_loop(
    tmp_path, monkeypatch
) -> None:
    process = _SlowLifecycleProcess()
    monkeypatch.setattr(recordings_module.shutil, "which", lambda _tool: "/usr/bin/ffmpeg")
    settings = DaemonSettings(
        backend="mock",
        artifacts_dir=tmp_path / "artifacts",
        recordings_dir=tmp_path / "recordings",
        runtime_dir=tmp_path / "runtime",
        local_token="dev",
        max_recording_seconds=1e-9,
    )
    app = create_app(settings)
    app.state.recordings = RecordingRegistry(
        replace(settings, backend="x11"),
        artifact_store=app.state.artifacts,
        popen_factory=lambda *args, **kwargs: process,
    )

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                headers={"Authorization": "Bearer dev"},
            ) as client:
                timer_delay: list[float] = []
                original_enforce = app.state.budget_policy.enforce

                def enforce(*kinds: str) -> None:
                    if kinds == ("recordings",):
                        started_at = time.perf_counter()
                        asyncio.get_running_loop().call_later(
                            0.01,
                            lambda: timer_delay.append(time.perf_counter() - started_at),
                        )
                    original_enforce(*kinds)

                app.state.budget_policy.enforce = enforce
                response = await client.post("/v1/recordings", json={})
                await asyncio.sleep(0.02)

        assert response.status_code == 429
        assert timer_delay[0] < 0.05

    asyncio.run(exercise())


def test_recording_shutdown_force_stops_process_after_stop_error(tmp_path, monkeypatch) -> None:
    process = _BrokenWaitProcess()

    monkeypatch.setattr(recordings_module.shutil, "which", lambda _tool: "/usr/bin/ffmpeg")
    registry = RecordingRegistry(
        DaemonSettings(
            backend="x11",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        ),
        popen_factory=lambda *args, **kwargs: process,
    )
    started = registry.start(name="broken-wait")

    registry.shutdown()

    assert process.terminated is True
    assert process.killed is False
    assert started.id not in registry._processes


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
    assert stopped.return_code == -9
    assert stopped.stop_method == "kill"
    assert stopped.stderr_tail == ["first line", "last ffmpeg error"]
    assert process.stdin.writes == [b"q\n"]
    assert process.stdin.closed is True
    assert process.killed is True


def test_failed_recording_stop_sanitizes_secret_bearing_ffmpeg_tail(
    tmp_path, monkeypatch
) -> None:
    process = _StubbornProcess()

    def fake_popen(*args, **kwargs):
        kwargs["stderr"].write(
            b"Bearer recording-secret\nartifact://logs/recording-secret.txt\n"
        )
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

    assert stopped.stderr_tail == ["Bearer [redacted]", "[redacted]"]
    assert "recording-secret" not in stopped.model_dump_json()


def test_recording_start_failure_redacts_stderr_path(tmp_path, monkeypatch) -> None:
    def failing_popen(*args, **kwargs):
        raise OSError(f"cannot open {tmp_path}/recordings/raw-secret.stderr.log")

    monkeypatch.setattr(recordings_module.shutil, "which", lambda _tool: "/usr/bin/ffmpeg")
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        )
    )
    app.state.recordings = RecordingRegistry(
        DaemonSettings(
            backend="x11",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
        ),
        artifact_store=app.state.artifacts,
        popen_factory=failing_popen,
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post("/v1/recordings", json={"name": "broken"})

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "recording_start_failed"
    serialized = response.text
    assert str(tmp_path) not in serialized
    assert "raw-secret" not in serialized
    assert body["details"]["error_type"] == "OSError"
    assert body["details"]["stderr_path"]["redacted"] is True


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


def test_recording_start_uses_deterministic_ffmpeg_argv(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class ExitedProcess:
        stdin = None

        def poll(self) -> int:
            return 0

    def fake_popen(args, **kwargs):
        captured["args"] = args
        return ExitedProcess()

    monkeypatch.setattr(recordings_module.shutil, "which", lambda _tool: "/usr/bin/ffmpeg")
    registry = RecordingRegistry(
        DaemonSettings(
            backend="x11",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            local_token="dev",
            display=":100",
            desktop_width=800,
            desktop_height=600,
        ),
        popen_factory=fake_popen,
    )

    started = registry.start(fps=7)

    assert captured["args"] == [
        "/usr/bin/ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostats",
        "-stdin",
        "-video_size",
        "800x600",
        "-framerate",
        "7",
        "-f",
        "x11grab",
        "-i",
        ":100",
        "-pix_fmt",
        "yuv420p",
        started.path,
    ]
    assert started.ffmpeg_args[0] == "ffmpeg"
    assert started.ffmpeg_args[-1] == started.path
