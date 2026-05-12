from __future__ import annotations

from typing import Any

from modal_computer_use.models import ActionResult, Screenshot

_MEDIA_TYPES = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


def screenshot_media_type(screenshot: Screenshot) -> str:
    return _MEDIA_TYPES[screenshot.format]


def screenshot_data_url(screenshot: Screenshot) -> str:
    return f"data:{screenshot_media_type(screenshot)};base64,{screenshot.to_base64()}"


def screenshot_metadata(screenshot: Screenshot) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "format": screenshot.format,
        "width": screenshot.width,
        "height": screenshot.height,
        "size_bytes": screenshot.size_bytes,
        "captured_at": screenshot.captured_at.isoformat(),
        "coordinate_space": screenshot.coordinate_space.model_dump(mode="json"),
        "cursor_visible": screenshot.cursor_visible,
    }
    if screenshot.sha256 is not None:
        metadata["sha256"] = screenshot.sha256
    if screenshot.artifact_uri is not None:
        metadata["artifact_uri"] = screenshot.artifact_uri
    if screenshot.cursor_position is not None:
        metadata["cursor_position"] = screenshot.cursor_position.model_dump(mode="json")
    return metadata


def action_result_summary(result: ActionResult) -> dict[str, Any]:
    summary: dict[str, Any] = {"ok": result.ok}
    if result.message is not None:
        summary["message"] = result.message
    if result.elapsed_ms is not None:
        summary["elapsed_ms"] = result.elapsed_ms
    return summary
