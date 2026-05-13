from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    return int(value)


def _optional_int_env(name: str) -> int | None:
    value = os.getenv(name)
    return int(value) if value else None


def _path_env(name: str, default: str) -> Path:
    return Path(os.getenv(name, default))


@dataclass(frozen=True)
class DaemonSettings:
    run_id: str | None = field(default_factory=lambda: os.getenv("COMPUTER_USE_RUN_ID") or None)
    display: str = field(default_factory=lambda: os.getenv("DISPLAY", ":99"))
    desktop_width: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_DESKTOP_WIDTH", 1440)
    )
    desktop_height: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_DESKTOP_HEIGHT", 900)
    )
    desktop_dpi: int = field(default_factory=lambda: _int_env("COMPUTER_USE_DESKTOP_DPI", 96))
    display_depth: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_DISPLAY_DEPTH", 24)
    )
    window_manager: str = field(
        default_factory=lambda: os.getenv("COMPUTER_USE_WINDOW_MANAGER", "xfce")
    )
    browser: str | None = field(
        default_factory=lambda: os.getenv("COMPUTER_USE_BROWSER") or None
    )
    browser_prewarm: bool = field(
        default_factory=lambda: _bool_env("COMPUTER_USE_BROWSER_PREWARM", False)
    )
    artifacts_dir: Path = field(
        default_factory=lambda: _path_env("COMPUTER_USE_ARTIFACTS_DIR", "/home/desktop/artifacts")
    )
    artifacts_persistent: bool = field(
        default_factory=lambda: _bool_env("COMPUTER_USE_ARTIFACTS_PERSISTENT", False)
    )
    artifacts_volume_mounted: bool = field(
        default_factory=lambda: _bool_env("COMPUTER_USE_ARTIFACTS_VOLUME_MOUNTED", False)
    )
    recordings_dir: Path = field(
        default_factory=lambda: _path_env(
            "COMPUTER_USE_RECORDINGS_DIR", "/home/desktop/recordings"
        )
    )
    runtime_dir: Path = field(
        default_factory=lambda: _path_env(
            "COMPUTER_USE_RUNTIME_DIR", "/tmp/modal-computer-use"  # noqa: S108
        )
    )
    trace_dir: Path = field(
        default_factory=lambda: _path_env(
            "COMPUTER_USE_TRACE_DIR", "/home/desktop/artifacts/traces"
        )
    )
    trace_actions: bool = field(
        default_factory=lambda: _bool_env("COMPUTER_USE_TRACE_ACTIONS", False)
    )
    screenshot_max_pixels: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_SCREENSHOT_MAX_PIXELS", 8_294_400)
    )
    screenshot_processing_location: str = field(
        default_factory=lambda: os.getenv("COMPUTER_USE_SCREENSHOT_PROCESSING_LOCATION", "auto")
    )
    post_action_delay_ms: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_POST_ACTION_DELAY_MS", 100)
    )
    default_action_timeout_ms: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_DEFAULT_ACTION_TIMEOUT_MS", 5_000)
    )
    max_action_timeout_ms: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_MAX_ACTION_TIMEOUT_MS", 300_000)
    )
    idempotency_cache_max_entries: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_IDEMPOTENCY_CACHE_MAX_ENTRIES", 1_000)
    )
    idempotency_cache_ttl_seconds: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_IDEMPOTENCY_CACHE_TTL_SECONDS", 3_600)
    )
    local_token: str | None = field(
        default_factory=lambda: os.getenv("COMPUTER_USE_LOCAL_TOKEN") or None
    )
    require_connect_user: bool = field(
        default_factory=lambda: _bool_env("COMPUTER_USE_REQUIRE_CONNECT_USER", True)
    )
    reject_query_tokens: bool = field(
        default_factory=lambda: _bool_env("COMPUTER_USE_REJECT_QUERY_TOKENS", True)
    )
    vnc_mode: str = field(default_factory=lambda: os.getenv("COMPUTER_USE_VNC_MODE", "off"))
    vnc_password: str | None = field(
        default_factory=lambda: os.getenv("COMPUTER_USE_VNC_PASSWORD") or None
    )
    backend: str = field(default_factory=lambda: os.getenv("COMPUTER_USE_BACKEND", "auto"))
    max_batch_actions: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_MAX_BATCH_ACTIONS", 50)
    )
    max_batch_duration_ms: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_MAX_BATCH_DURATION_MS", 30_000)
    )
    input_rate_limit_per_sec: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_INPUT_RATE_LIMIT_PER_SEC", 20)
    )
    otel_enabled: bool = field(
        default_factory=lambda: _bool_env("COMPUTER_USE_OTEL_ENABLED", False)
    )
    max_actions: int | None = field(
        default_factory=lambda: _optional_int_env("COMPUTER_USE_MAX_ACTIONS")
    )
    max_screenshots: int | None = field(
        default_factory=lambda: _optional_int_env("COMPUTER_USE_MAX_SCREENSHOTS")
    )
    max_artifact_bytes: int | None = field(
        default_factory=lambda: _optional_int_env("COMPUTER_USE_MAX_ARTIFACT_BYTES")
    )
    max_recording_seconds: int | None = field(
        default_factory=lambda: _optional_int_env("COMPUTER_USE_MAX_RECORDING_SECONDS")
    )
    max_idle_seconds: int | None = field(
        default_factory=lambda: _optional_int_env("COMPUTER_USE_MAX_IDLE_SECONDS")
    )
    image_profile: str = field(
        default_factory=lambda: os.getenv("COMPUTER_USE_IMAGE_PROFILE", "standard")
    )


def get_settings() -> DaemonSettings:
    return DaemonSettings()
