from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Any

import pytest

from modal_computer_use.daemon.desktop.process_runner import (
    AsyncioProcessRunner,
    IsolatedAsyncioProcessRunner,
    ProcessRunnerCapacityError,
    ThreadedProcessRunner,
)


@pytest.mark.parametrize("runner_kind", ["asyncio", "threaded", "isolated-asyncio"])
def test_process_runner_preserves_argv_env_stdin_and_output(runner_kind: str) -> None:
    runners = {
        "asyncio": AsyncioProcessRunner,
        "threaded": lambda: ThreadedProcessRunner(max_workers=1, max_pending=1),
        "isolated-asyncio": lambda: IsolatedAsyncioProcessRunner(max_active=2),
    }
    runner = runners[runner_kind]()

    async def exercise() -> subprocess.CompletedProcess[str]:
        return await runner.run(
            sys.executable,
            "-c",
            (
                "import os, sys; "
                "data = sys.stdin.read(); "
                "sys.stdout.write(os.environ['RUNNER_SENTINEL'] + ':' + data); "
                "sys.stderr.write('stderr')"
            ),
            env={"RUNNER_SENTINEL": "argv-ok"},
            input_text="stdin-ok",
        )

    try:
        result = asyncio.run(exercise())
    finally:
        runner.close()

    assert result.args[:2] == (sys.executable, "-c")
    assert result.returncode == 0
    assert result.stdout == "argv-ok:stdin-ok"
    assert result.stderr == "stderr"


@pytest.mark.parametrize("runner_kind", ["asyncio", "threaded", "isolated-asyncio"])
def test_process_runner_preserves_nonzero_check_behavior(runner_kind: str) -> None:
    runners = {
        "asyncio": AsyncioProcessRunner,
        "threaded": lambda: ThreadedProcessRunner(max_workers=1, max_pending=1),
        "isolated-asyncio": lambda: IsolatedAsyncioProcessRunner(max_active=2),
    }
    runner = runners[runner_kind]()

    async def exercise() -> subprocess.CompletedProcess[str]:
        return await runner.run(
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('failed'); raise SystemExit(7)",
            check=False,
        )

    try:
        result = asyncio.run(exercise())
        with pytest.raises(RuntimeError, match="failed"):
            asyncio.run(
                runner.run(
                    sys.executable,
                    "-c",
                    "import sys; sys.stderr.write('failed'); raise SystemExit(7)",
                )
            )
    finally:
        runner.close()

    assert result.returncode == 7
    assert result.stderr == "failed"


def test_threaded_runner_timeout_kills_drains_and_reaps() -> None:
    state: dict[str, Any] = {"calls": 0, "killed": False}

    class TimedOutProcess:
        args = ("hang",)
        returncode: int | None = None

        def communicate(self, _input=None, timeout=None):
            state["calls"] += 1
            if state["calls"] == 1:
                raise subprocess.TimeoutExpired(self.args, timeout)
            self.returncode = -9
            return b"partial", b"timeout"

        def kill(self) -> None:
            state["killed"] = True
            self.returncode = -9

        def poll(self) -> int | None:
            return self.returncode

    runner = ThreadedProcessRunner(
        max_workers=1,
        max_pending=0,
        popen_factory=lambda *_args, **_kwargs: TimedOutProcess(),
    )
    try:
        with pytest.raises(TimeoutError):
            asyncio.run(runner.run("hang", timeout=0.01))
    finally:
        runner.close()

    assert state == {"calls": 2, "killed": True}


