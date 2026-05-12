from __future__ import annotations

import base64
import builtins
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from .errors import ActionValidationError

Button = Literal["left", "middle", "right"]
ScrollDirection = Literal["up", "down", "left", "right"]
ImageFormat = Literal["png", "jpeg", "webp"]
ScreenshotStorage = Literal["inline", "artifact", "auto"]
ScreenshotProcessing = Literal["daemon", "client", "auto"]


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Point(StrictBaseModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)


class Region(StrictBaseModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


class CoordinateSpace(StrictBaseModel):
    desktop_width: int = Field(gt=0)
    desktop_height: int = Field(gt=0)
    image_width: int = Field(gt=0)
    image_height: int = Field(gt=0)
    scale_x: float = Field(default=1.0, gt=0)
    scale_y: float = Field(default=1.0, gt=0)
    source_region: Region | None = None

    @model_validator(mode="after")
    def _consistent_source(self) -> CoordinateSpace:
        if self.source_region is not None:
            if self.source_region.right > self.desktop_width:
                raise ValueError("source_region extends beyond desktop width")
            if self.source_region.bottom > self.desktop_height:
                raise ValueError("source_region extends beyond desktop height")
        return self

    @classmethod
    def from_dimensions(
        cls,
        *,
        desktop_width: int,
        desktop_height: int,
        image_width: int | None = None,
        image_height: int | None = None,
        source_region: Region | None = None,
    ) -> CoordinateSpace:
        src_width = source_region.width if source_region else desktop_width
        src_height = source_region.height if source_region else desktop_height
        img_width = image_width or src_width
        img_height = image_height or src_height
        return cls(
            desktop_width=desktop_width,
            desktop_height=desktop_height,
            image_width=img_width,
            image_height=img_height,
            scale_x=img_width / src_width,
            scale_y=img_height / src_height,
            source_region=source_region,
        )

    def to_desktop(self, point: Point) -> Point:
        origin_x = self.source_region.x if self.source_region else 0
        origin_y = self.source_region.y if self.source_region else 0
        return Point(
            x=round(point.x / self.scale_x + origin_x),
            y=round(point.y / self.scale_y + origin_y),
        )

    def to_image(self, point: Point) -> Point:
        origin_x = self.source_region.x if self.source_region else 0
        origin_y = self.source_region.y if self.source_region else 0
        return Point(
            x=round((point.x - origin_x) * self.scale_x),
            y=round((point.y - origin_y) * self.scale_y),
        )


class ScreenshotOptions(StrictBaseModel):
    format: ImageFormat = "png"
    quality: int = Field(default=90, ge=1, le=100)
    scale: float = Field(default=1.0, gt=0, le=8)
    show_cursor: bool = False
    encoding: Literal["base64"] = "base64"
    storage: ScreenshotStorage = "inline"
    processing: ScreenshotProcessing = "auto"


class Screenshot(StrictBaseModel):
    format: ImageFormat
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    size_bytes: int = Field(ge=0)
    bytes: builtins.bytes | None = None
    data_base64: str | None = None
    artifact_uri: str | None = None
    sha256: str | None = None
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    coordinate_space: CoordinateSpace
    cursor_visible: bool = False
    cursor_position: Point | None = None

    @model_validator(mode="after")
    def _has_payload(self) -> Screenshot:
        if self.bytes is None and self.data_base64 is None and self.artifact_uri is None:
            raise ValueError("screenshot must include bytes, data_base64, or artifact_uri")
        return self

    def as_bytes(self) -> bytes:
        if self.bytes is not None:
            return self.bytes
        if self.data_base64 is not None:
            return base64.b64decode(self.data_base64)
        raise ValueError("screenshot bytes are artifact-backed and not inline")

    def to_base64(self) -> str:
        if self.data_base64 is not None:
            return self.data_base64
        return base64.b64encode(self.as_bytes()).decode("ascii")

    def save(self, path: str | Path) -> Path:
        output = Path(path)
        output.write_bytes(self.as_bytes())
        return output

    def to_pil(self) -> Any:
        from io import BytesIO

        from PIL import Image

        return Image.open(BytesIO(self.as_bytes()))


class ActionResult(StrictBaseModel):
    ok: bool = True
    message: str | None = None
    elapsed_ms: float | None = Field(default=None, ge=0)
    output: dict[str, Any] = Field(default_factory=dict)


class LifecycleResult(ActionResult):
    status: str | None = None


class ProcessStatus(StrictBaseModel):
    name: str
    status: Literal["starting", "running", "stopped", "failed", "unknown"]
    pid: int | None = None
    started_at: datetime | None = None
    uptime_seconds: float | None = Field(default=None, ge=0)
    restart_count: int = Field(default=0, ge=0)
    exit_code: int | None = None
    last_error: str | None = None


class ComputerStatus(StrictBaseModel):
    status: Literal["starting", "running", "stopped", "degraded", "failed"]
    ready: bool
    display: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    processes: dict[str, ProcessStatus] = Field(default_factory=dict)
    resources: dict[str, Any] = Field(default_factory=dict)
    budgets: dict[str, Any] = Field(default_factory=dict)


class Recording(StrictBaseModel):
    id: str
    name: str | None = None
    status: Literal["recording", "stopped", "failed"]
    format: str
    fps: int = Field(gt=0, le=120)
    path: str
    artifact_uri: str | None = None
    size_bytes: int = Field(ge=0)
    sha256: str | None = None
    stderr_path: str | None = None
    stderr_tail: list[str] = Field(default_factory=list)
    error: str | None = None
    ffmpeg_args: list[str] = Field(default_factory=list)
    return_code: int | None = None
    stop_method: str | None = None
    started_at: datetime
    stopped_at: datetime | None = None
    duration_seconds: float | None = Field(default=None, ge=0)


class DisplayGeometry(StrictBaseModel):
    id: str
    x: int
    y: int
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    scale: float = Field(default=1.0, gt=0)


class DisplayInfo(StrictBaseModel):
    primary_display: DisplayGeometry
    total_displays: int = Field(ge=1)
    displays: list[DisplayGeometry]


class X11Window(StrictBaseModel):
    id: str
    title: str
    pid: int | None = None
    x: int
    y: int
    width: int = Field(ge=0)
    height: int = Field(ge=0)
    workspace: int | None = None
    is_active: bool = False


class ArtifactInfo(StrictBaseModel):
    path: str
    uri: str
    kind: Literal["file", "directory"]
    size_bytes: int | None = Field(default=None, ge=0)
    content_type: str | None = None
    sha256: str | None = None
    created_at: datetime | None = None
    modified_at: datetime | None = None
    created_by_call_id: str | None = None
    retention_class: Literal["ephemeral", "persistent", "debug", "trace"] = "ephemeral"


class ArtifactSyncResult(StrictBaseModel):
    ok: bool
    persistent: bool
    synced_paths: list[str] = Field(default_factory=list)
    message: str | None = None


class SandboxRef(StrictBaseModel):
    sandbox_id: str
    app_name: str
    name: str | None = None
    run_id: str | None = None
    config_hash: str | None = None
    status: Literal["created", "scheduled", "started", "ready", "finished", "unknown"]
    tags: dict[str, str] = Field(default_factory=dict)
    vnc_url: str | None = None
    artifacts_dir: str = "/home/desktop/artifacts"


class DebugUrls(StrictBaseModel):
    vnc: str | None = None
    daemon: str | None = None
    recording_dashboard: str | None = None


class ActionDecision(StrictBaseModel):
    decision: Literal["allow", "deny", "ask_user", "handoff"]
    reason: str | None = None


class BaseAction(StrictBaseModel):
    type: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    call_id: str | None = None
    sequence: int | None = None
    timeout_ms: int | None = Field(default=None, gt=0)


class MoveAction(BaseAction):
    type: Literal["move"] = "move"
    x: int = Field(ge=0)
    y: int = Field(ge=0)


class ClickAction(BaseAction):
    type: Literal["click"] = "click"
    x: int | None = Field(default=None, ge=0)
    y: int | None = Field(default=None, ge=0)
    button: Button = "left"
    modifiers: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coordinate_pair(self) -> ClickAction:
        if (self.x is None) != (self.y is None):
            raise ValueError("x and y must be supplied together")
        return self


class DoubleClickAction(ClickAction):
    type: Literal["double_click"] = "double_click"


class TripleClickAction(ClickAction):
    type: Literal["triple_click"] = "triple_click"


class DragAction(BaseAction):
    type: Literal["drag"] = "drag"
    start_x: int | None = Field(default=None, ge=0)
    start_y: int | None = Field(default=None, ge=0)
    end_x: int | None = Field(default=None, ge=0)
    end_y: int | None = Field(default=None, ge=0)
    path: list[Point] | None = None
    button: Button = "left"
    duration_ms: int = Field(default=500, ge=0, le=60_000)
    modifiers: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _valid_drag_shape(self) -> DragAction:
        has_path = self.path is not None
        has_end = self.end_x is not None and self.end_y is not None
        has_partial_start = (self.start_x is None) != (self.start_y is None)
        has_partial_end = (self.end_x is None) != (self.end_y is None)
        if has_partial_start or has_partial_end:
            raise ValueError("drag coordinates must be supplied as x/y pairs")
        if has_path and len(self.path or []) < 2:
            raise ValueError("drag path must contain at least two points")
        if not has_path and not has_end:
            raise ValueError("drag requires path or end coordinates")
        return self


class ScrollAction(BaseAction):
    type: Literal["scroll"] = "scroll"
    direction: ScrollDirection = "down"
    amount: int = Field(default=1, ge=1, le=10_000)
    x: int | None = Field(default=None, ge=0)
    y: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _coordinate_pair(self) -> ScrollAction:
        if (self.x is None) != (self.y is None):
            raise ValueError("x and y must be supplied together")
        return self


class MouseDownAction(BaseAction):
    type: Literal["mouse_down"] = "mouse_down"
    button: Button = "left"
    x: int | None = Field(default=None, ge=0)
    y: int | None = Field(default=None, ge=0)


class MouseUpAction(MouseDownAction):
    type: Literal["mouse_up"] = "mouse_up"


class TypeAction(BaseAction):
    type: Literal["type"] = "type"
    text: str
    delay_ms: int = Field(default=10, ge=0, le=10_000)
    method: Literal["auto", "xdotool", "clipboard"] = "auto"

    @field_validator("text")
    @classmethod
    def _safe_text(cls, value: str) -> str:
        for char in value:
            code = ord(char)
            if code < 32 and char not in ("\n", "\r"):
                raise ValueError("control characters are not allowed; use keypress/hotkey")
        return value


class KeyPressAction(BaseAction):
    type: Literal["keypress"] = "keypress"
    key: str
    modifiers: list[str] = Field(default_factory=list)
    duration_ms: int = Field(default=0, ge=0, le=60_000)


class HotkeyAction(BaseAction):
    type: Literal["hotkey"] = "hotkey"
    keys: list[str] = Field(min_length=1)
    duration_ms: int = Field(default=0, ge=0, le=60_000)


class HoldKeyAction(BaseAction):
    type: Literal["hold_key"] = "hold_key"
    key: str
    duration_ms: int | None = Field(default=None, ge=0, le=60_000)
    actions: list[dict[str, Any]] | None = None


class WaitAction(BaseAction):
    type: Literal["wait"] = "wait"
    duration_ms: int = Field(ge=0, le=600_000)


class ScreenshotAction(BaseAction):
    type: Literal["screenshot"] = "screenshot"
    options: ScreenshotOptions | None = None


class ZoomAction(BaseAction):
    type: Literal["zoom"] = "zoom"
    region: Region
    scale: float = Field(default=2.0, gt=0, le=8)
    options: ScreenshotOptions | None = None


class CursorPositionAction(BaseAction):
    type: Literal["cursor_position"] = "cursor_position"


class ReleaseAllAction(BaseAction):
    type: Literal["release_all"] = "release_all"


type ComputerAction = Annotated[
    MoveAction
    | ClickAction
    | DoubleClickAction
    | TripleClickAction
    | DragAction
    | ScrollAction
    | MouseDownAction
    | MouseUpAction
    | TypeAction
    | KeyPressAction
    | HotkeyAction
    | HoldKeyAction
    | WaitAction
    | ScreenshotAction
    | ZoomAction
    | CursorPositionAction
    | ReleaseAllAction,
    Field(discriminator="type"),
]

ComputerActionAdapter = TypeAdapter(ComputerAction)


def parse_action(action: ComputerAction | dict[str, Any]) -> ComputerAction:
    try:
        return ComputerActionAdapter.validate_python(action)
    except Exception as exc:  # pydantic exposes multiple validation exception paths.
        raise ActionValidationError(str(exc)) from exc


class ActionItemResult(StrictBaseModel):
    index: int = Field(ge=0)
    type: str
    ok: bool
    elapsed_ms: float | None = Field(default=None, ge=0)
    error_code: str | None = None
    error: str | None = None
    output: dict[str, Any] = Field(default_factory=dict)


class ActionBatchTiming(StrictBaseModel):
    daemon_ms: float = Field(ge=0)


class ActionBatchResult(StrictBaseModel):
    ok: bool
    call_id: str | None = None
    results: list[ActionItemResult]
    screenshot: Screenshot | None = None
    timing: ActionBatchTiming | None = None


class ActionBatchRequest(StrictBaseModel):
    actions: list[ComputerAction]
    screenshot_after: bool = False
    screenshot_options: ScreenshotOptions | None = None
    continue_on_error: bool = False
    source: str = "sdk"
    call_id: str | None = None
    run_id: str | None = None
    sequence: int | None = None
    max_action_timeout_ms: int | None = Field(default=None, gt=0)


class ValidationResult(StrictBaseModel):
    ok: bool
    errors: list[str] = Field(default_factory=list)


class VersionInfo(StrictBaseModel):
    api_version: str = "v1"
    daemon_version: str
    sdk_min_version: str
    sdk_max_version: str
    image_profile: str = "standard"
    modal_computer_use_package: str


class Capabilities(StrictBaseModel):
    primitives: list[str]
    screenshot_formats: list[ImageFormat]
    action_types: list[str]
    adapter_versions: dict[str, list[str]]
    image_profile: str
    vnc_enabled: bool


class TraceEntry(StrictBaseModel):
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    run_id: str | None = None
    call_id: str
    sequence: int | None = None
    source: str
    provider_action: dict[str, Any] | None = None
    normalized_action: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    elapsed_ms: float | None = None
    screenshot_before_uri: str | None = None
    screenshot_after_uri: str | None = None
    coordinate_space: CoordinateSpace | None = None
    redactions: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
