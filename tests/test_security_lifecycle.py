from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from modal_computer_use.borrowed import AsyncBorrowedComputer, BorrowedComputer
from modal_computer_use.client import AsyncDaemonClient, DaemonClient
from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.process_environment import (
    daemon_process_command,
    desktop_process_command,
    desktop_process_environment,
    prepare_desktop_output_file,
)
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.daemon.supervisor import Supervisor
from modal_computer_use.image import _credential_boundary_commands
from modal_computer_use.session_lease import AsyncSessionLeaseCoordinator, SessionLeaseCoordinator
from modal_computer_use.transports.http import AsyncHTTPTransport, HTTPTransport


def _settings(tmp_path, **overrides) -> DaemonSettings:
    values = {
        "backend": "mock",
        "artifacts_dir": tmp_path / "artifacts",
        "recordings_dir": tmp_path / "recordings",
        "runtime_dir": tmp_path / "runtime",
        "local_token": "local-owner",
    }
    values.update(overrides)
    return DaemonSettings(**values)


def test_lifecycle_requires_owner_proof_for_connect_and_minted_auth(tmp_path) -> None:
    app = create_app(_settings(tmp_path, local_token=None, tunnel_token="bootstrap-owner"))
    with TestClient(app) as client:
        bootstrap_owner = client.post(
            "/v1/computer/stop",
            headers={"Authorization": "Bearer bootstrap-owner"},
        )
        connect_missing = client.post(
            "/v1/computer/stop",
            headers={
                "X-Verified-User-Data": '{"sdk":"modal-computer-use"}',
            },
        )
        connect_valid = client.post(
            "/v1/computer/stop",
            headers={
                "X-Verified-User-Data": '{"sdk":"modal-computer-use"}',
                "X-Computer-Use-Owner-Proof": "bootstrap-owner",
            },
        )
        minted_response = client.post(
            "/v1/session/tunnel-authorize",
            headers={"Authorization": "Bearer bootstrap-owner"},
        )
        minted_token = minted_response.json()["token"]
        minted_missing = client.post(
            "/v1/computer/restart",
            headers={"Authorization": f"Bearer {minted_token}"},
        )
        minted_process_missing = client.post(
            "/v1/processes/xvfb/restart",
            headers={"Authorization": f"Bearer {minted_token}"},
        )
        connect_process_missing = client.post(
            "/v1/processes/xvfb/restart",
            headers={"X-Verified-User-Data": '{"sdk":"modal-computer-use"}'},
        )
        minted_valid = client.post(
            "/v1/computer/restart",
            headers={
                "Authorization": f"Bearer {minted_token}",
                "X-Computer-Use-Owner-Proof": "bootstrap-owner",
            },
        )

    assert bootstrap_owner.status_code == 200
    assert connect_missing.status_code == 403
    assert connect_missing.json()["code"] == "owner_authorization_required"
    assert connect_valid.status_code == 200
    assert minted_missing.status_code == 403
    assert minted_missing.json()["code"] == "owner_authorization_required"
    assert minted_process_missing.status_code == 403
    assert minted_process_missing.json()["code"] == "owner_authorization_required"
    assert connect_process_missing.status_code == 403
    assert connect_process_missing.json()["code"] == "owner_authorization_required"
    assert minted_valid.status_code == 200


def test_local_auth_is_an_owner_capability_without_duplicate_proof_header(tmp_path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/v1/computer/stop",
            headers={"Authorization": "Bearer local-owner"},
        )

    assert response.status_code == 200


def test_process_restart_accepts_local_owner_capability(tmp_path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        missing = client.post(
            "/v1/processes/xvfb/restart",
            headers={"Authorization": "Bearer local-owner"},
        )
        valid = client.post(
            "/v1/processes/xvfb/restart",
            headers={
                "Authorization": "Bearer local-owner",
                "X-Computer-Use-Owner-Proof": "local-owner",
            },
        )

    assert missing.status_code == 200
    assert valid.status_code == 200


@pytest.mark.asyncio
async def test_supervisor_start_rolls_back_partial_display_stack(tmp_path) -> None:
    settings = replace(_settings(tmp_path), backend="x11")
    supervisor = Supervisor(settings)
    events: list[str] = []

    class Process:
        _next_pid = 100

        def __init__(self, name: str) -> None:
            self.name = name
            self.pid = Process._next_pid
            Process._next_pid += 1
            self.stopped = False

        def poll(self) -> int | None:
            return 0 if self.stopped else None

        def terminate(self) -> None:
            events.append(f"terminate:{self.name}")
            self.stopped = True

        def kill(self) -> None:
            events.append(f"kill:{self.name}")
            self.stopped = True

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            events.append(f"wait:{self.name}")
            return 0

    def start(name: str, command: list[str]) -> None:
        del command
        events.append(f"start:{name}")
        supervisor.processes[name] = Process(name)  # type: ignore[assignment]
        if name == "window_manager":
            raise RuntimeError("window manager failed")

    supervisor._start_process = start  # type: ignore[method-assign]
    supervisor._wait_for_x_server_ready = lambda: asyncio.sleep(0)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="window manager failed"):
        await supervisor.start()

    assert supervisor.running is False
    assert events == [
        "start:xvfb",
        "start:window_manager",
        "terminate:window_manager",
        "wait:window_manager",
        "terminate:xvfb",
        "wait:xvfb",
    ]


