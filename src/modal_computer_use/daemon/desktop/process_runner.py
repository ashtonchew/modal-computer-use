from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol


class ProcessRunnerCapacityError(RuntimeError):
    """Raised when the bounded process runner has no available capacity."""


class ProcessRunner(Protocol):
    async def run(
        self,
        *args: str,
        env: Mapping[str, str] | None = None,
        timeout: float = 10.0,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]: ...

    def close(self) -> None: ...


class AsyncioProcessRunner:
    def __init__(self) -> None:
        self._closed = False
        self._processes: set[asyncio.subprocess.Process] = set()

    async def run(
        self,
        *args: str,
        env: Mapping[str, str] | None = None,
        timeout: float = 10.0,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if self._closed:
            raise RuntimeError("process runner is closed")
        process = await asyncio.create_subprocess_exec(
            *args,
            env=None if env is None else dict(env),
            stdin=asyncio.subprocess.PIPE if input_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        self._processes.add(process)
        try:
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(input_text.encode() if input_text is not None else None),
                    timeout=timeout,
                )
            except (TimeoutError, asyncio.CancelledError):
                _kill_process_group(process)
                await _finish_asyncio_process(process)
                raise
            return _completed_result(args, process.returncode, stdout, stderr, check=check)
        finally:
            self._processes.discard(process)

    def close(self) -> None:
        self._closed = True
        for process in tuple(self._processes):
            _kill_process_group(process)


RemoteRunnerFactory = Callable[[], ProcessRunner]
EventLoopFactory = Callable[[], asyncio.AbstractEventLoop]
RemoteScheduler = Callable[[Callable[[], None]], None]


class _IsolatedInvocation:
    def __init__(self) -> None:
        self.result: ConcurrentFuture[subprocess.CompletedProcess[str]] = ConcurrentFuture()
        self.cleanup: ConcurrentFuture[None] = ConcurrentFuture()
        self._lock = threading.Lock()
        self._task: asyncio.Task[subprocess.CompletedProcess[str]] | None = None
        self._cancel_requested = False

    def attach(self, task: asyncio.Task[subprocess.CompletedProcess[str]]) -> None:
        with self._lock:
            self._task = task
            cancel_requested = self._cancel_requested
        if cancel_requested:
            task.cancel()

    def cancel(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._cancel_requested = True
            task = self._task
        if task is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)


class IsolatedAsyncioProcessRunner:
    """Run subprocesses on a private standard-asyncio event-loop thread."""

    def __init__(
        self,
        *,
        max_active: int = 4,
        remote_runner_factory: RemoteRunnerFactory = AsyncioProcessRunner,
        loop_factory: EventLoopFactory = asyncio.SelectorEventLoop,
        remote_scheduler: RemoteScheduler | None = None,
    ) -> None:
        if max_active < 1:
            raise ValueError("max_active must be >= 1")
        self._capacity = max_active
        self._remote_runner_factory = remote_runner_factory
        self._loop = loop_factory()
        self._remote_scheduler = (
            self._schedule_on_loop if remote_scheduler is None else remote_scheduler
        )
        self._lock = threading.Lock()
        self._active: set[_IsolatedInvocation] = set()
        self._closed = False
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._startup_error: BaseException | None = None
        self._remote_runner: ProcessRunner | None = None
        self._thread = threading.Thread(
            target=self._run_loop,
            name="computer-use-asyncio-process",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()
        startup_error = self._get_startup_error()
        if startup_error is not None:
            self._stopped.wait()
            self._thread.join()
            raise RuntimeError("isolated process runner failed to start") from startup_error

    async def run(
        self,
        *args: str,
        env: Mapping[str, str] | None = None,
        timeout: float = 10.0,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        invocation = self._reserve()

        def start_remote() -> None:
            self._start_remote(
                invocation,
                args,
                None if env is None else dict(env),
                timeout,
                input_text,
                check,
            )

        try:
            self._remote_scheduler(start_remote)
        except BaseException:
            self._finish(invocation)
            raise
        result = asyncio.wrap_future(invocation.result)
        try:
            return await asyncio.shield(result)
        except asyncio.CancelledError:
            invocation.cancel(self._loop)
            await _await_cleanup_ack(invocation.cleanup)
            raise

    def _schedule_on_loop(self, callback: Callable[[], None]) -> None:
        self._loop.call_soon_threadsafe(callback)

    def close(self) -> None:
        if threading.current_thread() is self._thread:
            raise RuntimeError("cannot close isolated process runner from its loop thread")
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = tuple(self._active)
        for invocation in active:
            invocation.cancel(self._loop)
        for invocation in active:
            invocation.cleanup.result()
        self._loop.call_soon_threadsafe(self._shutdown_remote)
        self._stopped.wait()
        self._thread.join()

    def _reserve(self) -> _IsolatedInvocation:
        with self._lock:
            if self._closed:
                raise RuntimeError("process runner is closed")
            if len(self._active) >= self._capacity:
                raise ProcessRunnerCapacityError("process runner capacity is exhausted")
            invocation = _IsolatedInvocation()
            self._active.add(invocation)
            return invocation

    def _get_startup_error(self) -> BaseException | None:
        return self._startup_error

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._remote_runner = self._remote_runner_factory()
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
        else:
            self._ready.set()
            self._loop.run_forever()
        finally:
            self._loop.close()
            self._stopped.set()

    def _start_remote(
        self,
        invocation: _IsolatedInvocation,
        args: tuple[str, ...],
        env: dict[str, str] | None,
        timeout: float,
        input_text: str | None,
        check: bool,
    ) -> None:
        remote_runner = self._remote_runner
        if remote_runner is None:
            invocation.result.set_exception(RuntimeError("process runner failed to start"))
            self._finish(invocation)
            return
        task = self._loop.create_task(
            remote_runner.run(
                *args,
                env=env,
                timeout=timeout,
                input_text=input_text,
                check=check,
            )
        )
        invocation.attach(task)
        task.add_done_callback(lambda done: self._remote_done(invocation, done))

    def _remote_done(
        self,
        invocation: _IsolatedInvocation,
        task: asyncio.Task[subprocess.CompletedProcess[str]],
    ) -> None:
        try:
            invocation.result.set_result(task.result())
        except asyncio.CancelledError:
            invocation.result.cancel()
        except BaseException as exc:
            invocation.result.set_exception(exc)
        finally:
            self._finish(invocation)

    def _finish(self, invocation: _IsolatedInvocation) -> None:
        with self._lock:
            self._active.discard(invocation)
        if not invocation.cleanup.done():
            invocation.cleanup.set_result(None)

    def _shutdown_remote(self) -> None:
        try:
            if self._remote_runner is not None:
                self._remote_runner.close()
        finally:
            self._loop.stop()


PopenFactory = Callable[..., Any]


class _OwnedProcess:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: Any | None = None
        self._cancel_requested = False

    def attach(self, process: Any) -> None:
        with self._lock:
            self._process = process
            cancel_requested = self._cancel_requested
        if cancel_requested:
            self.kill()

    def kill(self) -> None:
        with self._lock:
            self._cancel_requested = True
            process = self._process
        if process is None:
            return
        _kill_process_group(process)


class ThreadedProcessRunner:
    def __init__(
        self,
        *,
        max_workers: int = 2,
        max_pending: int = 2,
        popen_factory: PopenFactory = subprocess.Popen,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if max_pending < 0:
            raise ValueError("max_pending must be >= 0")
        self._capacity = max_workers + max_pending
        self._popen_factory = popen_factory
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="computer-use-process",
        )
        self._lock = threading.Lock()
        self._active = 0
        self._closed = False
        self._owned: set[_OwnedProcess] = set()

    async def run(
        self,
        *args: str,
        env: Mapping[str, str] | None = None,
        timeout: float = 10.0,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        owned = self._reserve()
        try:
            future = self._executor.submit(
                self._run_blocking,
                owned,
                args,
                None if env is None else dict(env),
                timeout,
                input_text,
                check,
            )
        except BaseException:
            self._release(owned)
            raise
        future.add_done_callback(lambda _future: self._release(owned))
        wrapped = asyncio.wrap_future(future)
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            future.cancel()
            owned.kill()
            await _await_cancel_cleanup(wrapped, owned)
            raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            owned = tuple(self._owned)
        for process in owned:
            process.kill()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _reserve(self) -> _OwnedProcess:
        with self._lock:
            if self._closed:
                raise RuntimeError("process runner is closed")
            if self._active >= self._capacity:
                raise ProcessRunnerCapacityError("process runner capacity is exhausted")
            self._active += 1
            owned = _OwnedProcess()
            self._owned.add(owned)
            return owned

    def _release(self, owned: _OwnedProcess) -> None:
        with self._lock:
            if owned not in self._owned:
                return
            self._owned.remove(owned)
            self._active -= 1

    def _run_blocking(
        self,
        owned: _OwnedProcess,
        args: tuple[str, ...],
        env: dict[str, str] | None,
        timeout: float,
        input_text: str | None,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        process = self._popen_factory(
            args,
            env=env,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        owned.attach(process)
        input_bytes = input_text.encode() if input_text is not None else None
        try:
            stdout, stderr = process.communicate(input_bytes, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            owned.kill()
            process.communicate()
            raise TimeoutError from exc
        return _completed_result(args, process.returncode, stdout, stderr, check=check)


async def _finish_asyncio_process(process: asyncio.subprocess.Process) -> None:
    cleanup = asyncio.create_task(process.communicate())
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            _kill_process_group(process)
            continue
    with contextlib.suppress(Exception):
        cleanup.result()


async def _await_cancel_cleanup(
    wrapped: asyncio.Future[subprocess.CompletedProcess[str]],
    owned: _OwnedProcess,
) -> None:
    while not wrapped.done():
        try:
            await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            owned.kill()
            continue
        except Exception:
            break
    with contextlib.suppress(Exception, asyncio.CancelledError):
        wrapped.result()


async def _await_cleanup_ack(cleanup: ConcurrentFuture[None]) -> None:
    wrapped = asyncio.wrap_future(cleanup)
    while not wrapped.done():
        try:
            await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            continue
    wrapped.result()


def _kill_process_group(process: Any) -> None:
    pid = getattr(process, "pid", None)
    if os.name == "posix" and isinstance(pid, int) and not isinstance(pid, bool):
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
        else:
            return

    poll = getattr(process, "poll", None)
    returncode = poll() if callable(poll) else getattr(process, "returncode", None)
    if returncode is not None:
        return

    with contextlib.suppress(ProcessLookupError):
        process.kill()


def _completed_result(
    args: tuple[str, ...],
    returncode: int | None,
    stdout: bytes | str,
    stderr: bytes | str,
    *,
    check: bool,
) -> subprocess.CompletedProcess[str]:
    stdout_text = stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout
    stderr_text = stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr
    completed = subprocess.CompletedProcess(
        args,
        0 if returncode is None else returncode,
        stdout_text,
        stderr_text,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {completed.stderr}")
    return completed
