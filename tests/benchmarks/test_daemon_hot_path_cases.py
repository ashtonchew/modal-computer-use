from __future__ import annotations

import base64
import json

import pytest

from modal_computer_use.benchmarks import (
    COMMAND_ECHO_COMMAND,
    COMMAND_NONLOGIN_SHELL_ECHO_COMMAND,
    TYPE_1000_CHARS_TEXT,
    TYPE_1000_CHARS_TIMEOUT_MS,
    TYPING_BENCHMARK_TEXT,
    run_click_then_screenshot_benchmark,
    run_command_echo_benchmark,
    run_command_nonlogin_shell_echo_benchmark,
    run_coordinate_click_benchmark,
    run_coordinate_click_sequence_benchmark,
    run_move_click_sequence_benchmark,
    run_type_100_chars_benchmark,
    run_type_1000_chars_benchmark,
)


def test_command_benchmarks_preserve_legacy_and_attribute_canonical_nonlogin_shell() -> None:
    seen_commands: list[list[str]] = []

    class TimedClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/commands/run"
            assert headers is None
            seen_commands.append(json["command"])
            return {
                "ok": True,
                "elapsed_ms": 12.5,
                "output": {"returncode": 0, "stdout": "42\n"},
            }

    client = TimedClient()
    legacy = run_command_echo_benchmark(
        client=client,
        iterations=1,
        warmup_iterations=0,
    )
    canonical = run_command_nonlogin_shell_echo_benchmark(
        client=client,
        iterations=1,
        warmup_iterations=0,
    )

    assert seen_commands == [
        list(COMMAND_ECHO_COMMAND),
        list(COMMAND_NONLOGIN_SHELL_ECHO_COMMAND),
    ]
    assert legacy["command"]["argv"] == ["sh", "-lc", "printf '42\\n'"]
    assert legacy["shell_mode"] == "login"
    assert legacy["daemon_samples_ms"] == [12.5]
    assert legacy["attribution"]["status"] == "measured"
    assert canonical["command"] == {
        "argv": ["sh", "-c", "printf '42\\n'"],
        "timeout_seconds": 30,
        "transport_shape": "argv",
    }
    assert canonical["benchmark_semantics"] == "shell-command-echo-v2"
    assert canonical["shell_mode"] == "non_login"
    assert canonical["daemon_samples_ms"] == [12.5]
    assert canonical["attribution"]["status"] == "measured"


