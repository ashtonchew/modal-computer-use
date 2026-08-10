from __future__ import annotations

from typing import Any

import pytest

from modal_computer_use.errors import FrameValidationError
from modal_computer_use.namespaces.screenshots import (
    AsyncScreenshotsNamespace,
    ScreenshotsNamespace,
)


class _BinaryScreenshotClient:
    def __init__(
        self,
        *,
        data: bytes = b"image-bytes",
        header_overrides: dict[str, str] | None = None,
        removed_headers: tuple[str, ...] = (),
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.data = data
        self.header_overrides = header_overrides or {}
        self.removed_headers = removed_headers

    def post_bytes_with_headers(
        self,
        path: str,
        *,
        json: Any | None = None,
        headers=None,
        _mutation: bool = False,
    ):
        self.calls.append(
            {
                "method": "post_bytes_with_headers",
                "path": path,
                "json": json,
                "mutation": _mutation,
            }
        )
        response_headers = {
            "content-type": "image/png",
            "x-computer-use-width": "1024",
            "x-computer-use-height": "768",
            "x-computer-use-size-bytes": "11",
            "x-computer-use-sha256": (
                "2c8648d103e3dd7ad87660da0f126a1443b6d21ac1bd3ec000c5e24e2373a90c"
            ),
            "x-computer-use-captured-at": "2026-08-08T12:30:00+00:00",
            "x-computer-use-coordinate-space": (
                '{"desktop_width":1024,"desktop_height":768,'
                '"image_width":1024,"image_height":768,'
                '"scale_x":1.0,"scale_y":1.0,"source_region":null}'
            ),
            "x-computer-use-cursor-visible": "false",
            "x-computer-use-cursor-position": '{"x":12,"y":34}',
            "x-computer-use-timing-ms": "{}",
            "x-computer-use-capture-backend": "mss",
        }
        response_headers.update(self.header_overrides)
        for name in self.removed_headers:
            response_headers.pop(name)
        return self.data, response_headers

    def post_json(self, *args, **kwargs):
        raise AssertionError("inline screenshots must not fall back to JSON")


class _AsyncBinaryScreenshotClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def post_bytes_with_headers(self, *args, **kwargs):
        client = _BinaryScreenshotClient()
        response = client.post_bytes_with_headers(*args, **kwargs)
        self.calls.extend(client.calls)
        return response

    async def post_json(self, *args, **kwargs):
        raise AssertionError("inline screenshots must not fall back to JSON")


class _StructuredScreenshotClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post_json(
        self,
        path: str,
        *,
        json: Any | None = None,
        headers=None,
        _mutation: bool = False,
    ):
        self.calls.append(
            {
                "method": "post_json",
                "path": path,
                "json": json,
                "mutation": _mutation,
            }
        )
        response = {
            "format": "png",
            "width": 1024,
            "height": 768,
            "size_bytes": 11,
            "sha256": (
                "2c8648d103e3dd7ad87660da0f126a1443b6d21ac1bd3ec000c5e24e2373a90c"
            ),
            "captured_at": "2026-08-08T12:30:00+00:00",
            "coordinate_space": {
                "desktop_width": 1024,
                "desktop_height": 768,
                "image_width": 1024,
                "image_height": 768,
            },
        }
        if json["storage"] == "inline":
            response["data_base64"] = "aW1hZ2UtYnl0ZXM="
        else:
            response["artifact_uri"] = "artifact://screenshots/example.png"
        return response

    def post_bytes_with_headers(self, *args, **kwargs):
        raise AssertionError("non-inline screenshots must use the structured route")


class _AsyncStructuredScreenshotClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def post_json(self, *args, **kwargs):
        client = _StructuredScreenshotClient()
        response = client.post_json(*args, **kwargs)
        self.calls.extend(client.calls)
        return response

    async def post_bytes_with_headers(self, *args, **kwargs):
        raise AssertionError("non-inline screenshots must use the structured route")


def test_full_inline_returns_semantic_screenshot_from_one_binary_request() -> None:
    client = _BinaryScreenshotClient()
    namespace = ScreenshotsNamespace(client)  # type: ignore[arg-type]

    screenshot = namespace.full()

    assert screenshot.format == "png"
    assert screenshot.width == 1024
    assert screenshot.height == 768
    assert screenshot.size_bytes == 11
    assert screenshot.as_bytes() == b"image-bytes"
    assert screenshot.data_base64 is None
    assert screenshot.sha256 == (
        "2c8648d103e3dd7ad87660da0f126a1443b6d21ac1bd3ec000c5e24e2373a90c"
    )
    assert screenshot.captured_at.isoformat() == "2026-08-08T12:30:00+00:00"
    assert screenshot.coordinate_space.desktop_width == 1024
    assert screenshot.coordinate_space.image_height == 768
    assert screenshot.cursor_visible is False
    assert screenshot.cursor_position is not None
    assert (screenshot.cursor_position.x, screenshot.cursor_position.y) == (12, 34)
    assert client.calls == [
        {
            "method": "post_bytes_with_headers",
            "path": "/v1/screenshots/full/raw",
            "json": {
                "format": "png",
                "quality": 90,
                "scale": 1.0,
                "show_cursor": False,
                "processing": "auto",
                "storage": "inline",
            },
            "mutation": False,
        }
    ]


def test_full_inline_accepts_prefixed_route_timing_metadata() -> None:
    client = _BinaryScreenshotClient(
        header_overrides={
            "x-computer-use-timing-ms": (
                '{"total_ms":5.0,"route_ready_ms":0.1,'
                '"route_lock_wait_ms":0.2,"route_operation_ms":5.5,'
                '"route_total_ms":6.0}'
            )
        }
    )
    namespace = ScreenshotsNamespace(client)  # type: ignore[arg-type]

    screenshot = namespace.full()

    assert screenshot.as_bytes() == b"image-bytes"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_async_full_inline_returns_semantic_screenshot_from_one_binary_request() -> None:
    client = _AsyncBinaryScreenshotClient()
    namespace = AsyncScreenshotsNamespace(client)  # type: ignore[arg-type]

    screenshot = await namespace.full()

    assert screenshot.as_bytes() == b"image-bytes"
    assert screenshot.width == 1024
    assert screenshot.cursor_position is not None
    assert (screenshot.cursor_position.x, screenshot.cursor_position.y) == (12, 34)
    assert len(client.calls) == 1
    assert client.calls[0]["method"] == "post_bytes_with_headers"
    assert client.calls[0]["path"] == "/v1/screenshots/full/raw"


@pytest.mark.asyncio
async def test_async_internal_json_compat_uses_structured_inline_route() -> None:
    client = _AsyncStructuredScreenshotClient()
    namespace = AsyncScreenshotsNamespace(client)  # type: ignore[arg-type]

    screenshot = await namespace._full_json_inline_compat(processing="daemon")

    assert screenshot.data_base64 == "aW1hZ2UtYnl0ZXM="
    assert screenshot.bytes is None
    assert client.calls == [
        {
            "method": "post_json",
            "path": "/v1/screenshots/full",
            "json": {
                "format": "png",
                "quality": 90,
                "scale": 1.0,
                "show_cursor": False,
                "processing": "daemon",
                "storage": "inline",
            },
            "mutation": False,
        }
    ]


@pytest.mark.parametrize("storage", ["artifact", "auto"])
def test_full_non_inline_retains_structured_mutation_request(storage: str) -> None:
    client = _StructuredScreenshotClient()
    namespace = ScreenshotsNamespace(client)  # type: ignore[arg-type]

    screenshot = namespace.full(storage=storage)  # type: ignore[arg-type]

    assert screenshot.artifact_uri == "artifact://screenshots/example.png"
    assert len(client.calls) == 1
    assert client.calls[0]["method"] == "post_json"
    assert client.calls[0]["path"] == "/v1/screenshots/full"
    assert client.calls[0]["json"]["storage"] == storage
    assert client.calls[0]["mutation"] is True


@pytest.mark.asyncio
async def test_async_full_non_inline_retains_structured_mutation_request() -> None:
    client = _AsyncStructuredScreenshotClient()
    namespace = AsyncScreenshotsNamespace(client)  # type: ignore[arg-type]

    screenshot = await namespace.full(storage="artifact")

    assert screenshot.artifact_uri == "artifact://screenshots/example.png"
    assert len(client.calls) == 1
    assert client.calls[0]["method"] == "post_json"
    assert client.calls[0]["path"] == "/v1/screenshots/full"
    assert client.calls[0]["json"]["storage"] == "artifact"
    assert client.calls[0]["mutation"] is True


def test_full_inline_rejects_empty_binary_response_without_json_retry() -> None:
    client = _BinaryScreenshotClient(
        data=b"",
        header_overrides={
            "x-computer-use-size-bytes": "0",
            "x-computer-use-sha256": (
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            ),
        },
    )
    namespace = ScreenshotsNamespace(client)  # type: ignore[arg-type]

    with pytest.raises(FrameValidationError, match="binary screenshot response is empty"):
        namespace.full()

    assert len(client.calls) == 1
    assert client.calls[0]["method"] == "post_bytes_with_headers"


@pytest.mark.parametrize(
    ("header_overrides", "removed_headers"),
    [
        ({"content-type": "text/plain"}, ()),
        ({"content-type": "image/jpeg"}, ()),
        ({}, ("content-type",)),
    ],
)
def test_full_inline_rejects_missing_invalid_or_mismatched_content_type(
    header_overrides: dict[str, str],
    removed_headers: tuple[str, ...],
) -> None:
    secret_body = b"image-bytes"
    client = _BinaryScreenshotClient(
        data=secret_body,
        header_overrides=header_overrides,
        removed_headers=removed_headers,
    )
    namespace = ScreenshotsNamespace(client)  # type: ignore[arg-type]

    with pytest.raises(FrameValidationError) as exc_info:
        namespace.full(format="png")

    message = str(exc_info.value)
    assert message == "binary screenshot response content type is invalid"
    assert secret_body.decode() not in message
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("header_overrides", "removed_headers"),
    [
        ({"x-computer-use-width": "0"}, ()),
        ({"x-computer-use-width": "not-an-integer"}, ()),
        ({"x-computer-use-height": "-1"}, ()),
        ({"x-computer-use-size-bytes": "12"}, ()),
        ({"x-computer-use-size-bytes": "not-an-integer"}, ()),
        ({"x-computer-use-sha256": "not-a-sha256"}, ()),
        ({"x-computer-use-sha256": "0" * 64}, ()),
        ({}, ("x-computer-use-height",)),
    ],
)
def test_full_inline_rejects_invalid_dimensions_size_or_digest_without_leaking_values(
    header_overrides: dict[str, str],
    removed_headers: tuple[str, ...],
) -> None:
    client = _BinaryScreenshotClient(
        header_overrides=header_overrides,
        removed_headers=removed_headers,
    )
    namespace = ScreenshotsNamespace(client)  # type: ignore[arg-type]

    with pytest.raises(FrameValidationError) as exc_info:
        namespace.full()

    assert str(exc_info.value) == "binary screenshot response metadata is invalid"
    assert "image-bytes" not in str(exc_info.value)
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("header_overrides", "removed_headers"),
    [
        ({"x-computer-use-captured-at": "2026-08-08T12:30:00"}, ()),
        ({"x-computer-use-captured-at": "not-a-timestamp"}, ()),
        ({"x-computer-use-coordinate-space": "not-json"}, ()),
        (
            {
                "x-computer-use-coordinate-space": (
                    '{"desktop_width":1024,"desktop_height":768,'
                    '"image_width":1000,"image_height":768}'
                )
            },
            (),
        ),
        ({"x-computer-use-cursor-visible": "TRUE"}, ()),
        ({"x-computer-use-cursor-position": "not-json"}, ()),
        ({"x-computer-use-cursor-position": '{"x":1024,"y":0}'}, ()),
        ({"x-computer-use-timing-ms": "[]"}, ()),
        ({"x-computer-use-timing-ms": '{"capture_ms":-1}'}, ()),
        ({"x-computer-use-capture-backend": " "}, ()),
        ({}, ("x-computer-use-cursor-position",)),
        ({}, ("x-computer-use-capture-backend",)),
    ],
)
def test_full_inline_rejects_invalid_semantic_metadata(
    header_overrides: dict[str, str],
    removed_headers: tuple[str, ...],
) -> None:
    client = _BinaryScreenshotClient(
        header_overrides=header_overrides,
        removed_headers=removed_headers,
    )
    namespace = ScreenshotsNamespace(client)  # type: ignore[arg-type]

    with pytest.raises(FrameValidationError) as exc_info:
        namespace.full()

    assert str(exc_info.value) == "binary screenshot response metadata is invalid"
    assert len(client.calls) == 1


