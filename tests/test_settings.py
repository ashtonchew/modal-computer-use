from __future__ import annotations

from pathlib import Path

import pytest

from modal_computer_use.daemon import __main__ as daemon_main
from modal_computer_use.daemon import app as app_module
from modal_computer_use.daemon.desktop.x11 import MockDesktopBackend
from modal_computer_use.daemon.settings import DaemonSettings, get_settings


def test_daemon_settings_read_environment_when_instantiated(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("COMPUTER_USE_LOCAL_TOKEN", "late-token")
    monkeypatch.setenv("COMPUTER_USE_DESKTOP_WIDTH", "123")
    monkeypatch.setenv("COMPUTER_USE_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("COMPUTER_USE_MAX_ACTIONS", "7")
    monkeypatch.setenv("COMPUTER_USE_BROWSER_PREWARM", "true")
    monkeypatch.setenv("COMPUTER_USE_INPUT_BACKEND", "xtest")
    monkeypatch.setenv("COMPUTER_USE_SUBPROCESS_BACKEND", "threaded")

    settings = get_settings()

    assert settings.local_token == "late-token"  # noqa: S105 - test fixture value
    assert settings.desktop_width == 123
    assert settings.artifacts_dir == Path(tmp_path / "artifacts")
    assert settings.max_actions == 7
    assert settings.browser_prewarm is True
    assert settings.input_backend == "xtest"
    assert settings.subprocess_backend == "threaded"


def test_daemon_settings_use_sdk_primitive_defaults(monkeypatch) -> None:
    for key in (
        "COMPUTER_USE_DESKTOP_WIDTH",
        "COMPUTER_USE_DESKTOP_HEIGHT",
        "COMPUTER_USE_DESKTOP_DPI",
        "COMPUTER_USE_POST_ACTION_DELAY_MS",
        "COMPUTER_USE_READINESS_CACHE_TTL_MS",
        "COMPUTER_USE_SUBPROCESS_BACKEND",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = DaemonSettings()

    assert settings.desktop_width == 1024
    assert settings.desktop_height == 768
    assert settings.desktop_dpi == 96
    assert settings.post_action_delay_ms == 0
    assert settings.readiness_cache_ttl_ms == 1_000
    assert settings.subprocess_backend == "isolated-asyncio"


def test_daemon_settings_explicit_overrides_win(monkeypatch) -> None:
    monkeypatch.setenv("COMPUTER_USE_DESKTOP_WIDTH", "123")

    settings = DaemonSettings(desktop_width=456)

    assert settings.desktop_width == 456


@pytest.mark.parametrize(
    ("field", "value", "setting"),
    [
        ("backend", "x-11", "COMPUTER_USE_BACKEND"),
        ("input_backend", "x-test", "COMPUTER_USE_INPUT_BACKEND"),
        ("subprocess_backend", "threads", "COMPUTER_USE_SUBPROCESS_BACKEND"),
    ],
)
def test_daemon_settings_reject_invalid_backend_choices(
    field: str,
    value: str,
    setting: str,
) -> None:
    with pytest.raises(ValueError, match=rf"^{setting} must be one of: "):
        DaemonSettings(**{field: value})


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("COMPUTER_USE_BACKEND", "x-11"),
        ("COMPUTER_USE_INPUT_BACKEND", "x-test"),
        ("COMPUTER_USE_SUBPROCESS_BACKEND", "threads"),
    ],
)
def test_daemon_settings_reject_invalid_backend_environment(
    monkeypatch,
    setting: str,
    value: str,
) -> None:
    monkeypatch.setenv(setting, value)

    with pytest.raises(ValueError, match=rf"^{setting} must be one of: "):
        get_settings()


@pytest.mark.parametrize("backend", ["auto", "mock", "x11"])
@pytest.mark.parametrize("input_backend", ["auto", "xdotool", "xtest"])
@pytest.mark.parametrize(
    "subprocess_backend",
    ["asyncio", "threaded", "isolated-asyncio"],
)
def test_daemon_settings_accept_valid_backend_choices(
    backend: str,
    input_backend: str,
    subprocess_backend: str,
) -> None:
    settings = DaemonSettings(
        backend=backend,
        input_backend=input_backend,
        subprocess_backend=subprocess_backend,
    )

    assert settings.backend == backend
    assert settings.input_backend == input_backend
    assert settings.subprocess_backend == subprocess_backend


@pytest.mark.parametrize(
    "subprocess_backend",
    ["asyncio", "threaded", "isolated-asyncio"],
)
def test_create_app_wires_subprocess_backend_to_desktop_backend(
    monkeypatch,
    tmp_path,
    subprocess_backend: str,
) -> None:
    captured: dict[str, object] = {}

    def choose_backend(kind: str, **kwargs):
        captured.update({"kind": kind, **kwargs})
        return MockDesktopBackend(width=100, height=100)

    monkeypatch.setattr(app_module, "choose_backend", choose_backend)

    app_module.create_app(
        DaemonSettings(
            backend="x11",
            subprocess_backend=subprocess_backend,
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
        )
    )

    assert captured["kind"] == "x11"
    assert captured["subprocess_backend"] == subprocess_backend


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


def test_daemon_entrypoint_uses_h2_runner_when_configured(monkeypatch) -> None:
    calls = []
    monkeypatch.setenv("COMPUTER_USE_DAEMON_HOST", "127.0.0.2")
    monkeypatch.setenv("COMPUTER_USE_DAEMON_PORT", "9090")
    monkeypatch.setenv("COMPUTER_USE_DAEMON_HTTP_VERSION", "2")
    monkeypatch.setattr(
        daemon_main,
        "_run_hypercorn_h2",
        lambda **kwargs: calls.append(kwargs),
    )

    daemon_main.main()

    assert calls == [{"host": "127.0.0.2", "port": 9090}]
