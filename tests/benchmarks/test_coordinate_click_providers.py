from types import SimpleNamespace

from modal_computer_use.benchmarks.provider_comparison.daytona import DaytonaDriver
from modal_computer_use.benchmarks.provider_comparison.e2b import E2BDriver


class RecordingDaytonaMouse:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []
        self.moves: list[tuple[int, int]] = []

    def click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))

    def move(self, x: int, y: int) -> None:
        self.moves.append((x, y))


class RecordingE2BSandbox:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []
        self.moves: list[tuple[int, int]] = []

    def left_click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))

    def move_mouse(self, x: int, y: int) -> None:
        self.moves.append((x, y))


def test_daytona_coordinate_cases_use_only_native_coordinate_clicks() -> None:
    mouse = RecordingDaytonaMouse()
    sandbox = SimpleNamespace(computer_use=SimpleNamespace(mouse=mouse))
    driver = object.__new__(DaytonaDriver)
    driver._coordinate_click_index = 0

    single = driver.coordinate_click(sandbox)
    sequence = driver.coordinate_click_sequence(sandbox)

    assert mouse.moves == []
    assert mouse.clicks == [(24, 24), (16, 16), (128, 16), (128, 128), (16, 128)]
    assert single["logical_action_count"] == single["provider_action_count"] == 1
    assert single["benchmark_semantics"] == "coordinate-click-v1"
    assert single["provider_sdk_call_count"] == single["transport_request_count"] == 1
    assert sequence["logical_action_count"] == sequence["provider_action_count"] == 4
    assert sequence["benchmark_semantics"] == "coordinate-click-v1"
    assert sequence["provider_sdk_call_count"] == sequence["transport_request_count"] == 4
    assert sequence["native_batch"] is False
    assert sequence["batching"] == "sequential_requests"


def test_e2b_coordinate_cases_use_coordinate_overload_without_harness_move() -> None:
    sandbox = RecordingE2BSandbox()
    driver = object.__new__(E2BDriver)
    driver._coordinate_click_index = 0

    single = driver.coordinate_click(sandbox)
    sequence = driver.coordinate_click_sequence(sandbox)

    assert sandbox.moves == []
    assert sandbox.clicks == [(24, 24), (16, 16), (128, 16), (128, 128), (16, 128)]
    assert single["logical_action_count"] == single["provider_action_count"] == 1
    assert single["benchmark_semantics"] == "coordinate-click-v1"
    assert single["provider_sdk_call_count"] == 1
    assert single["transport_request_count"] == 2
    assert single["request_count_source"] == "pinned_sdk_implementation"
    assert sequence["logical_action_count"] == sequence["provider_action_count"] == 4
    assert sequence["benchmark_semantics"] == "coordinate-click-v1"
    assert sequence["provider_sdk_call_count"] == 4
    assert sequence["transport_request_count"] == 8
    assert sequence["native_batch"] is False
    assert sequence["batching"] == "sequential_requests"


def test_action_frame_drivers_dispatch_the_shared_single_click_before_screenshot() -> None:
    daytona_mouse = RecordingDaytonaMouse()
    daytona_sandbox = SimpleNamespace(computer_use=SimpleNamespace(mouse=daytona_mouse))
    daytona = object.__new__(DaytonaDriver)
    daytona.screenshot_full = lambda _resource: {  # type: ignore[method-assign]
        "payload": {
            "format": "png",
            "width": 1024,
            "height": 768,
            "decoded_size_bytes": 10,
        }
    }
    daytona_result = daytona.action_to_immediate_frame(daytona_sandbox)

    e2b_sandbox = RecordingE2BSandbox()
    e2b = object.__new__(E2BDriver)
    e2b.screenshot_full = lambda _resource: {  # type: ignore[method-assign]
        "payload": {
            "format": "jpeg",
            "width": 1280,
            "height": 720,
            "decoded_size_bytes": 11,
        }
    }
    e2b_result = e2b.action_to_immediate_frame(e2b_sandbox)

    assert daytona_mouse.clicks == [(512, 384)]
    assert e2b_sandbox.clicks == [(512, 384)]
    assert daytona_result["actions"]["logical_action_count"] == 1
    assert e2b_result["actions"]["logical_action_count"] == 1
    assert daytona_result["screenshot"]["format"] == "png"
    assert e2b_result["screenshot"]["format"] == "jpeg"
