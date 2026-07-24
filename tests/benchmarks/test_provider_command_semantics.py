from __future__ import annotations

from types import SimpleNamespace

from modal_computer_use.benchmarks.provider_comparison.daytona import DaytonaDriver
from modal_computer_use.benchmarks.provider_comparison.e2b import E2BDriver


class _Commands:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []

    def run(self, command: str, timeout: int | None = None):
        self.calls.append((command, timeout))
        return SimpleNamespace(exit_code=0, stdout="42")


def test_daytona_canonical_command_uses_nonlogin_shell_with_honest_metadata() -> None:
    calls: list[tuple[str, int]] = []

    class Process:
        def exec(self, command: str, timeout: int = 30):
            calls.append((command, timeout))
            return SimpleNamespace(exit_code=0, stdout="42")

    driver = object.__new__(DaytonaDriver)
    result = driver.command_nonlogin_shell_echo(SimpleNamespace(process=Process()))

    assert calls == [("sh -c 'printf 42'", 30)]
    assert result == {
        "exit_code": 0,
        "benchmark_semantics": "shell-command-echo-v2",
        "shell_mode": "non_login",
        "command": {
            "argv": ["sh", "-c", "printf 42"],
            "timeout_seconds": 30,
            "transport_shape": "command_string",
        },
    }


def test_e2b_canonical_command_uses_nonlogin_shell_with_honest_metadata() -> None:
    commands = _Commands()
    driver = object.__new__(E2BDriver)

    result = driver.command_nonlogin_shell_echo(SimpleNamespace(commands=commands))

    assert commands.calls == [("sh -c 'printf 42'", 30)]
    assert result == {
        "exit_code": 0,
        "benchmark_semantics": "shell-command-echo-v2",
        "shell_mode": "non_login",
        "command": {
            "argv": ["sh", "-c", "printf 42"],
            "timeout_seconds": 30,
            "transport_shape": "command_string",
        },
    }