@pytest.mark.parametrize(
    "output",
    [
        {"returncode": 7, "stdout": "42\n"},
        {"returncode": 0, "stdout": "wrong"},
        {"returncode": 0, "stdout": "42"},
        {"returncode": 0, "stdout": " 42"},
        {"returncode": 0, "stdout": "42 "},
        {"returncode": 0},
    ],
)
def test_command_benchmark_rejects_invalid_success_sentinel(output: dict[str, object]) -> None:
    class InvalidCommandClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/commands/run"
            return {"ok": True, "elapsed_ms": 12.5, "output": output}

    payload = run_command_nonlogin_shell_echo_benchmark(
        client=InvalidCommandClient(),
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["status"] == "failed"
    assert payload["successful_iterations"] == 0
    assert payload["samples_ms"] == []
    assert payload["failures"][0]["message"] == (
        "daemon command did not return the expected success sentinel"
    )


def test_type_100_chars_benchmark_uses_safe_metadata_and_attribution() -> None:
    class TimedClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            assert headers is None
            assert json["actions"][0]["type"] == "type"
            assert json["actions"][0]["text"] == TYPING_BENCHMARK_TEXT
            assert json["actions"][0]["method"] == "keystrokes"
            assert json["actions"][0]["delay_ms"] == 0
            return {
                "ok": True,
                "results": [{"ok": True, "output": {"length": 100, "method": "keystrokes"}}],
                "timing": {"daemon_ms": 12.5},
            }

    payload = run_type_100_chars_benchmark(
        client=TimedClient(),
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["status"] == "ok"
    assert payload["request"] == {
        "character_count": 100,
        "method": "keystrokes",
        "delay_ms": 0,
    }
    assert payload["daemon_samples_ms"] == [12.5]
    assert payload["resolved_methods"] == ["keystrokes"]
    assert payload["attribution"]["status"] == "measured"
    serialized = json.dumps(payload)
    assert TYPING_BENCHMARK_TEXT not in serialized
    assert '"text"' not in serialized

def test_type_100_chars_missing_timing_is_unavailable_not_failure() -> None:
    class OldClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            return {"ok": True, "results": [{"ok": True}]}

    payload = run_type_100_chars_benchmark(
        client=OldClient(),
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["status"] == "ok"
    assert payload["attribution"] == {
        "status": "unavailable",
        "reason": "daemon response did not include timing.daemon_ms",
    }
    assert payload["daemon_samples_ms"] == []
    assert payload["failures"] == []

def test_type_100_chars_malformed_timing_is_structured_failure() -> None:
    class MalformedTimingClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            return {"ok": True, "results": [{"ok": True}], "timing": {"daemon_ms": "fast"}}

    payload = run_type_100_chars_benchmark(
        client=MalformedTimingClient(),
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["status"] == "failed"
    assert payload["failures"][0]["case"] == "type_100_chars"
    assert payload["failures"][0]["message"] == "daemon action timing.daemon_ms was malformed"

def test_type_100_chars_failure_does_not_leak_typed_payload(monkeypatch) -> None:
    sentinel = "_".join(["SENTINEL", "TYPED", "PAYLOAD", "NO", "LEAK"])

    class FailingTypeClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            raise RuntimeError(f"backend echoed {sentinel}")

    monkeypatch.setattr("modal_computer_use.benchmarks.hot_paths.TYPING_BENCHMARK_TEXT", sentinel)

    payload = run_type_100_chars_benchmark(
        client=FailingTypeClient(),
        iterations=1,
        warmup_iterations=0,
    )

    serialized = json.dumps(payload)
    assert payload["status"] == "failed"
    assert payload["failures"][0]["message"] == "backend echoed [redacted typed text]"
    assert sentinel not in serialized
    assert '"text"' not in serialized

def test_type_1000_chars_benchmark_uses_safe_metadata_and_attribution() -> None:
    class TimedClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            assert headers is None
            assert json["actions"][0]["type"] == "type"
            assert json["actions"][0]["text"] == TYPE_1000_CHARS_TEXT
            assert json["actions"][0]["method"] == "keystrokes"
            assert json["actions"][0]["delay_ms"] == 0
            assert json["actions"][0]["timeout_ms"] == TYPE_1000_CHARS_TIMEOUT_MS
            return {
                "ok": True,
                "results": [{"ok": True, "output": {"length": 1000, "method": "keystrokes"}}],
                "timing": {"daemon_ms": 125.0},
            }

    payload = run_type_1000_chars_benchmark(
        client=TimedClient(),
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["status"] == "ok"
    assert payload["request"] == {
        "character_count": 1000,
        "method": "keystrokes",
        "delay_ms": 0,
        "timeout_ms": TYPE_1000_CHARS_TIMEOUT_MS,
    }
    assert payload["daemon_samples_ms"] == [125.0]
    assert payload["resolved_methods"] == ["keystrokes"]
    assert payload["attribution"]["status"] == "measured"
    serialized = json.dumps(payload)
    assert TYPE_1000_CHARS_TEXT not in serialized
    assert '"text"' not in serialized

def test_type_1000_chars_failure_does_not_leak_typed_payload(monkeypatch) -> None:
    sentinel = "_".join(["SENTINEL", "TYPED", "PAYLOAD", "1000", "NO", "LEAK"])

    class FailingTypeClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            raise RuntimeError(f"backend echoed {sentinel}")

    monkeypatch.setattr("modal_computer_use.benchmarks.hot_paths.TYPE_1000_CHARS_TEXT", sentinel)

    payload = run_type_1000_chars_benchmark(
        client=FailingTypeClient(),
        iterations=1,
        warmup_iterations=0,
    )

    serialized = json.dumps(payload)
    assert payload["status"] == "failed"
    assert payload["failures"][0]["message"] == "backend echoed [redacted typed text]"
    assert sentinel not in serialized
    assert '"text"' not in serialized

def test_type_1000_chars_failed_daemon_response_keeps_safe_error_detail() -> None:
    class FailedDaemonClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            return {
                "ok": False,
                "results": [
                    {
                        "index": 0,
                        "type": "type",
                        "ok": False,
                        "error_code": "timeout",
                        "error": "action timed out after 30000 ms",
                    }
                ],
                "timing": {"daemon_ms": 10100.0},
            }

    payload = run_type_1000_chars_benchmark(
        client=FailedDaemonClient(),
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["status"] == "failed"
    assert payload["failures"][0]["message"] == (
        "daemon action response was not ok: result[0] timeout: action timed out after 30000 ms"
    )

def test_move_click_sequence_benchmark_uses_safe_metadata_and_attribution() -> None:
    seen_actions = []

    class TimedClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            seen_actions.extend(json["actions"])
            return {
                "ok": True,
                "results": [{"ok": True} for _action in json["actions"]],
                "timing": {"daemon_ms": 18.0},
            }

    payload = run_move_click_sequence_benchmark(
        client=TimedClient(),
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["status"] == "ok"
    assert payload["action_count"] == 8
    assert payload["actions"] == [
        {"type": "move"},
        {"type": "click", "button": "left"},
        {"type": "move"},
        {"type": "click", "button": "left"},
        {"type": "move"},
        {"type": "click", "button": "left"},
        {"type": "move"},
        {"type": "click", "button": "left"},
    ]
    assert payload["daemon_samples_ms"] == [18.0]
    assert payload["attribution"]["status"] == "measured"
    serialized = json.dumps(payload)
    assert '"x"' not in serialized
    assert '"y"' not in serialized
    assert [action["type"] for action in seen_actions] == [
        "move",
        "click",
        "move",
        "click",
        "move",
        "click",
        "move",
        "click",
    ]


def test_coordinate_click_benchmark_uses_click_only_schedule_and_accounting() -> None:
    seen_actions: list[list[dict[str, object]]] = []

    class TimedClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            seen_actions.append(json["actions"])
            return {
                "ok": True,
                "results": [{"ok": True} for _action in json["actions"]],
                "timing": {"daemon_ms": 1.0},
            }

    payload = run_coordinate_click_benchmark(
        client=TimedClient(), iterations=2, warmup_iterations=1
    )

    assert seen_actions == [
        [{"type": "click", "x": 24, "y": 24, "button": "left"}],
        [{"type": "click", "x": 25, "y": 25, "button": "left"}],
        [{"type": "click", "x": 24, "y": 24, "button": "left"}],
    ]
    assert payload["semantic"] == "coordinate_click"
    assert payload["benchmark_semantics"] == "coordinate-click-v1"
    assert payload["logical_action_count"] == payload["provider_action_count"] == 1
    assert payload["provider_sdk_call_count"] == payload["transport_request_count"] == 1
    assert payload["request_count_source"] == "harness_direct"
    assert payload["native_batch"] is False
    assert payload["batching"] == "single_request"


def test_coordinate_click_sequence_uses_four_clicks_in_one_request() -> None:
    seen_actions: list[dict[str, object]] = []

    class TimedClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            seen_actions.extend(json["actions"])
            return {
                "ok": True,
                "results": [{"ok": True} for _action in json["actions"]],
                "timing": {"daemon_ms": 2.0},
            }

    payload = run_coordinate_click_sequence_benchmark(
        client=TimedClient(), iterations=1, warmup_iterations=0
    )

    assert seen_actions == [
        {"type": "click", "x": 16, "y": 16, "button": "left"},
        {"type": "click", "x": 128, "y": 16, "button": "left"},
        {"type": "click", "x": 128, "y": 128, "button": "left"},
        {"type": "click", "x": 16, "y": 128, "button": "left"},
    ]
    assert payload["semantic"] == "coordinate_click_sequence"
    assert payload["benchmark_semantics"] == "coordinate-click-v1"
    assert payload["logical_action_count"] == payload["provider_action_count"] == 4
    assert payload["provider_sdk_call_count"] == payload["transport_request_count"] == 1
    assert payload["native_batch"] is True
    assert payload["batching"] == "single_request"


def test_click_then_screenshot_benchmark_uses_fused_binary_endpoint() -> None:
    class Transport:
        last_http_version = "HTTP/2"

    class TimedClient:
        base_url = "http://testserver"
        transport = Transport()

        def post_bytes_with_headers(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run/raw-screenshot"
            assert headers is None
            assert json["screenshot_after"] is True
            assert json["screenshot_options"] == {"format": "png", "show_cursor": False}
            action_result = {
                "ok": True,
                "results": [{"ok": True}, {"ok": True}],
                "timing": {"daemon_ms": 20.0},
            }
            return b"png-bytes", {
                "x-computer-use-width": "1024",
                "x-computer-use-height": "768",
                "x-computer-use-action-result": base64.b64encode(
                    json_module_dumps(action_result).encode("utf-8")
                ).decode("ascii"),
                "x-computer-use-timing-ms": '{"total_ms":8.5}',
                "x-computer-use-capture-backend": "mss",
            }

    payload = run_click_then_screenshot_benchmark(
        client=TimedClient(),
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["status"] == "ok"
    assert payload["daemon_samples_ms"] == [20.0]
    assert payload["samples_bytes"] == [9]
    assert payload["last_result"]["screenshot_daemon_timing_ms"] == {"total_ms": 8.5}
    assert payload["last_result"]["capture_backend"] == "mss"
    assert payload["last_result"]["transport_http_version"] == "HTTP/2"
    assert payload["transport_http_versions"] == ["HTTP/2"]
    assert payload["action_count"] == 2


def json_module_dumps(value) -> str:
    return json.dumps(value, separators=(",", ":"))
