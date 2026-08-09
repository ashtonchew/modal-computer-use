from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import struct
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from ..errors import FrameValidationError
from ..models import ActionBatchResult, Screenshot
from .models import ComputerStepResult, ComputerStepTiming

STEP_ENVELOPE_MAGIC = b"MCUSTEP\x00"
STEP_ENVELOPE_VERSION = 1
STEP_ENVELOPE_PREFIX = struct.Struct(">HIHQ")
STEP_PROTOCOL = "computer-use.step.v1"
STEP_MEDIA_TYPE = "application/vnd.modal-computer-use.step;v=1"

# These are deliberately finite.  The daemon and SDK must reject a response
# before allocating untrusted lengths from its prefix or manifest.
MAX_STEP_MANIFEST_BYTES = 1 * 1024 * 1024
MAX_STEP_SEGMENTS = 501
MAX_STEP_SEGMENT_BYTES = 32 * 1024 * 1024
MAX_STEP_PAYLOAD_BYTES = 64 * 1024 * 1024
MAX_STEP_ENVELOPE_BYTES = (
    len(STEP_ENVELOPE_MAGIC)
    + STEP_ENVELOPE_PREFIX.size
    + MAX_STEP_MANIFEST_BYTES
    + MAX_STEP_PAYLOAD_BYTES
)


class StepEnvelopeError(FrameValidationError):
    """Raised when a computer-step response is malformed or out of bounds."""

    def __init__(self) -> None:
        super().__init__("invalid computer step envelope")