def test_threaded_runner_cancellation_kills_drains_and_reraises() -> None:
    attached = threading.Event()
    released = threading.Event()
    state = {"killed": False, "communicated": False}

    class BlockingProcess:
        args = ("hang",)
        returncode: int | None = None

        def communicate(self, _input=None, timeout=None):
            attached.set()
            assert released.wait(timeout=2)
            state["communicated"] = True
            self.returncode = -9
            return b"", b""

        def kill(self) -> None:
            state["killed"] = True
            released.set()

        def poll(self) -> int | None:
            return self.returncode

    runner = ThreadedProcessRunner(
        max_workers=1,
        max_pending=0,
        popen_factory=lambda *_args, **_kwargs: BlockingProcess(),
    )

    async def exercise() -> None:
        task = asyncio.create_task(runner.run("hang"))
        await asyncio.to_thread(attached.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(exercise())
    finally:
        runner.close()

    assert state == {"killed": True, "communicated": True}


def test_threaded_runner_rejects_work_beyond_bounded_capacity() -> None:
    attached = threading.Event()
    released = threading.Event()

    class BlockingProcess:
        args = ("hang",)
        returncode: int | None = None

        def communicate(self, _input=None, timeout=None):
            attached.set()
            assert released.wait(timeout=2)
            self.returncode = 0
            return b"", b""

        def kill(self) -> None:
            released.set()

        def poll(self) -> int | None:
            return self.returncode

    runner = ThreadedProcessRunner(
        max_workers=1,
        max_pending=0,
        popen_factory=lambda *_args, **_kwargs: BlockingProcess(),
    )

    async def exercise() -> None:
        first = asyncio.create_task(runner.run("hang"))
        await asyncio.to_thread(attached.wait, 2)
        with pytest.raises(ProcessRunnerCapacityError):
            await runner.run("second")
        released.set()
        await first

    try:
        asyncio.run(exercise())
    finally:
        runner.close()


def test_threaded_runner_cancels_queued_work_without_spawning() -> None:
    first_attached = threading.Event()
    first_released = threading.Event()
    calls: list[tuple[str, ...]] = []

    class BlockingProcess:
        returncode: int | None = None

        def __init__(self, args: tuple[str, ...]) -> None:
            self.args = args

        def communicate(self, _input=None, timeout=None):
            first_attached.set()
            assert first_released.wait(timeout=2)
            self.returncode = 0
            return b"", b""

        def kill(self) -> None:
            first_released.set()

        def poll(self) -> int | None:
            return self.returncode

    def create_process(args: tuple[str, ...], **_kwargs: object) -> BlockingProcess:
        calls.append(args)
        return BlockingProcess(args)

    runner = ThreadedProcessRunner(
        max_workers=1,
        max_pending=1,
        popen_factory=create_process,
    )

    async def exercise() -> None:
        first = asyncio.create_task(runner.run("first"))
        await asyncio.to_thread(first_attached.wait, 2)
        queued = asyncio.create_task(runner.run("queued"))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        first_released.set()
        await first

    try:
        asyncio.run(exercise())
    finally:
        runner.close()

    assert calls == [("first",)]


def test_threaded_runner_close_is_idempotent_and_rejects_new_work() -> None:
    runner = ThreadedProcessRunner(max_workers=1, max_pending=0)

    runner.close()
    runner.close()

    with pytest.raises(RuntimeError, match="closed"):
        asyncio.run(runner.run("true"))


class _ControlledRemoteRunner:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.cleanup_release = threading.Event()
        self.cleaned = threading.Event()
        self.closed = threading.Event()

    async def run(
        self,
        *args: str,
        env=None,
        timeout: float = 10.0,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del env, timeout, input_text, check
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            while not self.cleanup_release.is_set():
                await asyncio.sleep(0.001)
            self.cleaned.set()
            raise
        return subprocess.CompletedProcess(args, 0, "", "")

    def close(self) -> None:
        self.closed.set()


def test_isolated_asyncio_runner_timeout_kills_drains_and_reaps() -> None:
    runner = IsolatedAsyncioProcessRunner(max_active=1)
    try:
        with pytest.raises(TimeoutError):
            asyncio.run(
                runner.run(
                    sys.executable,
                    "-c",
                    "import time; time.sleep(10)",
                    timeout=0.01,
                )
            )
        result = asyncio.run(
            runner.run(sys.executable, "-c", "print('reaped')", timeout=1)
        )
    finally:
        runner.close()

    assert result.stdout == "reaped\n"


def test_isolated_asyncio_runner_cancellation_waits_for_cleanup_acknowledgment() -> None:
    remote = _ControlledRemoteRunner()
    runner = IsolatedAsyncioProcessRunner(
        max_active=1,
        remote_runner_factory=lambda: remote,
    )

    async def exercise() -> None:
        task = asyncio.create_task(runner.run("hang"))
        await asyncio.to_thread(remote.started.wait, 2)
        task.cancel()
        await asyncio.to_thread(remote.cancelled.wait, 2)
        await asyncio.sleep(0.01)
        assert not task.done()
        remote.cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert remote.cleaned.is_set()

    try:
        asyncio.run(exercise())
    finally:
        runner.close()


def test_isolated_asyncio_runner_cancellation_before_spawn_waits_for_remote_ack() -> None:
    remote = _ControlledRemoteRunner()
    loop = asyncio.SelectorEventLoop()
    scheduled: list[Callable[[], None]] = []
    runner = IsolatedAsyncioProcessRunner(
        max_active=1,
        remote_runner_factory=lambda: remote,
        loop_factory=lambda: loop,
        remote_scheduler=scheduled.append,
    )

    async def exercise() -> None:
        task = asyncio.create_task(runner.run("never-spawned"))
        while not scheduled:
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        loop.call_soon_threadsafe(scheduled.pop())
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(exercise())
    finally:
        runner.close()

    assert not remote.started.is_set()


def test_isolated_asyncio_runner_rejects_work_beyond_bounded_capacity() -> None:
    remote = _ControlledRemoteRunner()
    runner = IsolatedAsyncioProcessRunner(
        max_active=1,
        remote_runner_factory=lambda: remote,
    )

    async def exercise() -> None:
        first = asyncio.create_task(runner.run("hang"))
        await asyncio.to_thread(remote.started.wait, 2)
        with pytest.raises(ProcessRunnerCapacityError):
            await runner.run("second")
        first.cancel()
        remote.cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await first

    try:
        asyncio.run(exercise())
    finally:
        runner.close()


def test_isolated_asyncio_runner_close_waits_for_active_cleanup() -> None:
    remote = _ControlledRemoteRunner()
    runner = IsolatedAsyncioProcessRunner(
        max_active=1,
        remote_runner_factory=lambda: remote,
    )

    async def exercise() -> None:
        task = asyncio.create_task(runner.run("hang"))
        await asyncio.to_thread(remote.started.wait, 2)
        close_done = threading.Event()
        close_thread = threading.Thread(
            target=lambda: (runner.close(), close_done.set()),
        )
        close_thread.start()
        await asyncio.to_thread(remote.cancelled.wait, 2)
        assert not close_done.is_set()
        remote.cleanup_release.set()
        await asyncio.to_thread(close_thread.join, 2)
        assert close_done.is_set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    runner.close()
    assert remote.cleaned.is_set()
    assert remote.closed.is_set()


def test_isolated_asyncio_runner_close_kills_drains_and_reaps_active_child(
    monkeypatch,
) -> None:
    started = threading.Event()
    state = {"killed": False, "drained": False}

    class BlockingProcess:
        returncode: int | None = None

        async def communicate(self, _input=None):
            if state["killed"]:
                state["drained"] = True
                return b"", b""
            started.set()
            await asyncio.Future()

        def kill(self) -> None:
            state["killed"] = True
            self.returncode = -9

    async def create_process(*_args, **_kwargs):
        return BlockingProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    runner = IsolatedAsyncioProcessRunner(max_active=1)

    async def exercise() -> None:
        task = asyncio.create_task(runner.run("hang"))
        await asyncio.to_thread(started.wait, 2)
        await asyncio.to_thread(runner.close)
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert state == {"killed": True, "drained": True}


def test_isolated_asyncio_runner_close_rejects_new_work() -> None:
    runner = IsolatedAsyncioProcessRunner(max_active=1)

    runner.close()
    runner.close()

    with pytest.raises(RuntimeError, match="closed"):
        asyncio.run(runner.run("true"))


def test_isolated_asyncio_runner_thread_cannot_block_process_shutdown() -> None:
    runner = IsolatedAsyncioProcessRunner(max_active=1)
    try:
        assert runner._thread.daemon is True
    finally:
        runner.close()
