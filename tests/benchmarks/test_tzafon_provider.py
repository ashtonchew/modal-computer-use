from __future__ import annotations

import base64
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from modal_computer_use.benchmark_comparison import run_provider_comparison
from modal_computer_use.benchmarks.constants import (
    PROVIDER_BENCHMARK_TEXT,
    TYPE_1000_CHARS_TEXT,
)
from modal_computer_use.benchmarks.provider_comparison import tzafon
from modal_computer_use.benchmarks.provider_comparison.verification import TYPE_READBACK_TEXT


def _desktop_png_base64(*, width: int = 1280, height: int = 720) -> str:
    buffer = BytesIO()
    Image.new("RGB", (width, height), color="white").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class FakeExec:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def sync(self, computer_id: str, *, command: str, timeout_seconds: int) -> Any:
        self.calls.append((computer_id, command, timeout_seconds))
        if "printf 42" in command:
            stdout = "42\n"
        elif "getmouselocation" in command or "XQueryPointer" in command:
            stdout = "X=16\nY=128\n"
        elif "keypress_count" in command:
            stdout = f"keypress_count={len(TYPE_READBACK_TEXT)}\n"
        else:
            stdout = "ready=1\n"
        return SimpleNamespace(status="success", result={"exit_code": 0, "stdout": stdout})


class FakeComputers:
    def __init__(self, *, screenshot_data: str | None = None) -> None:
        self.exec = FakeExec()
        self.screenshot_data = screenshot_data or _desktop_png_base64()
        self.create_calls: list[dict[str, Any]] = []
        self.screenshot_calls: list[tuple[str, bool]] = []
        self.click_calls: list[tuple[str, int, int]] = []
        self.batch_calls: list[tuple[str, list[dict[str, Any]]]] = []
        self.type_calls: list[tuple[str, str]] = []
        self.delete_calls: list[str] = []
        self.batch_result: Any = SimpleNamespace(
            status="success",
            result={"executed": 4},
            error_message=None,
        )
        self.click_error: Exception | None = None

    def create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        return SimpleNamespace(id=f"computer-{len(self.create_calls)}")

    def screenshot(self, computer_id: str, *, base64: bool) -> Any:
        self.screenshot_calls.append((computer_id, base64))
        return SimpleNamespace(
            status="success",
            result={
                # The live Python API returns raw base64 in this field when
                # screenshot(base64=True) is requested.
                "screenshot_url": self.screenshot_data,
                "url": "https://secret.invalid/screenshot?token=must-not-leak",
            },
            error_message=None,
        )

    def click(self, computer_id: str, *, x: int, y: int) -> Any:
        self.click_calls.append((computer_id, x, y))
        if self.click_error is not None:
            raise self.click_error
        return SimpleNamespace(status="success", result={})

    def batch(self, computer_id: str, *, actions: list[dict[str, Any]]) -> Any:
        self.batch_calls.append((computer_id, actions))
        return self.batch_result

    def type(self, computer_id: str, *, text: str) -> Any:
        self.type_calls.append((computer_id, text))
        return SimpleNamespace(status="success", result={})

    def delete(self, computer_id: str) -> None:
        self.delete_calls.append(computer_id)


class FakeTzafonModule:
    def __init__(self, computers: FakeComputers | None = None) -> None:
        self.computers = computers or FakeComputers()
        self.client_kwargs: list[dict[str, Any]] = []
        module = self

        class Lightcone:
            def __init__(self, **kwargs: Any) -> None:
                module.client_kwargs.append(kwargs)
                self.computers = module.computers

        self.Lightcone = Lightcone


def _driver(computers: FakeComputers | None = None) -> tuple[Any, FakeComputers]:
    module = FakeTzafonModule(computers)
    return (
        tzafon.TzafonDriver(module, api_key="fake-api-key", base_url=None),
        module.computers,
    )


def test_tzafon_missing_api_key_is_not_measured(monkeypatch) -> None:
    monkeypatch.delenv("TZAFON_API_KEY", raising=False)

    provider = tzafon.run_tzafon_provider(iterations=1, warmup_iterations=0)

    assert provider["status"] == "not_measured"
    assert provider["cases"]["setup"]["reason"] == "TZAFON_API_KEY is not set"


