from __future__ import annotations

import ipaddress
import re
import warnings
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

VncMode = Literal["off", "view_only", "control"]
ModalIngress = Literal["attested-tunnel", "connect", "tunnel"]


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DesktopConfig(StrictBaseModel):
    resolution: tuple[int, int] = (1024, 768)
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

    @field_validator("modal_region")
    @classmethod
    def _valid_modal_region(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("modal_region must be non-empty when set")
        return value


class ResourceConfig(StrictBaseModel):
    profile: Literal["standard", "browser", "browser-gpu", "custom"] = "standard"
    cpu: float | None = Field(default=None, gt=0)
    memory_mib: int | None = Field(
        default=None,
        validation_alias=AliasChoices("memory_mib", "memory_mb"),
        ge=128,
    )
    gpu: str | None = None


class ImageConfig(StrictBaseModel):
    source: Literal["inline", "named"] = "inline"
    revision: str | None = None
    environment_name: str | None = None

    @model_validator(mode="after")
    def _valid_named_image(self) -> ImageConfig:
        if self.source == "named":
            if self.revision is None or re.fullmatch(r"[0-9a-f]{40}", self.revision) is None:
                raise ValueError("named images require a full 40-character Git revision")
        elif self.revision is not None or self.environment_name is not None:
            raise ValueError(
                "image.revision and image.environment_name are only valid when "
                "image.source is named"
            )
        if self.environment_name is not None and not self.environment_name.strip():
            raise ValueError("image.environment_name must be non-empty when set")
        return self


class NetworkConfig(StrictBaseModel):
    block_all: bool = Field(default=False, validation_alias=AliasChoices("block_all", "blocked"))
    daemon_http_version: Literal["1.1", "2"] = "1.1"
    outbound_cidr_allowlist: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "outbound_cidr_allowlist",
            "cidr_allowlist",
            "allowlist",
        ),
    )
    outbound_domain_allowlist: list[str] | None = None
    inbound_cidr_allowlist: list[str] | None = None

    @field_validator("outbound_cidr_allowlist", "inbound_cidr_allowlist")
    @classmethod
    def _valid_cidrs(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if any(not item.strip() for item in value):
            raise ValueError("CIDR allowlist entries must be non-empty")
        for item in value:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError as exc:
                raise ValueError("CIDR allowlist entries must be valid CIDR ranges") from exc
        return value

    @field_validator("outbound_domain_allowlist")
    @classmethod
    def _valid_domains(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(not item.strip() for item in value):
            raise ValueError("outbound_domain_allowlist entries must be non-empty")
        return value

    @model_validator(mode="after")
    def _valid_network_policy(self) -> NetworkConfig:
        if self.block_all and any(
            allowlist is not None
            for allowlist in (
                self.outbound_cidr_allowlist,
                self.outbound_domain_allowlist,
                self.inbound_cidr_allowlist,
            )
        ):
            raise ValueError("block_all cannot be combined with network allowlists")
        return self

    @property
    def cidr_allowlist(self) -> list[str] | None:
        """Compatibility alias for the deprecated outbound CIDR field name."""
        return self.outbound_cidr_allowlist


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
    post_action_delay_ms: int = Field(default=0, ge=0, le=10_000)
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
    input_backend: Literal["auto", "xtest", "xdotool"] = "auto"
    subprocess_backend: Literal["asyncio", "threaded", "isolated-asyncio"] = (
        "isolated-asyncio"
    )

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
    image: ImageConfig = Field(default_factory=ImageConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    browser: BrowserConfig | None = None
    actions: ActionConfig = Field(default_factory=ActionConfig)
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    run_id: str | None = None
    request_id: str | None = Field(default=None, exclude=True)
    ingress: ModalIngress = "attested-tunnel"
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
        if self.network.block_all and (
            self.ingress != "connect" or normalize_vnc_mode(self.expose_vnc) != "off"
        ):
            raise ValueError("network.block_all requires connect ingress with noVNC disabled")
        if self.image.source == "named":
            if self.resources.profile == "custom":
                raise ValueError("named images do not support the custom resource profile")
            if self.desktop.window_manager != "xfce":
                raise ValueError("named images require the xfce window manager")
            if self.resources.profile in ("browser", "browser-gpu") and (
                self.browser is None or self.browser.kind is None
            ):
                raise ValueError("named image selection requires browser.kind")
            if (
                self.resources.profile in ("browser", "browser-gpu")
                and self.browser is not None
                and not self.browser.prewarm
            ):
                raise ValueError("named browser images require browser.prewarm")
        return self


def normalize_vnc_mode(value: VncMode | bool) -> VncMode:
    if value is True:
        return "control"
    if value is False:
        return "off"
    if value not in ("off", "view_only", "control"):
        raise ValueError("expose_vnc must be off, view_only, control, True, or False")
    return value
