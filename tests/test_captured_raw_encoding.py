from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO

import pytest
from PIL import Image

from modal_computer_use.daemon.desktop import screenshots as screenshots_module
from modal_computer_use.daemon.desktop.screenshots import (
    CapturedRawScreenshot,
    try_encode_captured_raw,
)
from modal_computer_use.models import CoordinateSpace, Region, ScreenshotOptions, sha256_bytes

CAPTURED_AT = datetime(2026, 7, 21, tzinfo=UTC)
RGB = bytes((255, 0, 0, 0, 255, 0))


def _raw_screenshot(*, cursor_visible: bool = False) -> CapturedRawScreenshot:
    return CapturedRawScreenshot(
        width=2,
        height=1,
        rgb=RGB,
        sha256=sha256_bytes(RGB),
        captured_at=CAPTURED_AT,
        coordinate_space=CoordinateSpace.from_dimensions(
            desktop_width=2,
            desktop_height=1,
        ),
        cursor_visible=cursor_visible,
        capture_backend="mss-raw",
    )


def test_encode_captured_raw_png_uses_the_confirmed_pixels() -> None:
    raw = _raw_screenshot()

    result = try_encode_captured_raw(
        raw,
        ScreenshotOptions(format="png"),
        output_region=None,
    )

    assert result is not None
    with Image.open(BytesIO(result.data)) as image:
        rgb_image = image.convert("RGB")
        assert [rgb_image.getpixel((x, 0)) for x in range(2)] == [
            (255, 0, 0),
            (0, 255, 0),
        ]
    assert result.format == "png"
    assert result.width == 2
    assert result.height == 1
    assert result.sha256 == sha256_bytes(result.data)
    assert result.captured_at == CAPTURED_AT
    assert result.coordinate_space == raw.coordinate_space
    assert result.cursor_visible is False
    assert result.capture_backend == "mss-raw"


def test_encode_captured_raw_returns_none_for_cursor_incompatibility() -> None:
    options = ScreenshotOptions(format="png", show_cursor=True)

    assert try_encode_captured_raw(_raw_screenshot(), options, output_region=None) is None
    assert (
        try_encode_captured_raw(
            _raw_screenshot(cursor_visible=True),
            ScreenshotOptions(format="png", show_cursor=False),
            output_region=None,
        )
        is None
    )


def test_encode_captured_raw_returns_none_for_source_region_mismatch() -> None:
    source_region = Region(x=1, y=1, width=2, height=1)
    raw = CapturedRawScreenshot(
        width=2,
        height=1,
        rgb=RGB,
        sha256=sha256_bytes(RGB),
        captured_at=CAPTURED_AT,
        coordinate_space=CoordinateSpace.from_dimensions(
            desktop_width=4,
            desktop_height=3,
            source_region=source_region,
        ),
        cursor_visible=False,
    )

    assert try_encode_captured_raw(raw, ScreenshotOptions(), output_region=None) is None
    assert (
        try_encode_captured_raw(
            raw,
            ScreenshotOptions(),
            output_region=Region(x=0, y=0, width=2, height=1),
        )
        is None
    )
    result = try_encode_captured_raw(raw, ScreenshotOptions(), output_region=source_region)
    assert result is not None
    assert result.coordinate_space.source_region == source_region
    assert result.coordinate_space.desktop_width == 4
    assert result.coordinate_space.desktop_height == 3


def test_encode_captured_raw_returns_none_without_a_raw_frame() -> None:
    assert try_encode_captured_raw(None, ScreenshotOptions(), output_region=None) is None


@pytest.mark.parametrize(
    ("image_format", "pillow_format"),
    [("png", "PNG"), ("jpeg", "JPEG"), ("webp", "WEBP")],
)
def test_encode_captured_raw_supports_formats_and_scale(
    image_format: str,
    pillow_format: str,
) -> None:
    result = try_encode_captured_raw(
        _raw_screenshot(),
        ScreenshotOptions.model_validate(
            {"format": image_format, "quality": 80, "scale": 0.5}
        ),
        output_region=None,
    )

    assert result is not None
    assert result.format == image_format
    assert (result.width, result.height) == (1, 1)
    assert result.coordinate_space.desktop_width == 2
    assert result.coordinate_space.desktop_height == 1
    assert result.coordinate_space.source_region is None
    assert result.coordinate_space.scale_x == 0.5
    assert result.coordinate_space.scale_y == 1.0
    with Image.open(BytesIO(result.data)) as image:
        assert image.format == pillow_format
        assert image.size == (1, 1)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (replace(_raw_screenshot(), width=0), "dimensions must be positive"),
        (replace(_raw_screenshot(), width=1, rgb=RGB[:3]), "coordinate space"),
        (replace(_raw_screenshot(), rgb=RGB[:-1]), "RGB byte length"),
    ],
)
def test_encode_captured_raw_rejects_invalid_raw_frames(
    raw: CapturedRawScreenshot,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        try_encode_captured_raw(raw, ScreenshotOptions(), output_region=None)


def test_encode_captured_raw_propagates_encoder_errors(monkeypatch) -> None:
    def fail_encoder(_rgb: bytes, _size: tuple[int, int]) -> bytes:
        raise RuntimeError("encoder failed")

    monkeypatch.setattr(screenshots_module, "encode_rgb_png", fail_encoder)

    with pytest.raises(RuntimeError, match="encoder failed"):
        try_encode_captured_raw(
            _raw_screenshot(),
            ScreenshotOptions(format="png"),
            output_region=None,
        )