@pytest.mark.asyncio
async def test_lifespan_rolls_back_journal_supervisor_and_backend_on_startup_failure(
    tmp_path,
) -> None:
    app = create_app(_settings(tmp_path))
    events: list[str] = []

    async def journal_start() -> None:
        events.append("journal-start")

    async def journal_close() -> None:
        events.append("journal-close")

    async def supervisor_start() -> None:
        events.append("supervisor-start")
        raise RuntimeError("supervisor failed")

    async def supervisor_stop() -> None:
        events.append("supervisor-stop")

    def backend_close() -> None:
        events.append("backend-close")

    app.state.receipt_journal.start = journal_start
    app.state.receipt_journal.close = journal_close
    app.state.supervisor.start = supervisor_start
    app.state.supervisor.stop = supervisor_stop
    app.state.backend.close = backend_close

    with pytest.raises(RuntimeError, match="supervisor failed"):
        async with app.router.lifespan_context(app):
            raise AssertionError("lifespan should not yield")

    assert events == [
        "journal-start",
        "supervisor-start",
        "backend-close",
        "supervisor-stop",
        "journal-close",
    ]


def test_borrowed_surfaces_preserve_apps_and_commands_contract() -> None:
    sync = BorrowedComputer(
        DaemonClient("http://daemon.invalid"),
        SessionLeaseCoordinator(DaemonClient("http://daemon.invalid"), run_id="run"),
        base_url="http://daemon.invalid",
        token="minted",
        http2=False,
    )
    async_ = AsyncBorrowedComputer(
        AsyncDaemonClient("http://daemon.invalid"),
        AsyncSessionLeaseCoordinator(
            AsyncDaemonClient("http://daemon.invalid"),
            run_id="run",
            heartbeat_transport=AsyncDaemonClient("http://daemon.invalid"),
        ),
        base_url="http://daemon.invalid",
        token="minted",
        http2=False,
    )

    assert hasattr(sync, "apps")
    assert hasattr(sync, "commands")
    assert hasattr(async_, "apps")
    assert hasattr(async_, "commands")


def test_desktop_process_command_drops_from_root_controller(monkeypatch) -> None:
    env = desktop_process_environment(display=":99", environ={
        "COMPUTER_USE_DESKTOP_USER": "computer-desktop",
        "COMPUTER_USE_TUNNEL_TOKEN": "secret",
    })
    assert "COMPUTER_USE_TUNNEL_TOKEN" not in env
    monkeypatch.setattr("modal_computer_use.daemon.process_environment.os.name", "posix")
    monkeypatch.setattr(
        "modal_computer_use.daemon.process_environment._desktop_user_identity",
        lambda _: (1234, 2345),
    )
    monkeypatch.setattr("modal_computer_use.daemon.process_environment.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "modal_computer_use.daemon.process_environment.shutil.which",
        lambda name: "/usr/bin/setpriv" if name == "setpriv" else None,
    )

    command = desktop_process_command("sh", "-c", "id", environ=env)

    assert command[0] == "/usr/bin/setpriv"
    assert command[1:5] == ("--reuid=1234", "--regid=2345", "--init-groups", "--")
    assert command[-3:] == ("sh", "-c", "id")


def test_desktop_process_command_fails_closed_without_root_or_target_uid(monkeypatch) -> None:
    monkeypatch.setattr("modal_computer_use.daemon.process_environment.os.name", "posix")
    monkeypatch.setattr(
        "modal_computer_use.daemon.process_environment._desktop_user_identity",
        lambda _: (1234, 2345),
    )
    monkeypatch.setattr("modal_computer_use.daemon.process_environment.os.geteuid", lambda: 1900)

    with pytest.raises(RuntimeError, match="requires the managed root controller"):
        desktop_process_command(
            "true",
            environ={"COMPUTER_USE_DESKTOP_USER": "computer-desktop"},
        )