def encode_step_envelope(
    *,
    actions: ActionBatchResult | Mapping[str, Any],
    screenshot: Screenshot | Mapping[str, Any],
    timing: ComputerStepTiming | Mapping[str, Any],
) -> bytes:
    """Encode one bounded action result and final screenshot response.

    Screenshot bytes found in the final result or anywhere in action output
    are moved to ordered binary segments.  The manifest retains all semantic
    metadata and contains only opaque segment references.
    """

    segments: list[bytes] = []
    action_payload = _to_python(actions)
    action_payload = _extract_screenshot_segments(action_payload, segments)
    screenshot_payload = _extract_screenshot_segments(_to_python(screenshot), segments)
    final_ref = _screenshot_segment_ref(screenshot_payload)
    if final_ref is None or not segments:
        raise StepEnvelopeError()
    references = [*_segment_references(action_payload), final_ref]
    if len(references) != len(segments) or set(references) != set(range(len(segments))):
        raise StepEnvelopeError()
    timing_payload = _to_json_value(_to_python(timing))
    manifest = {
        "protocol": STEP_PROTOCOL,
        "actions": _to_json_value(action_payload),
        "screenshot": _to_json_value(screenshot_payload),
        "timing": timing_payload,
        "segments": [
            {
                "kind": "screenshot",
                "length": len(segment),
                "sha256": hashlib.sha256(segment).hexdigest(),
            }
            for segment in segments
        ],
    }
    if len(segments) > MAX_STEP_SEGMENTS:
        raise StepEnvelopeError()
    payload = b"".join(segments)
    if len(payload) > MAX_STEP_PAYLOAD_BYTES:
        raise StepEnvelopeError()
    try:
        manifest_bytes = json.dumps(
            manifest,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise StepEnvelopeError() from exc
    if len(manifest_bytes) > MAX_STEP_MANIFEST_BYTES:
        raise StepEnvelopeError()
    prefix = STEP_ENVELOPE_MAGIC + STEP_ENVELOPE_PREFIX.pack(
        STEP_ENVELOPE_VERSION,
        len(manifest_bytes),
        len(segments),
        len(payload),
    )
    return prefix + manifest_bytes + payload


def decode_step_envelope(data: bytes) -> ComputerStepResult:
    """Decode and validate one complete step envelope without exposing secrets."""

    try:
        if not isinstance(data, bytes):
            raise StepEnvelopeError()
        prefix_size = len(STEP_ENVELOPE_MAGIC) + STEP_ENVELOPE_PREFIX.size
        if len(data) < prefix_size:
            raise StepEnvelopeError()
        if data[: len(STEP_ENVELOPE_MAGIC)] != STEP_ENVELOPE_MAGIC:
            raise StepEnvelopeError()
        version, manifest_length, segment_count, payload_length = STEP_ENVELOPE_PREFIX.unpack(
            data[len(STEP_ENVELOPE_MAGIC) : prefix_size]
        )
        if version != STEP_ENVELOPE_VERSION:
            raise StepEnvelopeError()
        if manifest_length > MAX_STEP_MANIFEST_BYTES:
            raise StepEnvelopeError()
        if segment_count > MAX_STEP_SEGMENTS or payload_length > MAX_STEP_PAYLOAD_BYTES:
            raise StepEnvelopeError()
        manifest_start = prefix_size
        payload_start = manifest_start + manifest_length
        payload_end = payload_start + payload_length
        if payload_end != len(data):
            raise StepEnvelopeError()
        manifest = json.loads(
            data[manifest_start:payload_start].decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"protocol", "actions", "screenshot", "timing", "segments"}
            or manifest.get("protocol") != STEP_PROTOCOL
        ):
            raise StepEnvelopeError()
        segment_descriptors = manifest.get("segments")
        if not isinstance(segment_descriptors, list) or len(segment_descriptors) != segment_count:
            raise StepEnvelopeError()
        references = _segment_references(manifest.get("actions"))
        final_ref = _screenshot_segment_ref(manifest.get("screenshot"))
        if final_ref is None:
            raise StepEnvelopeError()
        references.append(final_ref)
        if len(references) != segment_count or set(references) != set(range(segment_count)):
            raise StepEnvelopeError()
        segments: list[bytes] = []
        offset = payload_start
        for descriptor in segment_descriptors:
            if (
                not isinstance(descriptor, dict)
                or set(descriptor) != {"kind", "length", "sha256"}
                or descriptor.get("kind") != "screenshot"
            ):
                raise StepEnvelopeError()
            length = descriptor.get("length")
            digest = descriptor.get("sha256")
            if (
                isinstance(length, bool)
                or not isinstance(length, int)
                or length < 0
                or length <= 0
                or length > MAX_STEP_SEGMENT_BYTES
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or offset + length > payload_end
            ):
                raise StepEnvelopeError()
            segment = data[offset : offset + length]
            if hashlib.sha256(segment).hexdigest() != digest:
                raise StepEnvelopeError()
            segments.append(segment)
            offset += length
        if offset != payload_end:
            raise StepEnvelopeError()
        actions_payload = _rehydrate_screenshot_segments(manifest.get("actions"), segments)
        screenshot_payload = _rehydrate_screenshot_segments(
            manifest.get("screenshot"), segments
        )
        timing_payload = manifest.get("timing")
        actions = ActionBatchResult.model_validate(actions_payload)
        screenshot = Screenshot.model_validate(screenshot_payload)
        timing = ComputerStepTiming.model_validate(timing_payload)
        return ComputerStepResult(actions=actions, screenshot=screenshot, timing=timing)
    except StepEnvelopeError:
        raise
    except Exception as exc:
        raise StepEnvelopeError() from exc


