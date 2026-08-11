from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from typing import Any

from ..constants import (
    COMMAND_ECHO_COMMAND,
    COMMAND_ECHO_STDOUT,
    COMMAND_NONLOGIN_SHELL_ECHO_BENCHMARK_SEMANTICS,
    COMMAND_NONLOGIN_SHELL_ECHO_COMMAND,
    COORDINATE_CLICK_BENCHMARK_SEMANTICS,
    COORDINATE_CLICK_SEQUENCE_ACTIONS,
    MOVE_CLICK_SEQUENCE_ACTIONS,
    PROVIDER_BENCHMARK_TEXT,
    TYPE_1000_CHARS_TEXT,
    coordinate_click_target,
)
from ..lifecycle import CleanupError
from ..safety import _safe_url_origin
from .action_frame import (
    ACTION_FRAME_CASE_ID,
    ACTION_FRAME_POINT,
    ACTION_FRAME_PROVIDER_TOPOLOGY,
)
from .live import run_product_provider_cases, wait_for_provider_screenshot_ready
from .payloads import describe_screenshot_payload, validated_screenshot_size
from .provider_sdk import (
    import_provider_module,
    package_version,
    provider_exit_code,
    provider_stdout,
)
from .results import provider_not_measured, provider_unavailable
from .verification import (
    TYPE_READBACK_FOCUS_X,
    TYPE_READBACK_FOCUS_Y,
    TYPE_READBACK_TEXT,
    verification_step,
    verify_provider_cursor_position,
    verify_provider_type_readback,
)

_DISPLAY_WIDTH = 1024
_DISPLAY_HEIGHT = 768
_DEFAULT_MAX_RETRIES = 2
_SUCCESS_ACTION_STATUSES = frozenset({"completed", "ok", "success"})
_INLINE_SCREENSHOT_KEYS = (
    "screenshot_url",
    "screenshot_base64",
    "image_base64",
    "data_base64",
    "image_data",
    "base64",
    "data",
)
_X11_CURSOR_READBACK_SCRIPT = (
    "import ctypes, os; "
    "lib = ctypes.CDLL('libX11.so.6'); "
    "lib.XOpenDisplay.argtypes = [ctypes.c_char_p]; "
    "lib.XOpenDisplay.restype = ctypes.c_void_p; "
    "display = lib.XOpenDisplay(os.environ.get('DISPLAY', ':0').encode()); "
    "root_result = ctypes.c_ulong(); child_result = ctypes.c_ulong(); "
    "root_x = ctypes.c_int(); root_y = ctypes.c_int(); "
    "window_x = ctypes.c_int(); window_y = ctypes.c_int(); mask = ctypes.c_uint(); "
    "lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]; "
    "lib.XDefaultRootWindow.restype = ctypes.c_ulong; "
    "root = lib.XDefaultRootWindow(display); "
    "lib.XQueryPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong, "
    "ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong), "
    "ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), "
    "ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), "
    "ctypes.POINTER(ctypes.c_uint)]; "
    "lib.XQueryPointer.restype = ctypes.c_int; "
    "ok = lib.XQueryPointer(display, root, ctypes.byref(root_result), "
    "ctypes.byref(child_result), ctypes.byref(root_x), ctypes.byref(root_y), "
    "ctypes.byref(window_x), ctypes.byref(window_y), ctypes.byref(mask)); "
    "print(f'X={root_x.value}\\nY={root_y.value}'); "
    "lib.XCloseDisplay.argtypes = [ctypes.c_void_p]; "
    "lib.XCloseDisplay(display); "
    "raise SystemExit(0 if ok else 3)"
)


