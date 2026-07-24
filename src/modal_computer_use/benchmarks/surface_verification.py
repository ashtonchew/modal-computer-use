from __future__ import annotations

import shlex
from typing import Any

from ..client import DaemonClient
from ..models import ActionBatchResult
from . import core

TYPE_READBACK_TEXT = "mcu-readback-0123456789"
TYPE_READBACK_FILE = "/tmp/modal-computer-use-type-readback-xev.log"  # noqa: S108
TYPE_READBACK_PID_FILE = "/tmp/modal-computer-use-type-readback-xev.pid"  # noqa: S108
TYPE_READBACK_TITLE = "mcu-type-readback"
TYPE_READBACK_FOCUS_X = 40
TYPE_READBACK_FOCUS_Y = 60


def _run_daemon_http_verification(client: DaemonClient) -> dict[str, Any]:
    def run_command(command: str, timeout: int) -> str:
        return _run_daemon_command(client, command, timeout=timeout)

    return {
        "cursor_position": _verification_step(
            lambda: _verify_cursor_position(run_command),
            redacted_text=None,
        ),
        "type_text": _verification_step(
            lambda: _verify_daemon_type_readback(client),
            redacted_text=TYPE_READBACK_TEXT,
        ),
    }

def _run_daemon_command(client: DaemonClient, command: str, *, timeout: int) -> str:
    result = client.post_json(
        "/v1/commands/run",
        json={"command": ["sh", "-lc", command], "timeout": timeout},
    )
    if not isinstance(result, dict) or not result.get("ok", False):
        raise RuntimeError("daemon readback command failed")
    output = result.get("output") if isinstance(result.get("output"), dict) else {}
    stdout = output.get("stdout") if isinstance(output, dict) else ""
    return stdout if isinstance(stdout, str) else ""

def _verify_daemon_type_readback(client: DaemonClient) -> dict[str, Any]:
    def run_command(command: str, timeout: int) -> str:
        return _run_daemon_command(client, command, timeout=timeout)

    def focus_target() -> None:
        _run_daemon_actions(
            client,
            [
                {"type": "move", "x": TYPE_READBACK_FOCUS_X, "y": TYPE_READBACK_FOCUS_Y},
                {
                    "type": "click",
                    "x": TYPE_READBACK_FOCUS_X,
                    "y": TYPE_READBACK_FOCUS_Y,
                    "button": "left",
                },
            ],
        )

    def type_text(text: str) -> None:
        _run_daemon_actions(
            client,
            [
                {
                    "type": "type",
                    "text": text,
                    "method": "keystrokes",
                    "delay_ms": 10,
                }
            ],
        )

    setup = _run_type_readback_setup(run_command)
    if setup["status"] != "ready":
        return setup
    focus_target()
    type_text(TYPE_READBACK_TEXT)
    result = _read_type_readback_file(run_command)
    result["method"] = "daemon_action_type"
    return result

def _run_daemon_actions(client: DaemonClient, actions: list[dict[str, Any]]) -> None:
    result = client.post_json("/v1/actions/run", json={"actions": actions})
    if not _action_batch_ok(result):
        raise RuntimeError("daemon readback action failed")

def _action_batch_ok(result: Any) -> bool:
    if isinstance(result, ActionBatchResult):
        return result.ok
    if isinstance(result, dict):
        value = result.get("ok")
        if isinstance(value, bool):
            return value
        items = result.get("results")
        if isinstance(items, list):
            return all(isinstance(item, dict) and item.get("ok") is True for item in items)
    return False

def _verify_cursor_position(run_command: Any) -> dict[str, Any]:
    expected = _expected_sequence_cursor_position()
    output = run_command("xdotool getmouselocation --shell", 10)
    observed = _parse_xdotool_position(output)
    if observed is None:
        return {
            "status": "unsupported",
            "reason": "cursor readback did not return xdotool coordinates",
            "expected": {"x": expected[0], "y": expected[1]},
            "observed": None,
        }
    ok = observed == expected
    return {
        "status": "ok" if ok else "failed",
        "expected": {"x": expected[0], "y": expected[1]},
        "observed": {"x": observed[0], "y": observed[1]} if observed is not None else None,
    }

