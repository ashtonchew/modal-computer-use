from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from modal_computer_use.models import Button, Point, Region, ScreenshotOptions, ScrollDirection


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextRequest(Schema):
    text: str


class TypeRequest(TextRequest):
    delay_ms: int = Field(default=10, ge=0, le=10_000)
    method: str = "auto"


class KeyRequest(Schema):
    key: str
    modifiers: list[str] = Field(default_factory=list)
    duration_ms: int = Field(default=0, ge=0, le=60_000)


class HotkeyRequest(Schema):
    keys: list[str] = Field(min_length=1)
    duration_ms: int = Field(default=0, ge=0, le=60_000)


class HoldRequest(Schema):
    key: str
    duration_ms: int | None = Field(default=None, ge=0, le=60_000)
    actions: list[dict[str, Any]] = Field(default_factory=list)


class MouseMoveRequest(Point):
    pass


class MouseClickRequest(Schema):
    x: int | None = Field(default=None, ge=0)
    y: int | None = Field(default=None, ge=0)
    button: Button = "left"
    double: bool = False
    modifiers: list[str] = Field(default_factory=list)


class MouseDragRequest(Schema):
    start_x: int | None = Field(default=None, ge=0)
    start_y: int | None = Field(default=None, ge=0)
    end_x: int | None = Field(default=None, ge=0)
    end_y: int | None = Field(default=None, ge=0)
    path: list[Point] | None = None
    duration_ms: int = Field(default=500, ge=0, le=60_000)
    modifiers: list[str] = Field(default_factory=list)


class MouseScrollRequest(Schema):
    direction: ScrollDirection = "down"
    amount: int = Field(default=1, ge=1, le=10_000)
    x: int | None = Field(default=None, ge=0)
    y: int | None = Field(default=None, ge=0)


class MouseButtonRequest(Schema):
    button: Button = "left"
    x: int | None = Field(default=None, ge=0)
    y: int | None = Field(default=None, ge=0)


class ScreenshotRequest(ScreenshotOptions):
    region: Region | None = None


class ZoomScreenshotRequest(Schema):
    region: Region
    scale: float = Field(default=2.0, gt=0, le=8)
    format: str = "png"
    quality: int = Field(default=90, ge=1, le=100)
    show_cursor: bool = True
    storage: str = "inline"

    @field_validator("format")
    @classmethod
    def _valid_format(cls, value: str) -> str:
        if value not in ("png", "jpeg", "webp"):
            raise ValueError("format must be png, jpeg, or webp")
        return value

    @field_validator("storage")
    @classmethod
    def _valid_storage(cls, value: str) -> str:
        if value not in ("inline", "artifact", "auto"):
            raise ValueError("storage must be inline, artifact, or auto")
        return value


class RecordingStartRequest(Schema):
    name: str | None = None
    fps: int = Field(default=12, ge=1, le=120)
    format: str = "mp4"

    @field_validator("format")
    @classmethod
    def _valid_format(cls, value: str) -> str:
        if value != "mp4":
            raise ValueError("format must be mp4")
        return value


class LaunchRequest(Schema):
    command: str
    args: list[str] = Field(default_factory=list)


class OpenArtifactRequest(Schema):
    path: str


class BrowserOpenUrlRequest(Schema):
    url: str
    wait_for_window: bool = True


class CommandRunRequest(Schema):
    command: list[str] = Field(min_length=1)
    timeout: float = Field(default=30.0, gt=0, le=600)
