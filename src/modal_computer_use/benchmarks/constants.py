from __future__ import annotations

from typing import Any, Literal

BenchmarkMode = Literal["mock-local", "http"]
BenchmarkSurface = Literal[
    "daemon-http",
    "daemon-hot-session",
    "daemon-transport-floor",
    "daemon-observation-stream",
    "sandbox-exec",
    "openai-adapter",
    "anthropic-adapter",
    "action-executor",
]
FutureBenchmarkStatus = Literal["not_measured", "unsupported"]
DEFAULT_SDK_BENCHMARK_SURFACES: tuple[BenchmarkSurface, ...] = (
    "daemon-http",
    "openai-adapter",
    "anthropic-adapter",
    "action-executor",
)
ACTION_BATCH_ACTIONS: list[dict[str, Any]] = [
    {"type": "move", "x": 10, "y": 10},
    {"type": "cursor_position"},
    {"type": "wait", "duration_ms": 0},
    {"type": "move", "x": 20, "y": 20},
    {"type": "cursor_position"},
]
MOVE_CLICK_ACTIONS: list[dict[str, Any]] = [
    {"type": "move", "x": 24, "y": 24},
    {"type": "click", "x": 24, "y": 24, "button": "left"},
]
MOVE_CLICK_SEQUENCE_ACTIONS: list[dict[str, Any]] = [
    {"type": "move", "x": 16, "y": 16},
    {"type": "click", "x": 16, "y": 16, "button": "left"},
    {"type": "move", "x": 128, "y": 16},
    {"type": "click", "x": 128, "y": 16, "button": "left"},
    {"type": "move", "x": 128, "y": 128},
    {"type": "click", "x": 128, "y": 128, "button": "left"},
    {"type": "move", "x": 16, "y": 128},
    {"type": "click", "x": 16, "y": 128, "button": "left"},
]
TYPING_BENCHMARK_TEXT = "0123456789" * 10
TYPE_1000_CHARS_TEXT = "0123456789" * 100
TYPING_BENCHMARK_METHOD = "xdotool"
TYPE_1000_CHARS_TIMEOUT_MS = 30_000
ADAPTER_BENCHMARK_TEXT = TYPING_BENCHMARK_TEXT
COMMAND_ECHO_COMMAND: tuple[str, ...] = ("sh", "-lc", "printf 42")
SANDBOX_EXEC_MOVE_CLICK_COMMAND: tuple[str, ...] = (
    "sh",
    "-lc",
    "command -v xdotool >/dev/null 2>&1 || exit 127; xdotool mousemove 24 24 click 1",
)
