from __future__ import annotations

import re
from re import _parser as _regex_parser
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from modal_computer_use._invariants import (
    require_coordinate_pair,
    require_drag_shape,
    require_safe_text,
)
from modal_computer_use.models import (
    ActionBatchRequest,
    Button,
    ImageFormat,
    Point,
    Region,
    ScreenshotOptions,
    ScreenshotStorage,
    ScrollDirection,
)


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ObservationActionCaptureRequest(ActionBatchRequest):
    capture_delay_ms: int = Field(default=0, ge=0, le=60_000)


class ObservationActionObserveChangeRequest(ObservationActionCaptureRequest):
    change_timeout_ms: int = Field(default=100, ge=0, le=60_000)
    poll_interval_ms: int = Field(default=8, ge=1, le=1_000)
    poll_strategy: Literal["fixed", "adaptive"] = "fixed"
    change_detection: Literal["full", "region", "auto_region"] = "full"
    change_signal: Literal["poll", "xdamage", "auto"] = "auto"
    dirty_frame_producer: Literal["auto", "off"] = "auto"
    dirty_frame_producer_wait_ms: int | None = Field(default=None, ge=0, le=60_000)
    dirty_region_confirmation: Literal["auto", "off"] = "auto"
    full_frame_fallback: bool = True
    frame_encoding: Literal["json-binary", "binary-envelope"] | None = None
    change_detection_region: Region | None = None
    change_region_radius: int = Field(default=192, ge=1, le=10_000)


