from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from modal_computer_use import ComputerConfig, CoordinateSpace, Point, Region, Screenshot
from modal_computer_use.errors import ActionValidationError
from modal_computer_use.models import (
    MouseDownAction,
    MouseUpAction,
    ScreenshotOptions,
    parse_action,
)


def test_config_aliases_and_vnc_normalization() -> None:
    config = ComputerConfig(resources={"memory_mb": 1024}, expose_vnc=True)
    assert config.resources.memory_mib == 1024
    assert config.expose_vnc == "control"
    assert config.desktop.resolution == (1024, 768)
    assert config.desktop.dpi == 96
    assert config.actions.post_action_delay_ms == 0


def test_vnc_password_is_internal_only() -> None:
    value = "vnc-secret"
    config = ComputerConfig(vnc_password=value)

    assert config.vnc_password == value
    assert value not in repr(config)
    assert "vnc_password" not in config.model_dump()
    assert value not in config.model_dump_json()


def test_runtime_modal_region_rejects_empty_string() -> None:
    with pytest.raises(ValidationError, match="modal_region must be non-empty"):
        ComputerConfig(runtime={"modal_region": "   "})


def test_runtime_modal_environment_is_distinct_and_rejects_empty_string() -> None:
    config = ComputerConfig(
        runtime={"modal_environment": "runtime-prod"},
        image={
            "source": "named",
            "revision": "a" * 40,
            "environment_name": "image-prod",
        },
    )
    assert config.runtime.modal_environment == "runtime-prod"
    assert config.image.environment_name == "image-prod"

    with pytest.raises(ValidationError, match="modal_environment must be non-empty"):
        ComputerConfig(runtime={"modal_environment": "   "})


def test_action_subprocess_backend_defaults_and_validates() -> None:
    assert ComputerConfig().actions.subprocess_backend == "isolated-asyncio"
    assert (
        ComputerConfig(actions={"subprocess_backend": "threaded"}).actions.subprocess_backend
        == "threaded"
    )
    assert (
        ComputerConfig(
            actions={"subprocess_backend": "isolated-asyncio"}
        ).actions.subprocess_backend
        == "isolated-asyncio"
    )

    with pytest.raises(ValidationError):
        ComputerConfig(actions={"subprocess_backend": "process-pool"})


def test_screenshot_capture_source_defaults_to_auto_and_validates() -> None:
    assert ComputerConfig().actions.screenshot_capture_source == "auto"
    assert (
        ComputerConfig(actions={"screenshot_capture_source": "x11-shm"})
        .actions.screenshot_capture_source
        == "x11-shm"
    )

    with pytest.raises(ValidationError):
        ComputerConfig(actions={"screenshot_capture_source": "native-xcb-fixed-up"})


def test_region_validation() -> None:
    with pytest.raises(ValidationError):
        Region(x=0, y=0, width=0, height=10)


def test_coordinate_space_transforms_region() -> None:
    space = CoordinateSpace(
        desktop_width=1000,
        desktop_height=800,
        image_width=250,
        image_height=200,
        scale_x=0.5,
        scale_y=0.5,
        source_region=Region(x=100, y=200, width=500, height=400),
    )
    assert space.to_desktop(Point(x=10, y=20)) == Point(x=120, y=240)
    assert space.to_image(Point(x=120, y=240)) == Point(x=10, y=20)


def test_screenshot_options_validation() -> None:
    with pytest.raises(ValidationError):
        ScreenshotOptions(format="bmp")


@pytest.mark.parametrize(
    ("format", "image_bytes", "base64_value", "json_base64_value"),
    [
        ("png", b"\x89PNG\r\n\x1a\n\x00\xff", "iVBORw0KGgoA/w==", "iVBORw0KGgoA_w=="),
        ("jpeg", b"\xff\xd8\xff\xe0\x00\x10JFIF", "/9j/4AAQSkZJRg==", "_9j_4AAQSkZJRg=="),
        ("webp", b"RIFF\xff\x00\xfe\x80WEBP", "UklGRv8A/oBXRUJQ", "UklGRv8A_oBXRUJQ"),
    ],
)
def test_byte_backed_screenshot_has_stable_helpers_and_json_round_trip(
    format: str,
    image_bytes: bytes,
    base64_value: str,
    json_base64_value: str,
) -> None:
    screenshot = Screenshot(
        format=format,
        width=2,
        height=1,
        size_bytes=len(image_bytes),
        bytes=image_bytes,
        coordinate_space=CoordinateSpace.from_dimensions(
            desktop_width=2,
            desktop_height=1,
        ),
    )

    assert screenshot.bytes is image_bytes
    assert screenshot.data_base64 is None
    assert screenshot.as_bytes() is image_bytes
    assert screenshot.to_base64() == base64_value
    assert screenshot.model_dump(mode="python")["bytes"] is image_bytes

    serialized = screenshot.model_dump_json()

    assert json.loads(serialized)["bytes"] == json_base64_value
    restored = Screenshot.model_validate_json(serialized)
    assert restored == screenshot
    assert restored.as_bytes() == image_bytes
    assert restored.to_base64() == base64_value


def test_byte_backed_screenshot_validation_error_does_not_expose_payload() -> None:
    payload_canary = "SECRET_SCREENSHOT_BYTES_CANARY!"
    serialized = json.dumps(
        {
            "format": "png",
            "width": 1,
            "height": 1,
            "size_bytes": 1,
            "bytes": payload_canary,
            "coordinate_space": {
                "desktop_width": 1,
                "desktop_height": 1,
                "image_width": 1,
                "image_height": 1,
            },
        }
    )

    with pytest.raises(ValidationError) as exc_info:
        Screenshot.model_validate_json(serialized)

    assert payload_canary not in str(exc_info.value)
    assert payload_canary not in repr(exc_info.value)


def test_action_union_validation() -> None:
    action = parse_action({"type": "click", "x": 1, "y": 2, "button": "left"})
    assert action.type == "click"
    with pytest.raises(ActionValidationError):
        parse_action({"type": "drag"})


def test_mouse_up_action_is_not_mouse_down_action() -> None:
    action = parse_action({"type": "mouse_up", "button": "left"})

    assert isinstance(action, MouseUpAction)
    assert not isinstance(action, MouseDownAction)
