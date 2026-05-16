from __future__ import annotations

import warnings
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

VncMode = Literal["off", "view_only", "control"]


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DesktopConfig(StrictBaseModel):
    resolution: tuple[int, int] = (1440, 900)
    dpi: int = Field(default=96, ge=48, le=240)
    window_manager: Literal["xfce", "openbox"] = "xfce"
    display_depth: int = Field(default=24, ge=8, le=32)

    @field_validator("resolution")
    @classmethod
    def _valid_resolution(cls, value: tuple[int, int]) -> tuple[int, int]:
        width, height = value
        if width < 320 or height < 240:
            raise ValueError("resolution must be at least 320x240")
        if width * height > 8_294_400:
            raise ValueError("resolution must not exceed 8.3 megapixels")
        return value


class RuntimeConfig(StrictBaseModel):
    timeout_seconds: int = Field(default=3600, ge=1, le=86_400)
    idle_timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)
    readiness_timeout_seconds: int = Field(default=120, ge=1, le=900)
    modal_region: str | None = None


class ResourceConfig(StrictBaseModel):
    profile: Literal["standard", "browser", "browser-gpu", "custom"] = "standard"
    cpu: float | None = Field(default=None, gt=0)
    memory_mib: int | None = Field(
        default=None,
        validation_alias=AliasChoices("memory_mib", "memory_mb"),
        ge=128,
    )
    gpu: str | None = None


class NetworkConfig(StrictBaseModel):
    block_all: bool = Field(default=False, validation_alias=AliasChoices("block_all", "blocked"))
    cidr_allowlist: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("cidr_allowlist", "allowlist"),
    )

    @field_validator("cidr_allowlist")
    @classmethod
    def _no_empty_cidrs(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(not item.strip() for item in value):
            raise ValueError("cidr_allowlist entries must be non-empty")
        return value


class StorageConfig(StrictBaseModel):
    recordings_dir: str = Field(
        default="/home/desktop/recordings",
        validation_alias=AliasChoices("recordings_dir", "recording_dir"),
    )
    artifacts_dir: str = "/home/desktop/artifacts"
    persist_artifacts: bool = False
    trace_dir: str = "/home/desktop/artifacts/traces"


class BrowserConfig(StrictBaseModel):
    kind: Literal["firefox", "chromium"] | None = None
    prewarm: bool = True
    profile_dir: str | None = None
    launch_args: list[str] = Field(default_factory=list)
    open_url_on_start: str | None = None
    gpu_mode: Literal["auto", "off", "chromium-vulkan"] | None = None

    @field_validator("launch_args")
    @classmethod
    def _valid_launch_args(cls, value: list[str]) -> list[str]:
        for arg in value:
            if "\x00" in arg:
                raise ValueError("launch_args must not contain NUL bytes")
        return value


class ActionConfig(StrictBaseModel):
    post_action_delay_ms: int = Field(default=100, ge=0, le=10_000)
    screenshot_after: bool = False
    trace_actions: bool = Field(
        default=False,
        validation_alias=AliasChoices("trace_actions", "action_trace"),
    )
    screenshot_processing_location: Literal["daemon", "client", "auto"] = Field(
        default="auto",
        validation_alias=AliasChoices("screenshot_processing_location", "screenshot_processing"),
    )
    max_batch_actions: int = Field(default=50, ge=1, le=500)
    max_batch_duration_ms: int = Field(default=30_000, ge=1, le=600_000)
    default_action_timeout_ms: int = Field(default=5_000, ge=1, le=300_000)
    max_action_timeout_ms: int = Field(default=300_000, ge=1, le=600_000)
    input_rate_limit_per_sec: int = Field(default=20, ge=0, le=10_000)

    @model_validator(mode="after")
    def _valid_timeouts(self) -> ActionConfig:
        if self.default_action_timeout_ms > self.max_action_timeout_ms:
            raise ValueError("default_action_timeout_ms must not exceed max_action_timeout_ms")
        return self


class BudgetConfig(StrictBaseModel):
    max_actions: int | None = Field(default=None, ge=1)
    max_screenshots: int | None = Field(default=None, ge=1)
    max_artifact_bytes: int | None = Field(default=None, ge=1)
    max_recording_seconds: int | None = Field(default=None, ge=1)
    max_idle_seconds: int | None = Field(default=None, ge=1)


class ComputerConfig(StrictBaseModel):
    desktop: DesktopConfig = Field(default_factory=DesktopConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    resources: ResourceConfig = Field(default_factory=ResourceConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    browser: BrowserConfig | None = None
    actions: ActionConfig = Field(default_factory=ActionConfig)
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    run_id: str | None = None
    request_id: str | None = Field(default=None, exclude=True)
    expose_vnc: VncMode | bool = "off"
    vnc_password: str | None = None

    @field_validator("expose_vnc", mode="before")
    @classmethod
    def _normalize_vnc_field(cls, value: VncMode | bool) -> VncMode:
        return normalize_vnc_mode(value)

    @model_validator(mode="after")
    def _normalize_compat(self) -> ComputerConfig:
        if self.request_id and not self.run_id:
            warnings.warn(
                "request_id is deprecated; use run_id",
                DeprecationWarning,
                stacklevel=2,
            )
            object.__setattr__(self, "run_id", self.request_id)
        return self


def normalize_vnc_mode(value: VncMode | bool) -> VncMode:
    if value is True:
        return "control"
    if value is False:
        return "off"
    if value not in ("off", "view_only", "control"):
        raise ValueError("expose_vnc must be off, view_only, control, True, or False")
    return value
