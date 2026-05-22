from __future__ import annotations

import base64
import json

from modal_computer_use.benchmarks import (
    TYPE_1000_CHARS_TEXT,
    TYPE_1000_CHARS_TIMEOUT_MS,
    TYPING_BENCHMARK_TEXT,
    run_click_screenshot_raw_benchmark,
    run_move_click_sequence_benchmark,
    run_type_100_chars_benchmark,
    run_type_1000_chars_benchmark,
)


def test_type_100_chars_benchmark_uses_safe_metadata_and_attribution() -> None:
    class TimedClient:
        base_url = "http://testserver"

        def post_json(self, path: str, *, json=None, headers=None):
            assert path == "/v1/actions/run"
            assert headers is None
            assert json["actions"][0]["type"] == "type"
            assert json["actions"][0]["text"] == TYPING_BENCHMARK_TEXT
            return {
                "ok": True,
                "results": [{"ok": True, "output": {"length": 100, "method": "xdotool"}}],
                "timing": {"daemon_ms": 12.5},
            }

    payload = run_type_100_chars_benchmark(
        client=TimedClient(),
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["status"] == "ok"
    assert payload["request"] == {"character_count": 100, "method": "xdotool"}
    assert payload["daemon_samples_ms"] == [12.5]
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
            assert json["actions"][0]["method"] == "xdotool"
            assert json["actions"][0]["timeout_ms"] == TYPE_1000_CHARS_TIMEOUT_MS
            return {
                "ok": True,
                "results": [{"ok": True, "output": {"length": 1000, "method": "xdotool"}}],
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
        "method": "xdotool",
        "timeout_ms": TYPE_1000_CHARS_TIMEOUT_MS,
    }
    assert payload["daemon_samples_ms"] == [125.0]
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


def test_click_screenshot_raw_benchmark_uses_fused_binary_endpoint() -> None:
    class TimedClient:
        base_url = "http://testserver"

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
            }

    payload = run_click_screenshot_raw_benchmark(
        client=TimedClient(),
        iterations=1,
        warmup_iterations=0,
    )

    assert payload["status"] == "ok"
    assert payload["daemon_samples_ms"] == [20.0]
    assert payload["samples_bytes"] == [9]
    assert payload["last_result"]["screenshot_daemon_timing_ms"] == {"total_ms": 8.5}
    assert payload["action_count"] == 2


def json_module_dumps(value) -> str:
    return json.dumps(value, separators=(",", ":"))