def test_desktop_output_file_keeps_root_owner_and_grants_desktop_group(monkeypatch) -> None:
    calls: list[tuple[str, int, int, int | None]] = []
    monkeypatch.setattr("modal_computer_use.daemon.process_environment.os.name", "posix")
    monkeypatch.setattr(
        "modal_computer_use.daemon.process_environment._desktop_user_identity",
        lambda _: (1234, 2345),
    )
    monkeypatch.setattr("modal_computer_use.daemon.process_environment.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "modal_computer_use.daemon.process_environment.os.fchown",
        lambda fd, uid, gid: calls.append(("chown", fd, uid, gid)),
    )
    monkeypatch.setattr(
        "modal_computer_use.daemon.process_environment.os.fchmod",
        lambda fd, mode: calls.append(("chmod", fd, mode, None)),
    )

    prepare_desktop_output_file(
        17,
        environ={"COMPUTER_USE_DESKTOP_USER": "computer-desktop"},
    )

    assert calls == [("chown", 17, 0, 2345), ("chmod", 17, 0o660, None)]


def test_daemon_process_command_uses_root_controller_without_secret_argv(monkeypatch) -> None:
    monkeypatch.setattr("modal_computer_use.daemon.process_environment.os.name", "nt")
    monkeypatch.setattr(
        "modal_computer_use.daemon.process_environment._desktop_user_identity",
        lambda _: (_ for _ in ()).throw(AssertionError("remote account must not be resolved")),
    )
    monkeypatch.setattr("modal_computer_use.daemon.process_environment.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "modal_computer_use.daemon.process_environment.shutil.which",
        lambda name: "/usr/bin/setpriv" if name == "setpriv" else None,
    )

    command = daemon_process_command(
        "python",
        "-m",
        "modal_computer_use.daemon",
        environ={
            "COMPUTER_USE_DAEMON_CONTROLLER": "root",
            "COMPUTER_USE_TUNNEL_TOKEN": "bootstrap-secret",
        },
        managed_image=True,
    )

    assert command[:2] == ("sh", "-c")
    assert "COMPUTER_USE_DAEMON_CONTROLLER" in command[2]
    assert "id -u" in command[2]
    assert "setpriv" not in command[2]
    assert "credential boundary is unavailable" in command[2]
    assert "exit 78" in command[2]
    assert "root controller is unavailable" in command[2]
    assert "exit 77" in command[2]
    assert command[3] == "modal-computer-use-daemon"
    assert "bootstrap-secret" not in command
    assert daemon_process_command("python", "-m", "daemon") == ("python", "-m", "daemon")


def test_configured_missing_desktop_user_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr("modal_computer_use.daemon.process_environment.os.name", "posix")
    monkeypatch.setattr(
        "modal_computer_use.daemon.process_environment._desktop_user_identity", lambda _: None
    )

    with pytest.raises(RuntimeError, match="configured desktop user does not exist"):
        desktop_process_command(
            "true",
            environ={"COMPUTER_USE_DESKTOP_USER": "missing-desktop"},
        )


def test_credential_boundary_recipe_limits_shared_paths() -> None:
    commands = "\n".join(_credential_boundary_commands())

    assert "uid 1901" in commands
    assert "computer-use" in commands
    assert "/home/desktop/artifacts" in commands
    assert "/home/desktop/recordings" in commands
    assert "/home/desktop/artifacts/traces" not in commands
    assert "/var/lib/computer-daemon/runtime" in commands
    assert "/var/lib/computer-daemon/vnc" in commands
    assert "3770" in commands
    assert "COMPUTER_USE_*" not in commands
    assert "sudoers" not in commands
    assert "-o root" in commands


def test_owner_proof_transport_header_is_scoped_to_owner_mutations() -> None:
    sync = HTTPTransport("http://daemon.invalid", owner_proof="owner-proof")
    async_ = AsyncHTTPTransport("http://daemon.invalid", owner_proof="owner-proof")
    try:
        assert sync._request_headers(None, path="/v1/computer/restart")[
            "X-Computer-Use-Owner-Proof"
        ] == "owner-proof"
        assert "X-Computer-Use-Owner-Proof" not in sync._request_headers(
            None, path="/v1/artifacts/manifest"
        )
        assert async_._request_headers(None, path="/v1/processes/xvfb/restart")[
            "X-Computer-Use-Owner-Proof"
        ] == "owner-proof"
        assert "X-Computer-Use-Owner-Proof" not in async_._request_headers(
            None, path="/v1/computer/status"
        )
    finally:
        sync.close()
        awaitable = async_.aclose()
        asyncio.run(awaitable)