def _to_python(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    return value


def _extract_screenshot_segments(value: Any, segments: list[bytes]) -> Any:
    value = _to_python(value)
    if isinstance(value, Mapping):
        screenshot_bytes = _screenshot_bytes(value)
        if screenshot_bytes is not None:
            if not screenshot_bytes or len(screenshot_bytes) > MAX_STEP_SEGMENT_BYTES:
                raise StepEnvelopeError()
            index = len(segments)
            segments.append(screenshot_bytes)
            result = {
                key: _extract_screenshot_segments(item, segments)
                for key, item in value.items()
                if key not in {"bytes", "data_base64"}
            }
            result["__step_segment__"] = index
            return result
        return {
            str(key): _extract_screenshot_segments(item, segments)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_extract_screenshot_segments(item, segments) for item in value]
    if isinstance(value, tuple):
        return [_extract_screenshot_segments(item, segments) for item in value]
    return value


def _screenshot_bytes(value: Mapping[str, Any]) -> bytes | None:
    if not _looks_like_screenshot(value):
        return None
    raw = value.get("bytes")
    if isinstance(raw, bytes):
        data = raw
    elif isinstance(raw, str):
        try:
            data = base64.b64decode(raw, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise StepEnvelopeError() from exc
    else:
        encoded = value.get("data_base64")
        if not isinstance(encoded, str):
            return None
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise StepEnvelopeError() from exc
    declared_size = value.get("size_bytes")
    declared_sha = value.get("sha256")
    if (
        isinstance(declared_size, bool)
        or not isinstance(declared_size, int)
        or declared_size != len(data)
        or not isinstance(declared_sha, str)
        or len(declared_sha) != 64
        or any(character not in "0123456789abcdef" for character in declared_sha)
        or hashlib.sha256(data).hexdigest() != declared_sha
    ):
        raise StepEnvelopeError()
    _validate_screenshot_metadata(value, data)
    return data


def _looks_like_screenshot(value: Mapping[str, Any]) -> bool:
    return all(key in value for key in ("format", "width", "height", "size_bytes")) and (
        "coordinate_space" in value
    )


def _rehydrate_screenshot_segments(value: Any, segments: Sequence[bytes]) -> Any:
    if isinstance(value, Mapping):
        if "__step_segment__" in value:
            index = value.get("__step_segment__")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise StepEnvelopeError()
            if index >= len(segments):
                raise StepEnvelopeError()
            segment = segments[index]
            result = {
                key: _rehydrate_screenshot_segments(item, segments)
                for key, item in value.items()
                if key != "__step_segment__"
            }
            if (
                result.get("size_bytes") != len(segment)
                or result.get("sha256") != hashlib.sha256(segment).hexdigest()
            ):
                raise StepEnvelopeError()
            result["bytes"] = segment
            result.pop("data_base64", None)
            _validate_screenshot_metadata(result, segment)
            return result
        return {
            str(key): _rehydrate_screenshot_segments(item, segments)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rehydrate_screenshot_segments(item, segments) for item in value]
    return value


def _to_json_value(value: Any) -> Any:
    value = _to_python(value)
    if isinstance(value, Mapping):
        return {str(key): _to_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_value(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        raise StepEnvelopeError()
    if isinstance(value, float) and not math.isfinite(value):
        raise StepEnvelopeError()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise StepEnvelopeError()


def _screenshot_segment_ref(value: Any) -> int | None:
    if not isinstance(value, Mapping) or not _looks_like_screenshot(value):
        return None
    reference = value.get("__step_segment__")
    if isinstance(reference, bool) or not isinstance(reference, int) or reference < 0:
        return None
    return reference


def _segment_references(value: Any) -> list[int]:
    if isinstance(value, Mapping):
        references: list[int] = []
        if "__step_segment__" in value:
            reference = _screenshot_segment_ref(value)
            if reference is None:
                raise StepEnvelopeError()
            references.append(reference)
        for item in value.values():
            references.extend(_segment_references(item))
        return references
    if isinstance(value, list):
        references = []
        for item in value:
            references.extend(_segment_references(item))
        return references
    return []


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StepEnvelopeError()
        result[key] = value
    return result


def _reject_non_finite(value: str) -> Any:
    raise StepEnvelopeError()


def _validate_screenshot_metadata(value: Mapping[str, Any], data: bytes) -> None:
    """Apply the semantic checks shared by every step image segment."""

    screenshot = Screenshot.model_validate(value)
    coordinate_space = screenshot.coordinate_space
    if (
        screenshot.size_bytes != len(data)
        or screenshot.sha256 != hashlib.sha256(data).hexdigest()
        or coordinate_space.image_width != screenshot.width
        or coordinate_space.image_height != screenshot.height
        or screenshot.captured_at.utcoffset() is None
    ):
        raise StepEnvelopeError()
    source_width = (
        coordinate_space.source_region.width
        if coordinate_space.source_region is not None
        else coordinate_space.desktop_width
    )
    source_height = (
        coordinate_space.source_region.height
        if coordinate_space.source_region is not None
        else coordinate_space.desktop_height
    )
    if not math.isclose(
        coordinate_space.scale_x,
        screenshot.width / source_width,
        rel_tol=1e-9,
        abs_tol=1e-9,
    ) or not math.isclose(
        coordinate_space.scale_y,
        screenshot.height / source_height,
        rel_tol=1e-9,
        abs_tol=1e-9,
    ):
        raise StepEnvelopeError()
    cursor = screenshot.cursor_position
    if cursor is not None and (
        cursor.x >= coordinate_space.desktop_width
        or cursor.y >= coordinate_space.desktop_height
    ):
        raise StepEnvelopeError()
