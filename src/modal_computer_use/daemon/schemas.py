from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from modal_computer_use.models import Button, Point, Region, ScreenshotOptions, ScrollDirection


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextRequest(Schema):
    text: str


class TypeRequest(TextRequest):
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

    @model_validator(mode="after")
    def _coordinate_pair(self) -> MouseClickRequest:
        if (self.x is None) != (self.y is None):
            raise ValueError("x and y must be supplied together")
        return self


class MouseDragRequest(Schema):
    start_x: int | None = Field(default=None, ge=0)
    start_y: int | None = Field(default=None, ge=0)
    end_x: int | None = Field(default=None, ge=0)
    end_y: int | None = Field(default=None, ge=0)
    path: list[Point] | None = None
    button: Button = "left"
    duration_ms: int = Field(default=500, ge=0, le=60_000)
    modifiers: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _valid_drag_shape(self) -> MouseDragRequest:
        if (self.start_x is None) != (self.start_y is None):
            raise ValueError("start coordinates must be supplied as x/y pairs")
        if (self.end_x is None) != (self.end_y is None):
            raise ValueError("end coordinates must be supplied as x/y pairs")
        if self.path is not None and len(self.path) < 2:
            raise ValueError("drag path must contain at least two points")
        if self.path is None and self.end_x is None and self.end_y is None:
            raise ValueError("drag requires path or end coordinates")
        return self


class MouseScrollRequest(Schema):
    direction: ScrollDirection = "down"
    amount: int = Field(default=1, ge=1, le=10_000)
    x: int | None = Field(default=None, ge=0)
    y: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _coordinate_pair(self) -> MouseScrollRequest:
        if (self.x is None) != (self.y is None):
            raise ValueError("x and y must be supplied together")
        return self


class MouseButtonRequest(Schema):
    button: Button = "left"
    x: int | None = Field(default=None, ge=0)
    y: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _coordinate_pair(self) -> MouseButtonRequest:
        if (self.x is None) != (self.y is None):
            raise ValueError("x and y must be supplied together")
        return self


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


class WaitForWindowRequest(Schema):
    title_regex: str | None = None
    class_name: str | None = None
    pid: int | None = Field(default=None, gt=0)
    timeout: float = Field(default=10.0, gt=0, le=300)

    @field_validator("title_regex", "class_name")
    @classmethod
    def _non_empty_selector(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("window selector cannot be empty")
        return value

    @field_validator("title_regex")
    @classmethod
    def _valid_title_regex(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError("title_regex must be a valid regular expression") from exc
        return value

    @model_validator(mode="after")
    def _has_selector(self) -> WaitForWindowRequest:
        if self.title_regex is None and self.class_name is None and self.pid is None:
            raise ValueError("wait-for requires title_regex, class_name, or pid")
        return self


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

    @field_validator("command")
    @classmethod
    def _valid_command(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("command must be non-empty and trimmed")
        if any(char.isspace() for char in value) or "\x00" in value or "/" in value:
            raise ValueError("command must be a single executable name")
        return value

    @field_validator("args")
    @classmethod
    def _valid_args(cls, value: list[str]) -> list[str]:
        for arg in value:
            if "\x00" in arg:
                raise ValueError("args must not contain NUL bytes")
        return value


class OpenArtifactRequest(Schema):
    path: str


class BrowserOpenUrlRequest(Schema):
    url: str
    wait_for_window: bool = True

    @field_validator("url")
    @classmethod
    def _valid_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("url must be an absolute http or https URL")
        if parsed.username or parsed.password:
            raise ValueError("url must not include credentials")
        return value


class CommandRunRequest(Schema):
    command: list[str] = Field(min_length=1)
    timeout: float = Field(default=30.0, gt=0, le=600)

    @field_validator("command")
    @classmethod
    def _valid_command_vector(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("command must contain at least one argv element")
        if value[0] == "":
            raise ValueError("command executable must be non-empty")
        for arg in value:
            if "\x00" in arg:
                raise ValueError("command arguments must not contain NUL bytes")
        return value