def test_tzafon_missing_sdk_is_unavailable(monkeypatch) -> None:
    monkeypatch.setenv("TZAFON_API_KEY", "fake-api-key")

    def missing(*_args: Any) -> Any:
        raise ImportError("tzafon is missing")

    monkeypatch.setattr(tzafon, "import_provider_module", missing)

    provider = tzafon.run_tzafon_provider(iterations=1, warmup_iterations=0)

    assert provider["status"] == "unavailable"
    assert "bench-tzafon" in provider["cases"]["setup"]["reason"]
    assert "fake-api-key" not in str(provider)


def test_tzafon_happy_path_runs_all_canonical_cases_and_readbacks(monkeypatch) -> None:
    module = FakeTzafonModule()
    monkeypatch.setenv("TZAFON_API_KEY", "fake-api-key")
    monkeypatch.setattr(tzafon, "import_provider_module", lambda *_args: module)

    payload = run_provider_comparison(
        providers=["tzafon"],
        iterations=1,
        warmup_iterations=0,
        mode="provider-live",
    )

    provider = payload["providers"]["tzafon"]
    assert payload["ok"] is True
    assert provider["status"] == "ok"
    assert {
        "product_create_to_first_screenshot",
        "cold_create_to_ready",
        "screenshot_full",
        "move_click",
        "move_click_sequence",
        "type_100_chars",
        "type_1000_chars",
        "command_echo",
    }.issubset(provider["cases"])
    assert all(provider["cases"][case]["status"] == "ok" for case in provider["cases"])
    assert provider["verification"]["cursor_position"]["status"] == "ok"
    assert provider["verification"]["type_text"]["status"] == "ok"
    assert provider["metadata"]["resolution_requested"] == "1024x768"
    assert provider["metadata"]["resolution"] == "1280x720"
    assert provider["metadata"]["requested_resolution_honored"] is False
    assert len(module.computers.create_calls) == 2
    assert module.computers.delete_calls == ["computer-1", "computer-2"]
    assert ("computer-2", PROVIDER_BENCHMARK_TEXT) in module.computers.type_calls
    assert ("computer-2", TYPE_1000_CHARS_TEXT) in module.computers.type_calls
    assert ("computer-2", TYPE_READBACK_TEXT) in module.computers.type_calls


def test_tzafon_creates_nonpersistent_1024x768_desktop() -> None:
    driver, computers = _driver()

    computer = driver.create_lifecycle_session()

    assert computer == "computer-1"
    assert computers.create_calls == [
        {
            "kind": "desktop",
            "display": {"width": 1024, "height": 768},
            "persistent": False,
        }
    ]


def test_tzafon_screenshot_decodes_inline_base64_without_returning_payload() -> None:
    driver, computers = _driver()

    result = driver.screenshot_full("computer-1")

    assert computers.screenshot_calls == [("computer-1", True)]
    assert result["size_bytes"] > 0
    assert result["payload"]["transport_encoding"] == "base64_string"
    assert result["payload"]["width"] == 1280
    assert result["payload"]["height"] == 720
    assert result["payload"]["format"] == "png"
    assert computers.screenshot_data not in str(result)
    assert "secret.invalid" not in str(result)
    assert "must-not-leak" not in str(result)


def test_tzafon_screenshot_rejects_non_inline_screenshot_url() -> None:
    driver, _ = _driver(
        FakeComputers(screenshot_data="https://secret.invalid/screenshot?token=must-not-leak")
    )

    with pytest.raises(RuntimeError, match="valid inline image bytes"):
        driver.screenshot_full("computer-1")


def test_tzafon_coordinate_click_records_semantic_and_provider_counts() -> None:
    driver, computers = _driver()

    result = driver.move_click("computer-1")

    assert computers.click_calls == [("computer-1", 24, 24)]
    assert result["action_count"] == 1
    assert result["logical_action_count"] == 2
    assert result["provider_action_count"] == 1
    assert result["request_count"] == 1
    assert result["semantic_equivalent"] == "coordinate_click_without_standalone_move"


