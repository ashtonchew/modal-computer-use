from __future__ import annotations

from pathlib import Path

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


def test_daemon_settings_explicit_overrides_win(monkeypatch) -> None:
    monkeypatch.setenv("COMPUTER_USE_DESKTOP_WIDTH", "123")

    settings = DaemonSettings(desktop_width=456)

    assert settings.desktop_width == 456
