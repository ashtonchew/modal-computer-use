from __future__ import annotations

import json
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


def _json_list_env(name: str) -> list[str]:
    value = os.getenv(name)
    if not value:
        return []
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError(f"{name} must be a JSON string list")
    return parsed


@dataclass(frozen=True)
class DaemonSettings:
    run_id: str | None = field(default_factory=lambda: os.getenv("COMPUTER_USE_RUN_ID") or None)
    display: str = field(default_factory=lambda: os.getenv("DISPLAY", ":99"))
    desktop_width: int = field(default_factory=lambda: _int_env("COMPUTER_USE_DESKTOP_WIDTH", 1024))
    desktop_height: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_DESKTOP_HEIGHT", 768)
    )
    desktop_dpi: int = field(default_factory=lambda: _int_env("COMPUTER_USE_DESKTOP_DPI", 96))
    display_depth: int = field(default_factory=lambda: _int_env("COMPUTER_USE_DISPLAY_DEPTH", 24))
    window_manager: str = field(
        default_factory=lambda: os.getenv("COMPUTER_USE_WINDOW_MANAGER", "xfce")
    )
    browser: str | None = field(default_factory=lambda: os.getenv("COMPUTER_USE_BROWSER") or None)
    browser_prewarm: bool = field(
        default_factory=lambda: _bool_env("COMPUTER_USE_BROWSER_PREWARM", False)
    )
    browser_profile_dir: str | None = field(
        default_factory=lambda: os.getenv("COMPUTER_USE_BROWSER_PROFILE_DIR") or None
    )
    browser_launch_args: list[str] = field(
        default_factory=lambda: _json_list_env("COMPUTER_USE_BROWSER_LAUNCH_ARGS")
    )
    browser_open_url_on_start: str | None = field(
        default_factory=lambda: os.getenv("COMPUTER_USE_BROWSER_OPEN_URL_ON_START") or None
    )
    browser_gpu_mode: str = field(
        default_factory=lambda: os.getenv("COMPUTER_USE_BROWSER_GPU_MODE", "auto")
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
        default_factory=lambda: _path_env("COMPUTER_USE_RECORDINGS_DIR", "/home/desktop/recordings")
    )
    runtime_dir: Path = field(
        default_factory=lambda: _path_env(
            "COMPUTER_USE_RUNTIME_DIR",
            "/tmp/modal-computer-use",  # noqa: S108
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
        default_factory=lambda: _int_env("COMPUTER_USE_POST_ACTION_DELAY_MS", 0)
    )
    readiness_cache_ttl_ms: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_READINESS_CACHE_TTL_MS", 1_000)
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
    tunnel_token: str | None = field(
        default_factory=lambda: os.getenv("COMPUTER_USE_TUNNEL_TOKEN") or None
    )
    tunnel_token_ttl_seconds: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_TUNNEL_TOKEN_TTL_SECONDS", 3_600)
    )
    require_connect_user: bool = field(
        default_factory=lambda: _bool_env("COMPUTER_USE_REQUIRE_CONNECT_USER", True)
    )
    trust_private_connect_proxy: bool = field(
        default_factory=lambda: _bool_env("COMPUTER_USE_TRUST_PRIVATE_CONNECT_PROXY", False)
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
    input_backend: str = field(
        default_factory=lambda: os.getenv("COMPUTER_USE_INPUT_BACKEND", "auto")
    )
    subprocess_backend: str = field(
        default_factory=lambda: os.getenv("COMPUTER_USE_SUBPROCESS_BACKEND", "asyncio")
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

    def __post_init__(self) -> None:
        _require_choice(
            "COMPUTER_USE_BACKEND",
            self.backend,
            {"auto", "mock", "x11"},
        )
        _require_choice(
            "COMPUTER_USE_INPUT_BACKEND",
            self.input_backend,
            {"auto", "xdotool", "xtest"},
        )
        _require_choice(
            "COMPUTER_USE_SUBPROCESS_BACKEND",
            self.subprocess_backend,
            {"asyncio", "isolated-asyncio", "threaded"},
        )


def get_settings() -> DaemonSettings:
    return DaemonSettings()


def _require_choice(name: str, value: str, allowed: set[str]) -> None:
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{name} must be one of: {choices}")