def _verification_step(operation: Any, *, redacted_text: str | None) -> dict[str, Any]:
    try:
        return operation()
    except Exception as exc:
        return {
            "status": "failed",
            "message": core._redact_text(str(exc), redacted_text),
        }

def _run_type_readback_setup(run_command: Any) -> dict[str, Any]:
    output = run_command(_type_readback_setup_command(), 15)
    if output.startswith("unsupported:"):
        return {
            "status": "unsupported",
            "reason": output.strip(),
        }
    return {"status": "ready"}

def _read_type_readback_file(run_command: Any) -> dict[str, Any]:
    output = run_command(_type_readback_result_command(), 10)
    observed = _parse_key_value_output(output)
    expected_count = len(TYPE_READBACK_TEXT)
    observed_count = _int_or_none(observed.get("keypress_count"))
    if observed_count is None:
        return {
            "status": "unsupported",
            "reason": "type readback did not return keypress_count",
            "expected": {"minimum_keypress_count": expected_count},
            "observed": {"keypress_count": None},
        }
    ok = observed_count is not None and observed_count >= expected_count
    return {
        "status": "ok" if ok else "failed",
        "expected": {"minimum_keypress_count": expected_count},
        "observed": {"keypress_count": observed_count},
    }

def _type_readback_setup_command() -> str:
    target = shlex.quote(TYPE_READBACK_FILE)
    pid_file = shlex.quote(TYPE_READBACK_PID_FILE)
    title = shlex.quote(TYPE_READBACK_TITLE)
    launcher = shlex.quote(
        "import os, subprocess, sys; "
        "env = os.environ.copy(); "
        "env['DISPLAY'] = env.get('DISPLAY') or ':0'; "
        "out = open(sys.argv[1], 'wb'); "
        "process = subprocess.Popen("
        "['xev', '-event', 'keyboard', '-name', sys.argv[3], '-geometry', '220x120+0+0'], "
        "stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, "
        "env=env, start_new_session=True"
        "); "
        "open(sys.argv[2], 'w').write(str(process.pid))"
    )
    return (
        "export DISPLAY=${DISPLAY:-:0}; "
        "if ! command -v xev >/dev/null 2>&1; then "
        "printf 'unsupported:no-xev\\n'; exit 0; fi; "
        "python_bin=$(command -v python3 || command -v python || true); "
        "if [ -z \"$python_bin\" ]; then printf 'unsupported:no-python\\n'; exit 0; fi; "
        f"rm -f {target} {pid_file}; "
        f"\"$python_bin\" -c {launcher} {target} {pid_file} {title}; "
        "sleep 0.5; "
        "printf 'ready=1\\n'"
    )

def _type_readback_result_command() -> str:
    target = shlex.quote(TYPE_READBACK_FILE)
    pid_file = shlex.quote(TYPE_READBACK_PID_FILE)
    return (
        "sleep 0.2; "
        f"if [ -f {pid_file} ]; then kill $(cat {pid_file}) >/dev/null 2>&1 || true; fi; "
        f"if [ ! -f {target} ]; then printf 'missing=1\\n'; exit 0; fi; "
        f"count=$(grep -c 'KeyPress event' {target} 2>/dev/null || true); "
        "printf 'keypress_count=%s\\n' \"$count\""
    )

def _expected_sequence_cursor_position() -> tuple[int, int]:
    for action in reversed(core.MOVE_CLICK_SEQUENCE_ACTIONS):
        if action["type"] == "move":
            return int(action["x"]), int(action["y"])
    raise RuntimeError("move/click sequence did not include a move action")

def _parse_xdotool_position(output: str) -> tuple[int, int] | None:
    values = _parse_key_value_output(output)
    x = _int_or_none(values.get("X"))
    y = _int_or_none(values.get("Y"))
    if x is None or y is None:
        return None
    return x, y

def _parse_key_value_output(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return values

def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
