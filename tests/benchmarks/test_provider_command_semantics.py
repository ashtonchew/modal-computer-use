from __future__ import annotations

from types import SimpleNamespace

import pytest

from modal_computer_use.benchmarks.provider_comparison.daytona import DaytonaDriver
from modal_computer_use.benchmarks.provider_comparison.e2b import E2BDriver
from modal_computer_use.benchmarks.provider_comparison.tzafon import TzafonDriver


class _Commands:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []

    def run(self, command: str, timeout: int | None = None):
        self.calls.append((command, timeout))
        return SimpleNamespace(exit_code=0, stdout="42\n")


def test_daytona_canonical_command_uses_nonlogin_shell_with_honest_metadata() -> None:
    calls: list[tuple[str, int]] = []

    class Process:
        def exec(self, command: str, timeout: int = 30):
            calls.append((command, timeout))
            return SimpleNamespace(exit_code=0, stdout="42\n")

    driver = object.__new__(DaytonaDriver)
    result = driver.command_nonlogin_shell_echo(SimpleNamespace(process=Process()))

    assert calls == [("sh -c 'printf '\"'\"'42\\n'\"'\"''", 30)]
    assert result == {
        "exit_code": 0,
        "benchmark_semantics": "shell-command-echo-v2",
        "shell_mode": "non_login",
        "command": {
            "argv": ["sh", "-c", "printf '42\\n'"],
            "timeout_seconds": 30,
            "transport_shape": "command_string",
        },
    }


def test_e2b_canonical_command_uses_nonlogin_shell_with_honest_metadata() -> None:
    commands = _Commands()
    driver = object.__new__(E2BDriver)

    result = driver.command_nonlogin_shell_echo(SimpleNamespace(commands=commands))

    assert commands.calls == [("sh -c 'printf '\"'\"'42\\n'\"'\"''", 30)]
    assert result == {
        "exit_code": 0,
        "benchmark_semantics": "shell-command-echo-v2",
        "shell_mode": "non_login",
        "command": {
            "argv": ["sh", "-c", "printf '42\\n'"],
            "timeout_seconds": 30,
            "transport_shape": "command_string",
        },
    }


def test_e2b_canonical_command_rejects_sdk_without_timeout_support() -> None:
    class NoTimeoutCommands:
        def run(self, command: str):
            return SimpleNamespace(exit_code=0, stdout="42\n")

    driver = object.__new__(E2BDriver)

    with pytest.raises(TypeError):
        driver.command_nonlogin_shell_echo(
            SimpleNamespace(commands=NoTimeoutCommands())
        )


@pytest.mark.parametrize("stdout", ["42", " 42\n", "42 \n", "42\n\n"])
@pytest.mark.parametrize("driver_type", [DaytonaDriver, E2BDriver])
def test_provider_canonical_command_rejects_stdout_whitespace(
    stdout: str, driver_type: type[DaytonaDriver] | type[E2BDriver]
) -> None:
    result = SimpleNamespace(exit_code=0, stdout=stdout)
    driver = object.__new__(driver_type)
    if driver_type is DaytonaDriver:
        sandbox = SimpleNamespace(
            process=SimpleNamespace(exec=lambda *_args, **_kwargs: result)
        )
    else:
        sandbox = SimpleNamespace(
            commands=SimpleNamespace(run=lambda *_args, **_kwargs: result)
        )

    with pytest.raises(RuntimeError, match="expected sentinel"):
        driver.command_nonlogin_shell_echo(sandbox)


@pytest.mark.parametrize("stdout", ["42", " 42\n", "42 \n", "42\n\n"])
def test_tzafon_canonical_command_rejects_stdout_whitespace(stdout: str) -> None:
    driver = object.__new__(TzafonDriver)
    driver._exec = lambda *_args, **_kwargs: SimpleNamespace(  # type: ignore[method-assign]
        status="success",
        result={"exit_code": 0, "stdout": stdout},
    )

    with pytest.raises(RuntimeError, match="expected sentinel"):
        driver.command_nonlogin_shell_echo("computer-1")


@pytest.mark.parametrize("driver_type", [DaytonaDriver, E2BDriver])
def test_provider_legacy_shell_command_also_requires_exact_stdout(
    driver_type: type[DaytonaDriver] | type[E2BDriver],
) -> None:
    result = SimpleNamespace(exit_code=0, stdout="42")
    driver = object.__new__(driver_type)
    if driver_type is DaytonaDriver:
        sandbox = SimpleNamespace(
            process=SimpleNamespace(exec=lambda *_args, **_kwargs: result)
        )
    else:
        sandbox = SimpleNamespace(
            commands=SimpleNamespace(run=lambda *_args, **_kwargs: result)
        )

    with pytest.raises(RuntimeError, match="expected sentinel"):
        driver.command_echo(sandbox)