def test_full_inline_rejects_cursor_visibility_that_does_not_match_request() -> None:
    client = _BinaryScreenshotClient(
        header_overrides={"x-computer-use-cursor-visible": "true"},
    )
    namespace = ScreenshotsNamespace(client)  # type: ignore[arg-type]

    with pytest.raises(FrameValidationError) as exc_info:
        namespace.full(show_cursor=False)

    assert str(exc_info.value) == "binary screenshot response metadata is invalid"
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("requested_format", "content_type"),
    [("jpeg", "image/jpeg"), ("webp", "image/webp")],
)
def test_full_inline_accepts_each_supported_matching_image_type(
    requested_format: str,
    content_type: str,
) -> None:
    client = _BinaryScreenshotClient(
        header_overrides={"content-type": content_type},
    )
    namespace = ScreenshotsNamespace(client)  # type: ignore[arg-type]

    screenshot = namespace.full(format=requested_format)  # type: ignore[arg-type]

    assert screenshot.format == requested_format
    assert screenshot.as_bytes() == b"image-bytes"


def test_full_inline_does_not_retry_after_binary_transport_failure() -> None:
    class _FailingClient(_BinaryScreenshotClient):
        def post_bytes_with_headers(self, *args, **kwargs):
            self.calls.append({"method": "post_bytes_with_headers"})
            raise TimeoutError("request outcome is unknown")

    client = _FailingClient()
    namespace = ScreenshotsNamespace(client)  # type: ignore[arg-type]

    with pytest.raises(TimeoutError, match="request outcome is unknown"):
        namespace.full()

    assert client.calls == [{"method": "post_bytes_with_headers"}]
