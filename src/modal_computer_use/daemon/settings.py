from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath


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
    return _validated_path(name, os.getenv(name, default))


def _validated_path(name: str, value: str | os.PathLike[str]) -> Path:
    """Validate one daemon storage/runtime path before it reaches the filesystem."""
    try:
        value_string = os.fspath(value)
    except TypeError as exc:
        raise ValueError(f"{name} must be an absolute normalized POSIX path") from exc
    if not isinstance(value_string, str):
        raise ValueError(f"{name} must be an absolute normalized POSIX path")
    if not value_string or value_string != value_string.strip():
        raise ValueError(f"{name} must be an absolute normalized POSIX path")
    if "\x00" in value_string or "\\" in value_string:
        raise ValueError(f"{name} must be an absolute normalized POSIX path")
    if not value_string.startswith("/") or value_string.startswith("//"):
        raise ValueError(f"{name} must be an absolute normalized POSIX path")
    path = PurePosixPath(value_string)
    if any(part in {".", ".."} for part in path.parts):
        raise ValueError(f"{name} must not contain dot components")
    if path.as_posix() != value_string:
        raise ValueError(f"{name} must be an absolute normalized POSIX path")
    return Path(value_string)


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
    max_tunnel_sessions: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_MAX_TUNNEL_SESSIONS", 0)
    )
    require_connect_user: bool = field(
        default_factory=lambda: _bool_env("COMPUTER_USE_REQUIRE_CONNECT_USER", True)
    )
    allow_unauthenticated_loopback: bool = field(
        default_factory=lambda: _bool_env("COMPUTER_USE_ALLOW_UNAUTHENTICATED_LOOPBACK", False)
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
    # MSS remains the production default; ``auto`` is an opt-in evaluation mode
    # for the native X11 shared-memory source.
    screenshot_capture_source: str = field(
        default_factory=lambda: os.getenv("COMPUTER_USE_SCREENSHOT_CAPTURE_SOURCE", "mss")
    )
    max_batch_actions: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_MAX_BATCH_ACTIONS", 50)
    )
    max_batch_duration_ms: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_MAX_BATCH_DURATION_MS", 30_000)
    )
    max_action_depth: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_MAX_ACTION_DEPTH", 32)
    )
    max_json_body_bytes: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_MAX_JSON_BODY_BYTES", 16_777_216)
    )
    max_websocket_message_bytes: int = field(
        default_factory=lambda: _int_env(
            "COMPUTER_USE_MAX_WEBSOCKET_MESSAGE_BYTES", 16_777_216
        )
    )
    max_hot_session_connections: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_MAX_HOT_SESSION_CONNECTIONS", 64)
    )
    max_observation_connections: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_MAX_OBSERVATION_CONNECTIONS", 16)
    )
    max_command_arguments: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_MAX_COMMAND_ARGUMENTS", 65_536)
    )
    max_drag_points: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_MAX_DRAG_POINTS", 1_024)
    )
    max_key_collection_size: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_MAX_KEY_COLLECTION_SIZE", 64)
    )
    input_rate_limit_per_sec: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_INPUT_RATE_LIMIT_PER_SEC", 100)
    )
    input_rate_limit_burst: int = field(
        default_factory=lambda: _int_env("COMPUTER_USE_INPUT_RATE_LIMIT_BURST", 400)
    )
    input_backend: str = field(
        default_factory=lambda: os.getenv("COMPUTER_USE_INPUT_BACKEND", "auto")
    )
    subprocess_backend: str = field(
        default_factory=lambda: os.getenv(
            "COMPUTER_USE_SUBPROCESS_BACKEND",
            "isolated-asyncio",
        )
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
        for name, value in (
            ("COMPUTER_USE_ARTIFACTS_DIR", self.artifacts_dir),
            ("COMPUTER_USE_RECORDINGS_DIR", self.recordings_dir),
            ("COMPUTER_USE_TRACE_DIR", self.trace_dir),
            ("COMPUTER_USE_RUNTIME_DIR", self.runtime_dir),
        ):
            object.__setattr__(self, _path_field_name(name), _validated_path(name, value))
        _require_choice(
            "COMPUTER_USE_BACKEND",
            self.backend,
            {"auto", "mock", "x11"},
        )
        _require_choice(
            "COMPUTER_USE_SCREENSHOT_CAPTURE_SOURCE",
            self.screenshot_capture_source,
            {"auto", "mss", "x11-shm"},
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
        _require_range("COMPUTER_USE_TUNNEL_TOKEN_TTL_SECONDS", self.tunnel_token_ttl_seconds, 1)
        _require_range("COMPUTER_USE_MAX_TUNNEL_SESSIONS", self.max_tunnel_sessions, 0)
        _require_range("COMPUTER_USE_MAX_BATCH_ACTIONS", self.max_batch_actions, 1, 500)
        _require_range("COMPUTER_USE_MAX_ACTION_DEPTH", self.max_action_depth, 1, 128)
        _require_range("COMPUTER_USE_MAX_JSON_BODY_BYTES", self.max_json_body_bytes, 0)
        _require_range(
            "COMPUTER_USE_MAX_WEBSOCKET_MESSAGE_BYTES",
            self.max_websocket_message_bytes,
            0,
        )
        _require_range(
            "COMPUTER_USE_MAX_HOT_SESSION_CONNECTIONS",
            self.max_hot_session_connections,
            0,
        )
        _require_range(
            "COMPUTER_USE_MAX_OBSERVATION_CONNECTIONS",
            self.max_observation_connections,
            0,
        )
        _require_range("COMPUTER_USE_MAX_COMMAND_ARGUMENTS", self.max_command_arguments, 0)
        _require_range("COMPUTER_USE_MAX_DRAG_POINTS", self.max_drag_points, 0)
        _require_range("COMPUTER_USE_MAX_KEY_COLLECTION_SIZE", self.max_key_collection_size, 0)
        _require_range(
            "COMPUTER_USE_INPUT_RATE_LIMIT_PER_SEC",
            self.input_rate_limit_per_sec,
            0,
            10_000,
        )
        _require_range(
            "COMPUTER_USE_INPUT_RATE_LIMIT_BURST",
            self.input_rate_limit_burst,
            1,
            100_000,
        )


def get_settings() -> DaemonSettings:
    return DaemonSettings()


def _require_choice(name: str, value: str, allowed: set[str]) -> None:
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{name} must be one of: {choices}")


def _require_range(name: str, value: int, minimum: int, maximum: int | None = None) -> None:
    if value < minimum or (maximum is not None and value > maximum):
        if maximum is None:
            raise ValueError(f"{name} must be at least {minimum}")
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


def _path_field_name(environment_name: str) -> str:
    return {
        "COMPUTER_USE_ARTIFACTS_DIR": "artifacts_dir",
        "COMPUTER_USE_RECORDINGS_DIR": "recordings_dir",
        "COMPUTER_USE_TRACE_DIR": "trace_dir",
        "COMPUTER_USE_RUNTIME_DIR": "runtime_dir",
    }[environment_name]