class ActionObserveChangeScreenshotRequest(ActionBatchRequest):
    screenshot_options: ScreenshotOptions = Field(default_factory=ScreenshotOptions)
    previous_source_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    capture_delay_ms: int = Field(default=0, ge=0, le=60_000)
    change_timeout_ms: int = Field(default=100, ge=0, le=60_000)
    poll_interval_ms: int = Field(default=8, ge=1, le=1_000)
    poll_strategy: Literal["fixed", "adaptive"] = "fixed"
    change_detection: Literal["full", "region", "auto_region"] = "full"
    change_signal: Literal["poll", "xdamage", "auto"] = "auto"
    change_detection_region: Region | None = None
    change_region_radius: int = Field(default=192, ge=1, le=10_000)

    @field_validator("previous_source_sha256")
    @classmethod
    def _valid_previous_source_sha256(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("previous_source_sha256 must be a lowercase sha256 hex digest")
        return value


class TextRequest(Schema):
    text: str


class TypeRequest(TextRequest):
    delay_ms: int = Field(default=10, ge=0, le=10_000)
    method: Literal["auto", "keystrokes", "xdotool", "clipboard"] = "auto"

    @field_validator("text")
    @classmethod
    def _safe_text(cls, value: str) -> str:
        return require_safe_text(value)


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
        require_coordinate_pair(self.x, self.y)
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
        require_drag_shape(
            start_x=self.start_x,
            start_y=self.start_y,
            end_x=self.end_x,
            end_y=self.end_y,
            path=self.path,
            coordinate_message="drag coordinates must be supplied as x/y pairs",
            start_coordinate_message="start coordinates must be supplied as x/y pairs",
            end_coordinate_message="end coordinates must be supplied as x/y pairs",
        )
        return self


class MouseScrollRequest(Schema):
    direction: ScrollDirection = "down"
    amount: int = Field(default=1, ge=1, le=10_000)
    x: int | None = Field(default=None, ge=0)
    y: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _coordinate_pair(self) -> MouseScrollRequest:
        require_coordinate_pair(self.x, self.y)
        return self


class MouseButtonRequest(Schema):
    button: Button = "left"
    x: int | None = Field(default=None, ge=0)
    y: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _coordinate_pair(self) -> MouseButtonRequest:
        require_coordinate_pair(self.x, self.y)
        return self


class ScreenshotRequest(ScreenshotOptions):
    region: Region | None = None


class ObservationStreamRequest(ScreenshotRequest):
    fps: float = Field(default=5.0, gt=0, le=30)
    max_frames: int | None = Field(default=None, ge=1, le=10_000)
    idle_timeout_ms: int | None = Field(default=None, ge=100, le=300_000)
    send_unchanged: bool = False
    transport_timing: bool = False
    frame_encoding: Literal["json-binary", "binary-envelope"] = "json-binary"
    delivery: Literal["latest", "reliable"] = "latest"
    keyframe_interval: int = Field(default=30, ge=1, le=10_000)
    delta_mode: Literal["auto", "off"] = "auto"
    delta_max_ratio: float = Field(default=0.35, ge=0, le=1)
    tile_size: int = Field(default=64, ge=16, le=512)
    max_patch_rects: int = Field(default=4, ge=1, le=16)
    multi_rect_min_savings: float = Field(default=0.3, ge=0, le=1)


class ObservationTransportProbeRequest(Schema):
    size_bytes: int = Field(default=0, ge=0, le=1_000_000)
    frame_encoding: Literal["json-binary", "binary-envelope"] = "json-binary"


class ZoomScreenshotRequest(Schema):
    region: Region
    scale: float = Field(default=2.0, gt=0, le=8)
    format: ImageFormat = "png"
    quality: int = Field(default=90, ge=1, le=100)
    show_cursor: bool = True
    storage: ScreenshotStorage = "inline"


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
        _validate_window_regex_safety(value)
        try:
            re.compile(value)
        except (re.error, OverflowError) as exc:
            raise ValueError("title_regex must be a valid regular expression") from exc
        return value

    @model_validator(mode="after")
    def _has_selector(self) -> WaitForWindowRequest:
        if self.title_regex is None and self.class_name is None and self.pid is None:
            raise ValueError("wait-for requires title_regex, class_name, or pid")
        return self


_MAX_WINDOW_REGEX_LENGTH = 256
_MAX_WINDOW_REGEX_REPEAT = 100
_MAX_WINDOW_REGEX_FIXED_REPEAT = 4096
_UNSAFE_WINDOW_REGEX_MARKERS = (
    "(?=",
    "(?!",
    "(?<=",
    "(?<!",
    "(?P=",
    "(?(",
)


def _validate_window_regex_safety(pattern: str) -> None:
    """Allow Python regex features whose repeat structure is bounded."""
    if len(pattern) > _MAX_WINDOW_REGEX_LENGTH:
        raise ValueError("title_regex is too long")
    if any(marker in pattern for marker in _UNSAFE_WINDOW_REGEX_MARKERS):
        raise ValueError("title_regex contains an unsupported construct")
    try:
        parsed = _regex_parser.parse(pattern, 0)
    except re.error:
        return
    except OverflowError as exc:
        raise ValueError("title_regex repeat bound is too large") from exc
    _validate_window_regex_tokens(parsed, flags=parsed.state.flags)


type _RegexCharacterSet = frozenset[int] | str
type _RegexRepeatProfile = tuple[str, _RegexCharacterSet | None]


def _validate_window_regex_tokens(
    tokens: list[tuple[Any, Any]],
    *,
    flags: int,
) -> tuple[bool, bool, bool, bool]:
    """Return empty, branch, repeat, and unresolved-ambiguity facts."""
    can_empty = True
    has_branch = False
    has_repeat = False
    previous_repeat: _RegexRepeatProfile | None = None
    active_repeats: list[_RegexCharacterSet | None] = []
    ambiguous_repeat_chain = False

    def note_repeat(profile: _RegexRepeatProfile | None) -> None:
        nonlocal active_repeats, ambiguous_repeat_chain
        if profile is None:
            return
        characters = profile[1]
        if any(
            not _character_sets_provably_disjoint(active, characters)
            for active in active_repeats
        ):
            ambiguous_repeat_chain = True
        active_repeats = [
            active
            for active in active_repeats
            if not _character_sets_provably_disjoint(active, characters)
        ]
        active_repeats.append(characters)

    def note_required(characters: _RegexCharacterSet | None) -> None:
        nonlocal active_repeats, ambiguous_repeat_chain
        if ambiguous_repeat_chain:
            raise ValueError("title_regex has separated ambiguous repeats")
        active_repeats = [
            active
            for active in active_repeats
            if not _character_sets_provably_disjoint(active, characters)
        ]
        if not active_repeats:
            ambiguous_repeat_chain = False

    for opcode, argument in tokens:
        name = getattr(opcode, "name", str(opcode))
        if name.startswith("GROUPREF"):
            raise ValueError("title_regex backreferences are unsupported")
        if name in {"ASSERT", "ASSERT_NOT"}:
            raise ValueError("title_regex lookarounds are unsupported")
        if name in {"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"}:
            minimum, maximum, body = argument
            if maximum != _regex_parser.MAXREPEAT:
                if minimum == maximum and maximum > _MAX_WINDOW_REGEX_FIXED_REPEAT:
                    raise ValueError("title_regex repeat bound is too large")
                if minimum != maximum and maximum > _MAX_WINDOW_REGEX_REPEAT:
                    raise ValueError("title_regex variable repeat bound is too large")
            body_empty, body_branch, body_repeat, _body_ambiguous = (
                _validate_window_regex_tokens(
                body,
                flags=flags,
            )
            )
            if body_empty or body_branch or body_repeat:
                raise ValueError("title_regex has an unsafe quantified group")
            current_repeat = (
                (repr(body), _regex_character_set(body, flags=flags))
                if minimum != maximum
                else None
            )
            _reject_overlapping_adjacent_repeats(previous_repeat, current_repeat)
            if current_repeat is not None:
                note_repeat(current_repeat)
            elif minimum > 0:
                note_required(_regex_character_set(body, flags=flags))
            previous_repeat = current_repeat
            has_repeat = True
            can_empty &= minimum == 0
            continue
        if name == "BRANCH":
            _none, branches = argument
            branch_facts = [
                _validate_window_regex_tokens(branch, flags=flags) for branch in branches
            ]
            branch_prefixes = [
                _regex_prefix_character_set(branch, flags=flags) for branch in branches
            ]
            if any(facts[0] for facts in branch_facts) or any(
                not _character_sets_provably_disjoint(left, right)
                for index, left in enumerate(branch_prefixes)
                for right in branch_prefixes[index + 1 :]
            ):
                raise ValueError("title_regex has ambiguous alternation")
            can_empty &= any(facts[0] for facts in branch_facts)
            has_branch = True
            branch_has_repeat = any(facts[2] for facts in branch_facts)
            has_repeat |= branch_has_repeat
            ambiguous_repeat_chain |= any(facts[3] for facts in branch_facts)
            if branch_has_repeat:
                note_repeat((repr(argument), None))
            elif any(_tokens_have_consuming(branch) for branch in branches):
                note_required(None)
            previous_repeat = None
            continue
        if name == "SUBPATTERN":
            _group, add_flags, del_flags, body = argument
            subpattern_flags = (flags | add_flags) & ~del_flags
            body_empty, body_branch, body_repeat, body_ambiguous = (
                _validate_window_regex_tokens(
                body,
                flags=subpattern_flags,
            )
            )
            can_empty &= body_empty
            has_branch |= body_branch
            has_repeat |= body_repeat
            ambiguous_repeat_chain |= body_ambiguous
            current_repeat = _sole_repeat_profile(body, flags=subpattern_flags)
            _reject_overlapping_adjacent_repeats(previous_repeat, current_repeat)
            if current_repeat is not None:
                note_repeat(current_repeat)
            elif body_repeat:
                current_repeat = (repr(body), None)
                _reject_overlapping_adjacent_repeats(previous_repeat, current_repeat)
                note_repeat(current_repeat)
            elif _tokens_have_consuming(body):
                note_required(_regex_character_set(body, flags=subpattern_flags))
            previous_repeat = current_repeat
            continue
        if name == "ATOMIC_GROUP":
            body_empty, _body_branch, body_repeat, body_ambiguous = (
                _validate_window_regex_tokens(
                argument,
                flags=flags,
            )
            )
            can_empty &= body_empty
            ambiguous_repeat_chain |= body_ambiguous
            current_repeat = _sole_repeat_profile(argument, flags=flags)
            _reject_overlapping_adjacent_repeats(previous_repeat, current_repeat)
            if current_repeat is not None:
                note_repeat(current_repeat)
            elif body_repeat:
                current_repeat = (repr(argument), None)
                _reject_overlapping_adjacent_repeats(previous_repeat, current_repeat)
                note_repeat(current_repeat)
            elif _tokens_have_consuming(argument):
                note_required(None)
            previous_repeat = current_repeat
            continue
        if name in {"AT", "SUCCESS", "FAILURE"}:
            continue
        can_empty = False
        note_required(_regex_character_set([(opcode, argument)], flags=flags))
        previous_repeat = None
    return can_empty, has_branch, has_repeat, ambiguous_repeat_chain


def _sole_repeat_profile(
    tokens: list[tuple[Any, Any]],
    *,
    flags: int,
) -> _RegexRepeatProfile | None:
    if len(tokens) != 1:
        return None
    opcode, argument = tokens[0]
    name = getattr(opcode, "name", str(opcode))
    if name in {"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"}:
        minimum, maximum, body = argument
        if minimum == maximum:
            return None
        return repr(body), _regex_character_set(body, flags=flags)
    if name == "SUBPATTERN":
        _group, add_flags, del_flags, body = argument
        return _sole_repeat_profile(body, flags=(flags | add_flags) & ~del_flags)
    return None


def _tokens_have_consuming(tokens: list[tuple[Any, Any]]) -> bool:
    return any(
        getattr(opcode, "name", str(opcode)) not in {"AT", "SUCCESS", "FAILURE"}
        for opcode, _argument in tokens
    )


def _reject_overlapping_adjacent_repeats(
    previous: _RegexRepeatProfile | None,
    current: _RegexRepeatProfile | None,
) -> None:
    if previous is None or current is None:
        return
    previous_key, previous_characters = previous
    current_key, current_characters = current
    if previous_key == current_key or not _character_sets_provably_disjoint(
        previous_characters,
        current_characters,
    ):
        raise ValueError("title_regex has adjacent ambiguous repeats")


def _regex_character_set(
    tokens: list[tuple[Any, Any]],
    *,
    flags: int,
) -> _RegexCharacterSet | None:
    if len(tokens) != 1:
        return None
    opcode, argument = tokens[0]
    name = getattr(opcode, "name", str(opcode))
    if name == "SUBPATTERN":
        _group, add_flags, del_flags, body = argument
        return _regex_character_set(body, flags=(flags | add_flags) & ~del_flags)
    if name == "CATEGORY":
        return f"category:{getattr(argument, 'name', argument)}"
    if name == "LITERAL":
        return None if flags & re.IGNORECASE else frozenset({int(argument)})
    if name != "IN" or flags & re.IGNORECASE:
        return None
    characters: set[int] = set()
    category: str | None = None
    for item_opcode, item_argument in argument:
        item_name = getattr(item_opcode, "name", str(item_opcode))
        if item_name == "LITERAL":
            characters.add(int(item_argument))
        elif item_name == "RANGE":
            lower, upper = item_argument
            if upper - lower > _MAX_WINDOW_REGEX_FIXED_REPEAT:
                return None
            characters.update(range(lower, upper + 1))
        elif item_name == "CATEGORY" and not characters and category is None:
            category = f"category:{getattr(item_argument, 'name', item_argument)}"
        else:
            return None
    if category is not None:
        return category if not characters else None
    return frozenset(characters)


def _regex_prefix_character_set(
    tokens: list[tuple[Any, Any]],
    *,
    flags: int,
) -> _RegexCharacterSet | None:
    for opcode, argument in tokens:
        name = getattr(opcode, "name", str(opcode))
        if name in {"AT", "SUCCESS", "FAILURE"}:
            continue
        if name == "SUBPATTERN":
            _group, add_flags, del_flags, body = argument
            return _regex_prefix_character_set(
                body,
                flags=(flags | add_flags) & ~del_flags,
            )
        if name in {"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"}:
            minimum, _maximum, body = argument
            if minimum == 0:
                return None
            return _regex_prefix_character_set(body, flags=flags)
        return _regex_character_set([(opcode, argument)], flags=flags)
    return None


def _character_sets_provably_disjoint(
    left: _RegexCharacterSet | None,
    right: _RegexCharacterSet | None,
) -> bool:
    if left is None or right is None:
        return False
    if isinstance(left, frozenset) and isinstance(right, frozenset):
        return left.isdisjoint(right)
    if isinstance(left, frozenset) and isinstance(right, str):
        return all(not _category_matches(right, value) for value in left)
    if isinstance(left, str) and isinstance(right, frozenset):
        return all(not _category_matches(left, value) for value in right)
    assert isinstance(left, str) and isinstance(right, str)
    pair = frozenset({left.removeprefix("category:"), right.removeprefix("category:")})
    return pair in {
        frozenset({"CATEGORY_DIGIT", "CATEGORY_NOT_DIGIT"}),
        frozenset({"CATEGORY_SPACE", "CATEGORY_NOT_SPACE"}),
        frozenset({"CATEGORY_WORD", "CATEGORY_NOT_WORD"}),
        frozenset({"CATEGORY_DIGIT", "CATEGORY_SPACE"}),
        frozenset({"CATEGORY_DIGIT", "CATEGORY_NOT_WORD"}),
        frozenset({"CATEGORY_SPACE", "CATEGORY_WORD"}),
    }


def _category_matches(category: str, value: int) -> bool:
    character = chr(value)
    name = category.removeprefix("category:")
    predicates = {
        "CATEGORY_DIGIT": character.isdecimal(),
        "CATEGORY_NOT_DIGIT": not character.isdecimal(),
        "CATEGORY_SPACE": character.isspace(),
        "CATEGORY_NOT_SPACE": not character.isspace(),
        "CATEGORY_WORD": character.isalnum() or character == "_",
        "CATEGORY_NOT_WORD": not (character.isalnum() or character == "_"),
    }
    return predicates.get(name, True)


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


class BrowserRenderMetricsRequest(BrowserOpenUrlRequest):
    wait_for_window: bool = False
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)


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
