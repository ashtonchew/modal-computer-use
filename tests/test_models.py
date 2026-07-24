from __future__ import annotations

import pytest
from pydantic import ValidationError

from modal_computer_use import ComputerConfig, CoordinateSpace, Point, Region
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


def test_runtime_modal_region_rejects_empty_string() -> None:
    with pytest.raises(ValidationError, match="modal_region must be non-empty"):
        ComputerConfig(runtime={"modal_region": "   "})


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


def test_action_union_validation() -> None:
    action = parse_action({"type": "click", "x": 1, "y": 2, "button": "left"})
    assert action.type == "click"
    with pytest.raises(ActionValidationError):
        parse_action({"type": "drag"})


def test_mouse_up_action_is_not_mouse_down_action() -> None:
    action = parse_action({"type": "mouse_up", "button": "left"})

    assert isinstance(action, MouseUpAction)
    assert not isinstance(action, MouseDownAction)
