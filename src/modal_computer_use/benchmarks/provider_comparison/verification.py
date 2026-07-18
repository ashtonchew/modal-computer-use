from __future__ import annotations

import shlex
from collections.abc import Callable
from typing import Any

from ..constants import MOVE_CLICK_SEQUENCE_ACTIONS
from ..safety import _redact_text
from .sdk_support import call_first_available, provider_computer_use

TYPE_READBACK_TEXT = "mcu-readback-0123456789"
TYPE_READBACK_FILE = "/tmp/modal-computer-use-type-readback-xev.log"  # noqa: S108
TYPE_READBACK_PID_FILE = "/tmp/modal-computer-use-type-readback-xev.pid"  # noqa: S108
TYPE_READBACK_TITLE = "mcu-type-readback"
TYPE_READBACK_FOCUS_X = 40
TYPE_READBACK_FOCUS_Y = 60


def verify_daytona_cursor_position(sandbox: Any) -> dict[str, Any]:
    expected = _expected_sequence_cursor_position()
    observed = _provider_point_xy(
        call_first_available(provider_computer_use(sandbox).mouse, ("get_position", "position"))
    )
    ok = observed == expected
    return {
        "status": "ok" if ok else "failed",
        "expected": {"x": expected[0], "y": expected[1]},
        "observed": {"x": observed[0], "y": observed[1]} if observed is not None else None,
        "method": "computer_use.mouse.get_position",
    }


def verify_provider_cursor_position(
    run_command: Callable[[str, int], str],
) -> dict[str, Any]:
    expected = _expected_sequence_cursor_position()
    output = run_command("xdotool getmouselocation --shell", 10)
    observed = _parse_xdotool_position(output)
    ok = observed == expected
    return {
        "status": "ok" if ok else "failed",
        "expected": {"x": expected[0], "y": expected[1]},
        "observed": {"x": observed[0], "y": observed[1]} if observed is not None else None,
    }


def verification_step(
    operation: Callable[[], dict[str, Any]],
    *,
    redacted_text: str | None,
) -> dict[str, Any]:
    try:
        return operation()
    except Exception as exc:
        return {
            "status": "failed",
            "message": _redact_text(str(exc), redacted_text),
        }


def verify_provider_type_readback(
    *,
    type_text: Callable[[str], Any],
    focus_target: Callable[[], Any] | None = None,
    run_command: Callable[[str, int], str],
) -> dict[str, Any]:
    setup = _run_type_readback_setup(run_command)
    if setup["status"] != "ready":
        return setup
    if focus_target is not None:
        focus_target()
    type_text(TYPE_READBACK_TEXT)
    return _read_type_readback_file(run_command)


def _run_type_readback_setup(run_command: Callable[[str, int], str]) -> dict[str, Any]:
    output = run_command(_type_readback_setup_command(), 15)
    if output.startswith("unsupported:"):
        return {
            "status": "unsupported",
            "reason": output.strip(),
        }
    return {"status": "ready"}


def _read_type_readback_file(run_command: Callable[[str, int], str]) -> dict[str, Any]:
    output = run_command(_type_readback_result_command(), 10)
    observed = _parse_key_value_output(output)
    expected_count = len(TYPE_READBACK_TEXT)
    observed_count = _int_or_none(observed.get("keypress_count"))
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
        "printf 'unsupported:no-xev\n'; exit 0; fi; "
        "python_bin=$(command -v python3 || command -v python || true); "
        "if [ -z \"$python_bin\" ]; then printf 'unsupported:no-python\n'; exit 0; fi; "
        f"rm -f {target} {pid_file}; "
        f'"$python_bin" -c {launcher} {target} {pid_file} {title}; '
        "sleep 0.5; "
        "printf 'ready=1\n'"
    )


def _type_readback_result_command() -> str:
    target = shlex.quote(TYPE_READBACK_FILE)
    pid_file = shlex.quote(TYPE_READBACK_PID_FILE)
    return (
        "sleep 0.2; "
        f"if [ -f {pid_file} ]; then kill $(cat {pid_file}) >/dev/null 2>&1 || true; fi; "
        f"if [ ! -f {target} ]; then printf 'missing=1\n'; exit 0; fi; "
        f"count=$(grep -c 'KeyPress event' {target} 2>/dev/null || true); "
        "printf 'keypress_count=%s\n' \"$count\""
    )


def _expected_sequence_cursor_position() -> tuple[int, int]:
    for action in reversed(MOVE_CLICK_SEQUENCE_ACTIONS):
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


def _provider_point_xy(value: Any) -> tuple[int, int] | None:
    if isinstance(value, dict):
        x = _int_or_none(value.get("x"))
        y = _int_or_none(value.get("y"))
    else:
        x = _int_or_none(getattr(value, "x", None))
        y = _int_or_none(getattr(value, "y", None))
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


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