def run_tzafon_provider(
    *, iterations: int, warmup_iterations: int, benchmark_case: str = "all"
) -> dict[str, Any]:
    provider = "tzafon"
    api_key = os.environ.get("TZAFON_API_KEY")
    if not api_key:
        return provider_not_measured(provider, "TZAFON_API_KEY is not set")
    try:
        tzafon_module = import_provider_module("tzafon", "Lightcone")
    except ImportError:
        return provider_unavailable(
            provider,
            "install the bench-tzafon extra to run Tzafon benchmarks",
        )

    base_url = os.environ.get("LIGHTCONE_BASE_URL")
    metadata = {
        "sdk_package": "tzafon",
        "sdk_version": package_version("tzafon"),
        "sdk_max_retries": _DEFAULT_MAX_RETRIES,
        "sdk_retry_policy": "provider_default",
        "api_origin": _safe_url_origin(base_url),
        "computer_kind": "desktop",
        "resolution_requested": f"{_DISPLAY_WIDTH}x{_DISPLAY_HEIGHT}",
        "persistent": False,
        "model_api_calls": False,
        "startup_model": "managed_desktop",
        "uses_snapshot_or_template": False,
        "readiness_contract": (
            "computers.create -> first valid inline screenshot decoded by the caller"
        ),
        "setup_included": True,
        "ingress_included": False,
        "first_observation_api": "computers.screenshot(base64=True)",
        "target_kind": "product",
        "topology": dict(ACTION_FRAME_PROVIDER_TOPOLOGY),
        "action_equivalence": {
            "move_click": (
                "one coordinate click; Tzafon does not expose a standalone pointer-move action"
            ),
            "move_click_sequence": (
                "four coordinate clicks submitted as one native batch request"
            ),
        },
    }
    driver = TzafonDriver(tzafon_module, api_key=api_key, base_url=base_url)
    return run_product_provider_cases(
        provider=provider,
        driver=driver,
        cold_cases=("cold_create_to_ready",),
        warm_cases=(
            "screenshot_full",
            "move_click",
            "move_click_sequence",
            "coordinate_click",
            "coordinate_click_sequence",
            "type_100_chars",
            "type_1000_chars",
            "command_echo",
            "command_nonlogin_shell_echo",
        ),
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        metadata=metadata,
        benchmark_case=benchmark_case,
    )


