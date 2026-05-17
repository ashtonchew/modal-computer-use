from __future__ import annotations

from pathlib import Path

from modal_computer_use.daemon import __main__ as daemon_main
from modal_computer_use.daemon.settings import DaemonSettings, get_settings


def test_daemon_settings_read_environment_when_instantiated(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("COMPUTER_USE_LOCAL_TOKEN", "late-token")
    monkeypatch.setenv("COMPUTER_USE_DESKTOP_WIDTH", "123")
    monkeypatch.setenv("COMPUTER_USE_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("COMPUTER_USE_MAX_ACTIONS", "7")
    monkeypatch.setenv("COMPUTER_USE_BROWSER_PREWARM", "true")

    settings = get_settings()

    assert settings.local_token == "late-token"  # noqa: S105 - test fixture value
    assert settings.desktop_width == 123
    assert settings.artifacts_dir == Path(tmp_path / "artifacts")
    assert settings.max_actions == 7
    assert settings.browser_prewarm is True


def test_daemon_settings_use_sdk_primitive_defaults(monkeypatch) -> None:
    for key in (
        "COMPUTER_USE_DESKTOP_WIDTH",
        "COMPUTER_USE_DESKTOP_HEIGHT",
        "COMPUTER_USE_DESKTOP_DPI",
        "COMPUTER_USE_POST_ACTION_DELAY_MS",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = DaemonSettings()

    assert settings.desktop_width == 1024
    assert settings.desktop_height == 768
    assert settings.desktop_dpi == 96
    assert settings.post_action_delay_ms == 0


def test_daemon_settings_explicit_overrides_win(monkeypatch) -> None:
    monkeypatch.setenv("COMPUTER_USE_DESKTOP_WIDTH", "123")

    settings = DaemonSettings(desktop_width=456)

    assert settings.desktop_width == 456


def test_daemon_entrypoint_reads_host_and_port_environment(monkeypatch) -> None:
    calls = []
    app = object()
    monkeypatch.setenv("COMPUTER_USE_DAEMON_HOST", "127.0.0.2")
    monkeypatch.setenv("COMPUTER_USE_DAEMON_PORT", "9090")
    monkeypatch.setattr(daemon_main, "create_app", lambda: app)
    monkeypatch.setattr(
        daemon_main.uvicorn,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    daemon_main.main()

    assert calls == [((app,), {"host": "127.0.0.2", "port": 9090, "log_config": None})]


def test_daemon_entrypoint_defaults_port_to_8080(monkeypatch) -> None:
    calls = []
    app = object()
    monkeypatch.delenv("COMPUTER_USE_DAEMON_PORT", raising=False)
    monkeypatch.setenv("COMPUTER_USE_DAEMON_HOST", "127.0.0.2")
    monkeypatch.setattr(daemon_main, "create_app", lambda: app)
    monkeypatch.setattr(
        daemon_main.uvicorn,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    daemon_main.main()

    assert calls[0][1]["port"] == 8080
