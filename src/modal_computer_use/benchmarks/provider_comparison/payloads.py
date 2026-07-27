from __future__ import annotations

import base64
from io import BytesIO
from typing import Any

from PIL import Image


def describe_screenshot_payload(value: Any) -> dict[str, Any]:
    return _describe_screenshot_payload_at(value, source="provider_return_value")


def validated_screenshot_size(payload: dict[str, Any], *, provider: str) -> int:
    size_bytes = payload.get("decoded_size_bytes")
    width = payload.get("width")
    height = payload.get("height")
    if (
        not isinstance(size_bytes, int)
        or size_bytes <= 0
        or not isinstance(payload.get("format"), str)
        or not isinstance(width, int)
        or width <= 0
        or not isinstance(height, int)
        or height <= 0
    ):
        raise RuntimeError(f"{provider} screenshot did not contain a fully decoded image")
    return size_bytes


def _describe_screenshot_payload_at(value: Any, *, source: str) -> dict[str, Any]:
    if value is None:
        return {"source": source, "transport_size_bytes": 0, "decoded_size_bytes": 0}
    if isinstance(value, bytes | bytearray):
        payload = bytes(value)
        return {
            "source": source,
            "transport_encoding": "raw_bytes",
            "transport_size_bytes": len(payload),
            "decoded_size_bytes": len(payload),
            **_image_payload_metadata(payload),
        }
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        decoded = _decode_base64_payload(value)
        return {
            "source": source,
            "transport_encoding": "base64_string" if decoded is not None else "text",
            "transport_size_bytes": len(encoded),
            "decoded_size_bytes": len(decoded) if decoded is not None else None,
            **(_image_payload_metadata(decoded) if decoded is not None else {}),
        }
    read = getattr(value, "read", None)
    if callable(read):
        return _describe_screenshot_payload_at(read(), source=f"{source}.read()")

    provider_reported_size = getattr(value, "size_bytes", None)
    for attribute in (
        "bytes",
        "data",
        "image",
        "screenshot",
        "image_base64",
        "base64_string",
        "base64",
    ):
        payload = getattr(value, attribute, None)
        if payload is not None:
            metadata = _describe_screenshot_payload_at(payload, source=f"{source}.{attribute}")
            if isinstance(provider_reported_size, int | float):
                metadata["provider_reported_size_bytes"] = int(provider_reported_size)
            return metadata
    return {
        "source": source,
        "transport_size_bytes": 0,
        "decoded_size_bytes": 0,
        "provider_reported_size_bytes": (
            int(provider_reported_size) if isinstance(provider_reported_size, int | float) else None
        ),
    }


def _decode_base64_payload(value: str) -> bytes | None:
    try:
        return base64.b64decode(value, validate=True)
    except ValueError:
        return None


def _image_payload_metadata(payload: bytes) -> dict[str, Any]:
    try:
        with Image.open(BytesIO(payload)) as image:
            image.load()
            return {
                "format": image.format.lower() if image.format else None,
                "width": image.width,
                "height": image.height,
            }
    except Exception as exc:
        raise ValueError("provider screenshot pixels could not be fully decoded") from exc