class TzafonDriver:
    def __init__(
        self,
        tzafon_module: Any,
        *,
        api_key: str,
        base_url: str | None,
    ) -> None:
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = tzafon_module.Lightcone(**client_kwargs)
        self._observed_resolution: str | None = None
        self._observed_screenshot_format: str | None = None
        self._coordinate_click_index = 0

    def create_lifecycle_session(self) -> Any:
        computer = self._client.computers.create(
            kind="desktop",
            display={"width": _DISPLAY_WIDTH, "height": _DISPLAY_HEIGHT},
            persistent=False,
        )
        return _computer_id(computer)

    def observe_first_screenshot(self, computer: Any) -> dict[str, Any]:
        wait_for_provider_screenshot_ready(self.screenshot_full, computer)
        return {"status": "ready", "resolution": self._observed_resolution}

    def cleanup_session(self, computer: Any) -> list[CleanupError]:
        try:
            self._client.computers.delete(_computer_id(computer))
        except Exception as exc:
            return [("computers.delete", exc)]
        return []

    def screenshot_full(self, computer: Any) -> dict[str, Any]:
        result = self._client.computers.screenshot(_computer_id(computer), base64=True)
        _ensure_action_succeeded(result)
        inline_payload = _inline_screenshot_payload(result)
        payload = describe_screenshot_payload(inline_payload)
        size_bytes = validated_screenshot_size(payload, provider="Tzafon")
        width = payload.get("width")
        height = payload.get("height")
        if not isinstance(width, int) or width <= 0 or not isinstance(height, int) or height <= 0:
            raise RuntimeError("Tzafon screenshot did not contain valid image dimensions")
        self._observed_resolution = f"{width}x{height}"
        screenshot_format = payload.get("format")
        self._observed_screenshot_format = (
            screenshot_format if isinstance(screenshot_format, str) else None
        )
        return {"size_bytes": size_bytes, "payload": payload}

    def resource_metadata(self, computer: Any) -> dict[str, Any]:
        del computer
        if self._observed_resolution is None:
            return {}
        requested_resolution = f"{_DISPLAY_WIDTH}x{_DISPLAY_HEIGHT}"
        return {
            "resolution": self._observed_resolution,
            "resolution_source": "first_validated_screenshot",
            "requested_resolution_honored": self._observed_resolution == requested_resolution,
            "screenshot_format": self._observed_screenshot_format,
        }

    def move_click(self, computer: Any) -> dict[str, Any]:
        result = self._client.computers.click(_computer_id(computer), x=24, y=24)
        _ensure_action_succeeded(result)
        return {
            "action_count": 1,
            "logical_action_count": 2,
            "provider_action_count": 1,
            "request_count": 1,
            "semantic": "coordinate_click",
            "semantic_equivalent": "coordinate_click_without_standalone_move",
        }

    def move_click_sequence(self, computer: Any) -> dict[str, Any]:
        actions = [
            {"type": "click", "x": action["x"], "y": action["y"]}
            for action in MOVE_CLICK_SEQUENCE_ACTIONS
            if action["type"] == "click"
        ]
        result = self._client.computers.batch(_computer_id(computer), actions=actions)
        _ensure_batch_succeeded(result, expected_actions=len(actions))
        return {
            "action_count": len(actions),
            "logical_action_count": len(MOVE_CLICK_SEQUENCE_ACTIONS),
            "provider_action_count": len(actions),
            "request_count": 1,
            "native_batch": True,
            "semantic": "coordinate_click_sequence",
        }

    def coordinate_click(self, computer: Any) -> dict[str, Any]:
        x, y = coordinate_click_target(self._coordinate_click_index)
        self._coordinate_click_index += 1
        result = self._client.computers.click(_computer_id(computer), x=x, y=y)
        _ensure_action_succeeded(result)
        return {
            "semantic": "coordinate_click",
            "benchmark_semantics": COORDINATE_CLICK_BENCHMARK_SEMANTICS,
            "logical_action_count": 1,
            "provider_action_count": 1,
            "provider_sdk_call_count": 1,
            "transport_request_count": 1,
            "request_count_source": "harness_direct",
            "native_batch": False,
            "batching": "single_request",
        }

    def coordinate_click_sequence(self, computer: Any) -> dict[str, Any]:
        actions = [
            {"type": "click", "x": action["x"], "y": action["y"]}
            for action in COORDINATE_CLICK_SEQUENCE_ACTIONS
        ]
        result = self._client.computers.batch(_computer_id(computer), actions=actions)
        _ensure_batch_succeeded(result, expected_actions=len(actions))
        return {
            "semantic": "coordinate_click_sequence",
            "benchmark_semantics": COORDINATE_CLICK_BENCHMARK_SEMANTICS,
            "logical_action_count": len(actions),
            "provider_action_count": len(actions),
            "provider_sdk_call_count": 1,
            "transport_request_count": 1,
            "request_count_source": "harness_direct",
            "native_batch": True,
            "batching": "single_request",
        }

    def action_to_immediate_frame(self, computer: Any) -> dict[str, Any]:
        result = self._client.computers.click(_computer_id(computer), x=512, y=384)
        _ensure_action_succeeded(result)
        actions = {
            "semantic": "coordinate_click",
            "benchmark_semantics": "one-left-click-at-512-384-v1",
            "logical_action_count": 1,
            "provider_action_count": 1,
            "provider_sdk_call_count": 1,
            "transport_request_count": 1,
            "request_count_source": "harness_direct",
            "native_batch": False,
            "batching": "single_request",
        }
        screenshot = self.screenshot_full(computer)
        return {
            "path": "provider-sdk-action-then-screenshot",
            "actions": {"case_id": ACTION_FRAME_CASE_ID, **actions},
            "screenshot": {**screenshot["payload"], "show_cursor": None},
        }

    def type_100_chars(self, computer: Any) -> dict[str, Any]:
        return self._type_text(computer, PROVIDER_BENCHMARK_TEXT)

    def type_1000_chars(self, computer: Any) -> dict[str, Any]:
        return self._type_text(computer, TYPE_1000_CHARS_TEXT)

    def command_echo(self, computer: Any) -> dict[str, Any]:
        result = self._exec(
            computer,
            shlex.join(COMMAND_ECHO_COMMAND),
            timeout=30,
        )
        if provider_stdout(result) != COMMAND_ECHO_STDOUT:
            raise RuntimeError("Tzafon command output did not match the expected sentinel")
        return {"exit_code": provider_exit_code(result)}

    def command_nonlogin_shell_echo(self, computer: Any) -> dict[str, Any]:
        result = self._exec(
            computer,
            shlex.join(COMMAND_NONLOGIN_SHELL_ECHO_COMMAND),
            timeout=30,
        )
        if provider_stdout(result) != COMMAND_ECHO_STDOUT:
            raise RuntimeError("Tzafon command output did not match the expected sentinel")
        return {
            "exit_code": provider_exit_code(result),
            "benchmark_semantics": COMMAND_NONLOGIN_SHELL_ECHO_BENCHMARK_SEMANTICS,
            "shell_mode": "non_login",
            "command": {
                "argv": list(COMMAND_NONLOGIN_SHELL_ECHO_COMMAND),
                "timeout_seconds": 30,
                "transport_shape": "command_string",
            },
        }

    def verify_readbacks(self, computer: Any) -> dict[str, Any]:
        def run_command(command: str, timeout: int) -> str:
            return provider_stdout(self._exec(computer, command, timeout=timeout))

        def focus_target() -> None:
            result = self._client.computers.click(
                _computer_id(computer),
                x=TYPE_READBACK_FOCUS_X,
                y=TYPE_READBACK_FOCUS_Y,
            )
            _ensure_action_succeeded(result)

        def type_text(text: str) -> Any:
            result = self._client.computers.type(_computer_id(computer), text=text)
            _ensure_action_succeeded(result)
            return result

        return {
            "cursor_position": verification_step(
                lambda: verify_provider_cursor_position(
                    run_command,
                    query_command=_x11_cursor_readback_command(),
                ),
                redacted_text=None,
            ),
            "type_text": verification_step(
                lambda: verify_provider_type_readback(
                    type_text=type_text,
                    focus_target=focus_target,
                    run_command=run_command,
                ),
                redacted_text=TYPE_READBACK_TEXT,
            ),
        }

    def verify_action_frame_readback(self, computer: Any) -> dict[str, Any]:
        def run_command(command: str, timeout: int) -> str:
            return provider_stdout(self._exec(computer, command, timeout=timeout))

        return {
            "cursor_position": verification_step(
                lambda: verify_provider_cursor_position(
                    run_command,
                    query_command=_x11_cursor_readback_command(),
                    expected=ACTION_FRAME_POINT,
                ),
                redacted_text=None,
            )
        }

    def _type_text(self, computer: Any, text: str) -> dict[str, Any]:
        result = self._client.computers.type(_computer_id(computer), text=text)
        _ensure_action_succeeded(result)
        return {"character_count": len(text), "method": "provider_default"}

    def _exec(self, computer: Any, command: str, *, timeout: int) -> Any:
        result = self._client.computers.exec.sync(
            _computer_id(computer),
            command=command,
            timeout_seconds=timeout,
        )
        exit_code = provider_exit_code(result)
        if exit_code not in (None, 0):
            raise RuntimeError("Tzafon command exited nonzero")
        return result