@pytest.mark.parametrize(
    "status",
    [None, 7, "COMMAND_NOT_SUPPORTED", "FAILED", "UNKNOWN_ACTION"],
)
def test_tzafon_action_requires_explicit_success_status(status: Any) -> None:
    with pytest.raises(RuntimeError, match="action failed"):
        tzafon._ensure_action_succeeded(
            SimpleNamespace(status=status, result={}, error_message=None)
        )


def test_tzafon_four_click_sequence_uses_one_native_batch() -> None:
    driver, computers = _driver()

    result = driver.move_click_sequence("computer-1")

    assert len(computers.batch_calls) == 1
    computer_id, actions = computers.batch_calls[0]
    assert computer_id == "computer-1"
    assert actions == [
        {"type": "click", "x": 16, "y": 16},
        {"type": "click", "x": 128, "y": 16},
        {"type": "click", "x": 128, "y": 128},
        {"type": "click", "x": 16, "y": 128},
    ]
    assert result["action_count"] == 4
    assert result["logical_action_count"] == 8
    assert result["provider_action_count"] == 4
    assert result["request_count"] == 1
    assert result["native_batch"] is True


@pytest.mark.parametrize(
    "batch_result",
    [
        {},
        {"status": "success"},
        SimpleNamespace(status="success", result={}, error_message=None),
        SimpleNamespace(status="TIMEOUT", result={"executed": 4}, error_message=None),
        SimpleNamespace(status="failed", result={"executed": 0}, error_message="stopped"),
        SimpleNamespace(status="success", result={"executed": 3}, error_message=None),
        SimpleNamespace(
            status="success",
            result={
                "executed": 4,
                "results": [
                    {"status": "success"},
                    {"status": "success"},
                    {"status": "failed", "error_message": "stopped"},
                    {"status": "success"},
                ],
            },
            error_message=None,
        ),
    ],
)
def test_tzafon_batch_rejects_failed_or_partial_execution(batch_result: Any) -> None:
    driver, computers = _driver()
    computers.batch_result = batch_result

    with pytest.raises(RuntimeError):
        driver.move_click_sequence("computer-1")


def test_tzafon_command_echo_uses_exec_sync() -> None:
    driver, computers = _driver()

    assert driver.command_echo("computer-1") == {"exit_code": 0}
    assert computers.exec.calls == [
        ("computer-1", "sh -lc 'printf 42'", 30),
    ]


def test_tzafon_cleanup_runs_after_normal_and_failing_setup(monkeypatch) -> None:
    healthy = FakeTzafonModule()
    monkeypatch.setenv("TZAFON_API_KEY", "fake-api-key")
    monkeypatch.setattr(tzafon, "import_provider_module", lambda *_args: healthy)
    tzafon.run_tzafon_provider(iterations=1, warmup_iterations=0)
    assert healthy.computers.delete_calls == ["computer-1", "computer-2"]

    broken_computers = FakeComputers(screenshot_data="not-valid-base64")
    broken = FakeTzafonModule(broken_computers)
    monkeypatch.setattr(tzafon, "import_provider_module", lambda *_args: broken)
    monkeypatch.setattr(
        tzafon,
        "wait_for_provider_screenshot_ready",
        lambda operation, computer: operation(computer),
    )
    provider = tzafon.run_tzafon_provider(iterations=1, warmup_iterations=0)

    assert provider["status"] == "failed"
    assert broken_computers.delete_calls == ["computer-1", "computer-2"]


def test_tzafon_errors_redact_benchmark_text_and_credentials(monkeypatch) -> None:
    computers = FakeComputers()
    computers.click_error = RuntimeError(
        f"failed while typing {PROVIDER_BENCHMARK_TEXT}; api_key=fake-api-key"
    )
    module = FakeTzafonModule(computers)
    monkeypatch.setenv("TZAFON_API_KEY", "fake-api-key")
    monkeypatch.setattr(tzafon, "import_provider_module", lambda *_args: module)

    provider = tzafon.run_tzafon_provider(iterations=1, warmup_iterations=0)
    serialized = str(provider)

    assert provider["status"] == "failed"
    assert PROVIDER_BENCHMARK_TEXT not in serialized
    assert "fake-api-key" not in serialized