def _computer_id(computer: Any) -> str:
    if isinstance(computer, str) and computer:
        return computer
    value = computer.get("id") if isinstance(computer, Mapping) else getattr(computer, "id", None)
    if not isinstance(value, str) or not value:
        raise RuntimeError("Tzafon create response did not include a computer id")
    return value


def _ensure_action_succeeded(result: Any) -> None:
    error_message = _result_value(result, "error_message")
    if isinstance(error_message, str) and error_message:
        raise RuntimeError("Tzafon action failed")
    status = _result_value(result, "status")
    if not isinstance(status, str) or status.lower() not in _SUCCESS_ACTION_STATUSES:
        raise RuntimeError("Tzafon action failed")


def _ensure_batch_succeeded(result: Any, *, expected_actions: int) -> None:
    error_message = _result_value(result, "error_message")
    if isinstance(error_message, str) and error_message:
        raise RuntimeError("Tzafon batch failed")
    status = _result_value(result, "status")
    if status is not None and (
        not isinstance(status, str) or status.lower() not in _SUCCESS_ACTION_STATUSES
    ):
        raise RuntimeError("Tzafon batch failed")
    executed = _batch_result_value(result, "executed")
    if isinstance(executed, bool) or not isinstance(executed, int):
        raise RuntimeError("Tzafon batch returned an invalid executed count")
    if executed != expected_actions:
        raise RuntimeError("Tzafon batch did not execute every action")
    results = _batch_result_value(result, "results")
    if isinstance(results, list):
        if len(results) != expected_actions:
            raise RuntimeError("Tzafon batch did not return every action result")
        for action_result in results:
            _ensure_action_succeeded(action_result)


def _batch_result_value(result: Any, key: str) -> Any:
    value = _result_value(result, key)
    if value is not None:
        return value
    return _result_value(_result_value(result, "result"), key)


def _inline_screenshot_payload(result: Any) -> str:
    result_data = _result_value(result, "result")
    payload = _find_inline_screenshot_value(result_data)
    if payload is None:
        raise RuntimeError("Tzafon screenshot response did not include inline base64 image data")
    marker = ";base64,"
    if payload.startswith("data:") and marker in payload:
        payload = payload.split(marker, 1)[1]
    return payload


def _find_inline_screenshot_value(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    for key in _INLINE_SCREENSHOT_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    for key in ("screenshot", "image"):
        nested = _find_inline_screenshot_value(value.get(key))
        if nested is not None:
            return nested
    return None


def _result_value(result: Any, key: str) -> Any:
    if isinstance(result, Mapping):
        return result.get(key)
    return getattr(result, key, None)


def _x11_cursor_readback_command() -> str:
    return (
        "export DISPLAY=${DISPLAY:-:0}; "
        f"{shlex.join(('python3', '-c', _X11_CURSOR_READBACK_SCRIPT))}"
    )
