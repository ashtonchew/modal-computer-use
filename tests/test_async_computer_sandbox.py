from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from modal_computer_use import (
    AsyncComputerSandbox,
    AsyncDaemonClient,
    ComputerConfig,
    SessionStartupTiming,
)
from modal_computer_use.errors import SandboxAmbiguousError, SandboxUnavailableError
from modal_computer_use.state import APP_ID_TAG

REPO_ROOT = Path(__file__).resolve().parents[1]


class _AioCall:
    def __init__(
        self,
        name: str,
        function: Callable[..., Awaitable[object]],
        calls: list[str],
    ) -> None:
        self._name = name
        self._function = function
        self._calls = calls

    def __call__(self, *_args: object, **_kwargs: object) -> object:
        self._calls.append(f"{self._name}.sync")
        raise AssertionError(f"{self._name} must use Modal's native .aio operation")

    async def aio(self, *args: object, **kwargs: object) -> object:
        self._calls.append(f"{self._name}.aio")
        return await self._function(*args, **kwargs)


class _AioIteratorCall:
    def __init__(
        self,
        name: str,
        function: Callable[..., AsyncIterator[object]],
        calls: list[str],
    ) -> None:
        self._name = name
        self._function = function
        self._calls = calls

    def __call__(self, *_args: object, **_kwargs: object) -> object:
        self._calls.append(f"{self._name}.sync")
        raise AssertionError(f"{self._name} must use Modal's native .aio operation")

    def aio(self, *args: object, **kwargs: object) -> AsyncIterator[object]:
        self._calls.append(f"{self._name}.aio")
        return self._function(*args, **kwargs)


class _FakeApp:
    def __init__(self, calls: list[str]) -> None:
        self.app_id = "ap-async"
        self._tags: dict[str, str] = {}
        self.get_tags = _AioCall("app.get_tags", self._get_tags, calls)
        self.set_tags = _AioCall("app.set_tags", self._set_tags, calls)

    async def _get_tags(self) -> object:
        return dict(self._tags)

    async def _set_tags(self, tags: dict[str, str]) -> object:
        self._tags = dict(tags)
        return None


class _FakeConnectToken:
    url = "https://connect.invalid"
    token = "connect-token"  # noqa: S105 - inert test credential


class _FakeProcess:
    def __init__(self, calls: list[str], stdout: str) -> None:
        async def read() -> object:
            return stdout

        self.stdout = SimpleNamespace(
            read=_AioCall("process.stdout.read", read, calls)
        )


class _FakeSandbox:
    def __init__(
        self,
        calls: list[str],
        *,
        sandbox_id: str,
        name: str | None = None,
        run_id: str = "run-async",
    ) -> None:
        self.object_id = sandbox_id
        self.name = name
        self.calls = calls
        self.tags = {
            "computer-use": "true",
            APP_ID_TAG: "ap-async",
            "computer-use.run_id": run_id,
            "computer-use.config_hash": "a" * 16,
            "computer-use.created_at": "2026-08-02T00:00:00Z",
        }
        self.terminate_calls: list[bool] = []
        self.detach_calls = 0
        self.connect_token_error: BaseException | None = None
        self.daemon_bearer = "daemon-bearer"
        self.wait_until_ready = _AioCall(
            "sandbox.wait_until_ready", self._wait_until_ready, calls
        )
        self.create_connect_token = _AioCall(
            "sandbox.create_connect_token", self._create_connect_token, calls
        )
        self.get_tags = _AioCall("sandbox.get_tags", self._get_tags, calls)
        self.tunnels = _AioCall("sandbox.tunnels", self._tunnels, calls)
        self.exec = _AioCall("sandbox.exec", self._exec, calls)
        self.terminate = _AioCall("sandbox.terminate", self._terminate, calls)
        self.detach = _AioCall("sandbox.detach", self._detach, calls)

    async def _wait_until_ready(self, *, timeout: float) -> object:
        assert timeout > 0
        return None

    async def _create_connect_token(self, **kwargs: object) -> object:
        assert kwargs["port"] == 8080
        if self.connect_token_error is not None:
            raise self.connect_token_error
        return _FakeConnectToken()

    async def _get_tags(self) -> object:
        return dict(self.tags)

    async def _tunnels(self) -> object:
        return {8080: SimpleNamespace(url="https://daemon.invalid")}

    async def _exec(self, *_args: str, **_kwargs: object) -> object:
        return _FakeProcess(self.calls, self.daemon_bearer)

    async def _terminate(self, *, wait: bool = False) -> object:
        self.terminate_calls.append(wait)
        return None

    async def _detach(self) -> object:
        self.detach_calls += 1
        return None


class _FakeModalRuntime:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.app = _FakeApp(self.calls)
        self.created = _FakeSandbox(self.calls, sandbox_id="sb-created")
        self.by_id = _FakeSandbox(self.calls, sandbox_id="sb-by-id")
        self.by_name = _FakeSandbox(
            self.calls, sandbox_id="sb-by-name", name="desktop-one"
        )
        self.listed: list[_FakeSandbox] = []
        self.create_hook: Callable[..., Awaitable[_FakeSandbox]] | None = None
        self.App = SimpleNamespace(
            lookup=_AioCall("App.lookup", self._lookup_app, self.calls)
        )
        self.Sandbox = SimpleNamespace(
            create=_AioCall("Sandbox.create", self._create, self.calls),
            from_id=_AioCall("Sandbox.from_id", self._from_id, self.calls),
            from_name=_AioCall("Sandbox.from_name", self._from_name, self.calls),
            list=_AioIteratorCall("Sandbox.list", self._list, self.calls),
        )
        self.Probe = SimpleNamespace(with_tcp=lambda port: f"tcp:{port}")

    async def _lookup_app(self, app_name: str, **kwargs: object) -> object:
        assert app_name == "modal-computer-use"
        assert "create_if_missing" in kwargs
        return self.app

    async def _create(self, *_args: str, **_kwargs: object) -> _FakeSandbox:
        if self.create_hook is not None:
            return await self.create_hook()
        return self.created

    async def _from_id(self, sandbox_id: str) -> _FakeSandbox:
        assert sandbox_id == self.by_id.object_id
        return self.by_id

    async def _from_name(
        self,
        app_name: str,
        name: str,
        **_kwargs: object,
    ) -> _FakeSandbox:
        assert app_name == "modal-computer-use"
        assert name == self.by_name.name
        return self.by_name

    async def _list(self, **kwargs: object) -> AsyncIterator[object]:
        tags = kwargs.get("tags")
        for sandbox in self.listed:
            if isinstance(tags, dict) and any(
                sandbox.tags.get(str(key)) != value for key, value in tags.items()
            ):
                continue
            yield sandbox


def _install_runtime(monkeypatch: pytest.MonkeyPatch) -> _FakeModalRuntime:
    runtime = _FakeModalRuntime()
    monkeypatch.setitem(sys.modules, "modal", runtime)
    return runtime


def _connect_config() -> ComputerConfig:
    return ComputerConfig(run_id="run-async", ingress="connect", expose_vnc="off")


def _assert_modal_calls_are_native(calls: list[str]) -> None:
    assert calls
    assert not [call for call in calls if call.endswith(".sync")]


@pytest.mark.asyncio
async def test_create_is_lazy_native_async_ready_on_entry_and_owns_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _install_runtime(monkeypatch)
    readiness_calls: list[str] = []

    async def ready(client: AsyncDaemonClient, **_kwargs: object) -> None:
        readiness_calls.append(client.base_url)

    monkeypatch.setattr(AsyncDaemonClient, "wait_until_ready", ready)

    context = AsyncComputerSandbox.create(config=_connect_config(), image=object())

    assert not inspect.isawaitable(context)
    assert runtime.calls == []
    async with context as computer:
        assert computer.client.base_url == "https://connect.invalid"
        assert readiness_calls == ["https://connect.invalid"]
        assert runtime.created.terminate_calls == []

    assert runtime.created.terminate_calls == [True]
    assert runtime.created.detach_calls == 1
    _assert_modal_calls_are_native(runtime.calls)


@pytest.mark.asyncio
async def test_attach_is_lazy_ready_on_entry_and_never_terminates_remote_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _install_runtime(monkeypatch)
    readiness_calls: list[str] = []

    async def ready(client: AsyncDaemonClient, **_kwargs: object) -> None:
        readiness_calls.append(client.base_url)

    monkeypatch.setattr(AsyncDaemonClient, "wait_until_ready", ready)
    runtime.listed = [runtime.by_id]

    context = AsyncComputerSandbox.attach(sandbox_id="sb-by-id", ingress="connect")

    assert not inspect.isawaitable(context)
    assert runtime.calls == []
    async with context as computer:
        assert computer.client.base_url == "https://connect.invalid"
        assert readiness_calls == ["https://connect.invalid"]

    assert runtime.by_id.terminate_calls == []
    assert runtime.by_id.detach_calls == 1
    _assert_modal_calls_are_native(runtime.calls)


@pytest.mark.asyncio
async def test_attested_tunnel_uses_native_modal_bootstrap_and_a_final_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _install_runtime(monkeypatch)
    runtime.listed = [runtime.by_id]
    readiness_tokens: list[str | None] = []
    authorization_calls: list[tuple[str, str | None]] = []
    closed_tokens: list[str | None] = []

    async def ready(client: AsyncDaemonClient, **_kwargs: object) -> None:
        readiness_tokens.append(client.transport.token)

    async def authorize(
        client: AsyncDaemonClient,
        path: str,
        **_kwargs: object,
    ) -> object:
        authorization_calls.append((path, client.transport.token))
        return {"token": "attested-token"}

    async def close(client: AsyncDaemonClient) -> None:
        closed_tokens.append(client.transport.token)

    monkeypatch.setattr(AsyncDaemonClient, "wait_until_ready", ready)
    monkeypatch.setattr(AsyncDaemonClient, "post_json", authorize)
    monkeypatch.setattr(AsyncDaemonClient, "aclose", close)

    async with AsyncComputerSandbox.attach(
        sandbox_id="sb-by-id",
        ingress="attested-tunnel",
    ) as computer:
        assert computer.client.base_url == "https://daemon.invalid"
        assert computer.client.transport.token == "attested-token"  # noqa: S105

    assert readiness_tokens == ["daemon-bearer", "attested-token"]
    assert authorization_calls == [
        ("/v1/session/tunnel-authorize", "daemon-bearer")
    ]
    assert closed_tokens == ["daemon-bearer", "attested-token"]
    assert runtime.calls.count("sandbox.tunnels.aio") == 2
    assert "sandbox.exec.aio" in runtime.calls
    assert "process.stdout.read.aio" in runtime.calls
    assert runtime.by_id.detach_calls == 1
    assert runtime.by_id.terminate_calls == []
    _assert_modal_calls_are_native(runtime.calls)


@pytest.mark.asyncio
async def test_create_merges_app_tags_with_native_modal_tag_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _install_runtime(monkeypatch)
    runtime.app._tags = {"existing": "preserved"}

    async def ready(_client: AsyncDaemonClient, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(AsyncDaemonClient, "wait_until_ready", ready)

    async with AsyncComputerSandbox.create(
        config=_connect_config(),
        image=object(),
        app_tags={"documentation": "public"},
    ):
        pass

    assert runtime.app._tags == {
        "existing": "preserved",
        "documentation": "public",
    }
    assert runtime.calls.count("app.get_tags.aio") == 1
    assert runtime.calls.count("app.set_tags.aio") == 1
    _assert_modal_calls_are_native(runtime.calls)


@pytest.mark.asyncio
async def test_created_async_computer_exposes_an_eligible_session_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_runtime(monkeypatch)
    config = ComputerConfig(
        run_id="run-session",
        ingress="connect",
        expose_vnc="off",
        runtime={"modal_environment": "production", "modal_region": "us-west"},
    )

    async def ready(_client: AsyncDaemonClient, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(AsyncDaemonClient, "wait_until_ready", ready)

    async with AsyncComputerSandbox.create(config=config, image=object()) as computer:
        handle = computer.session_handle()
        metadata = computer.metadata()

        assert handle.sandbox_id == "sb-created"
        assert handle.session_id == metadata.tags["computer-use.session_id"]
        assert handle.app_name == "modal-computer-use"
        assert handle.modal_environment == "production"
        assert handle.requested_modal_region == "us-west"
        assert handle.ingress == "connect"
        assert handle.vnc_mode == "off"
        assert handle.config_hash == metadata.config_hash


@pytest.mark.asyncio
async def test_explicit_detach_transfers_created_sandbox_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _install_runtime(monkeypatch)

    async def ready(_client: AsyncDaemonClient, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(AsyncDaemonClient, "wait_until_ready", ready)

    async with AsyncComputerSandbox.create(
        config=_connect_config(), image=object()
    ) as computer:
        await computer.detach()

    assert runtime.created.terminate_calls == []
    assert runtime.created.detach_calls == 1


@pytest.mark.asyncio
async def test_detach_racing_context_exit_transfers_ownership_before_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _install_runtime(monkeypatch)
    detach_started = asyncio.Event()
    allow_detach = asyncio.Event()

    async def ready(_client: AsyncDaemonClient, **_kwargs: object) -> None:
        return None

    async def detach() -> object:
        runtime.created.detach_calls += 1
        detach_started.set()
        await allow_detach.wait()
        return None

    runtime.created.detach = _AioCall("sandbox.detach", detach, runtime.calls)
    monkeypatch.setattr(AsyncDaemonClient, "wait_until_ready", ready)
    context = AsyncComputerSandbox.create(config=_connect_config(), image=object())
    computer = await context.__aenter__()

    detach_task = asyncio.create_task(computer.detach())
    await detach_started.wait()
    exit_task = asyncio.create_task(context.__aexit__(None, None, None))
    await asyncio.sleep(0)
    allow_detach.set()
    await asyncio.gather(detach_task, exit_task)

    assert runtime.created.terminate_calls == []
    assert runtime.created.detach_calls == 1
    _assert_modal_calls_are_native(runtime.calls)


@pytest.mark.asyncio
async def test_detach_winning_terminate_race_prevents_remote_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _install_runtime(monkeypatch)
    detach_started = asyncio.Event()
    allow_detach = asyncio.Event()

    async def ready(_client: AsyncDaemonClient, **_kwargs: object) -> None:
        return None

    async def detach() -> object:
        runtime.created.detach_calls += 1
        detach_started.set()
        await allow_detach.wait()
        return None

    runtime.created.detach = _AioCall("sandbox.detach", detach, runtime.calls)
    monkeypatch.setattr(AsyncDaemonClient, "wait_until_ready", ready)
    context = AsyncComputerSandbox.create(config=_connect_config(), image=object())
    computer = await context.__aenter__()

    detach_task = asyncio.create_task(computer.detach())
    await detach_started.wait()
    terminate_task = asyncio.create_task(computer.terminate(wait=True))
    await asyncio.sleep(0)
    allow_detach.set()
    await detach_task

    with pytest.raises(SandboxUnavailableError, match="handle has been detached"):
        await terminate_task
    await context.__aexit__(None, None, None)

    assert runtime.created.terminate_calls == []
    assert runtime.created.detach_calls == 1
    _assert_modal_calls_are_native(runtime.calls)


@pytest.mark.asyncio
async def test_explicit_terminate_is_available_for_an_attached_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _install_runtime(monkeypatch)

    async def ready(_client: AsyncDaemonClient, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(AsyncDaemonClient, "wait_until_ready", ready)
    runtime.listed = [runtime.by_id]

    async with AsyncComputerSandbox.attach(
        sandbox_id="sb-by-id", ingress="connect"
    ) as computer:
        await computer.terminate(wait=True)

    assert runtime.by_id.terminate_calls == [True]
    assert runtime.by_id.detach_calls == 1


@pytest.mark.parametrize(
    "selectors",
    [
        {},
        {"sandbox_id": "sb-one", "name": "desktop-one"},
        {"sandbox_id": "sb-one", "run_id": "run-one"},
        {"name": "desktop-one", "run_id": "run-one"},
        {
            "sandbox_id": "sb-one",
            "name": "desktop-one",
            "run_id": "run-one",
        },
    ],
)
@pytest.mark.asyncio
async def test_attach_requires_exactly_one_selector_before_modal_io(
    monkeypatch: pytest.MonkeyPatch,
    selectors: dict[str, str],
) -> None:
    runtime = _install_runtime(monkeypatch)

    with pytest.raises(ValueError):
        async with AsyncComputerSandbox.attach(**selectors):
            raise AssertionError("unreachable")

    assert runtime.calls == []


@pytest.mark.parametrize("readiness_timeout", [0, -1, float("inf"), float("nan"), True])
@pytest.mark.asyncio
async def test_attach_rejects_invalid_readiness_timeout_before_modal_io(
    monkeypatch: pytest.MonkeyPatch,
    readiness_timeout: object,
) -> None:
    runtime = _install_runtime(monkeypatch)

    with pytest.raises(ValueError, match="positive finite number"):
        async with AsyncComputerSandbox.attach(
            sandbox_id="sb-by-id",
            readiness_timeout=readiness_timeout,  # type: ignore[arg-type]
        ):
            raise AssertionError("unreachable")

    assert runtime.calls == []


@pytest.mark.parametrize(
    ("selector", "expected_call", "target_attribute"),
    [
        ({"sandbox_id": "sb-by-id"}, "Sandbox.from_id.aio", "by_id"),
        ({"name": "desktop-one"}, "Sandbox.from_name.aio", "by_name"),
        ({"run_id": "run-async"}, "Sandbox.list.aio", "by_id"),
    ],
)
@pytest.mark.asyncio
async def test_attach_resolves_id_name_and_run_id_with_modal_aio(
    monkeypatch: pytest.MonkeyPatch,
    selector: dict[str, str],
    expected_call: str,
    target_attribute: str,
) -> None:
    runtime = _install_runtime(monkeypatch)
    target = getattr(runtime, target_attribute)
    runtime.listed = [target]

    async def ready(_client: AsyncDaemonClient, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(AsyncDaemonClient, "wait_until_ready", ready)

    async with AsyncComputerSandbox.attach(**selector, ingress="connect") as computer:
        assert computer.metadata().sandbox_id == target.object_id

    assert expected_call in runtime.calls
    assert target.detach_calls == 1
    assert target.terminate_calls == []
    _assert_modal_calls_are_native(runtime.calls)


@pytest.mark.asyncio
async def test_ambiguous_run_id_detaches_every_resolved_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _install_runtime(monkeypatch)
    first = _FakeSandbox(runtime.calls, sandbox_id="sb-first")
    second = _FakeSandbox(runtime.calls, sandbox_id="sb-second")
    runtime.listed = [first, second]

    with pytest.raises(SandboxAmbiguousError):
        async with AsyncComputerSandbox.attach(run_id="run-async", ingress="connect"):
            raise AssertionError("unreachable")

    assert first.detach_calls == 1
    assert second.detach_calls == 1
    assert first.terminate_calls == []
    assert second.terminate_calls == []
    _assert_modal_calls_are_native(runtime.calls)


@pytest.mark.asyncio
async def test_failed_creation_after_allocation_terminates_and_detaches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _install_runtime(monkeypatch)
    runtime.created.connect_token_error = RuntimeError("credential creation failed")

    with pytest.raises(RuntimeError, match="credential creation failed"):
        async with AsyncComputerSandbox.create(config=_connect_config(), image=object()):
            raise AssertionError("unreachable")

    assert runtime.created.terminate_calls == [True]
    assert runtime.created.detach_calls == 1
    _assert_modal_calls_are_native(runtime.calls)


@pytest.mark.asyncio
async def test_timing_failure_after_allocation_terminates_and_detaches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _install_runtime(monkeypatch)

    class FailingTiming(SessionStartupTiming):
        def mark(self, stage: str) -> None:
            if stage == "sandbox_registered":
                raise RuntimeError("timing sink failed")
            super().mark(stage)

    with pytest.raises(RuntimeError, match="timing sink failed"):
        async with AsyncComputerSandbox.create(
            config=_connect_config(),
            image=object(),
            timing=FailingTiming(),
        ):
            raise AssertionError("unreachable")

    assert runtime.created.terminate_calls == [True]
    assert runtime.created.detach_calls == 1
    assert "sandbox.wait_until_ready.aio" not in runtime.calls
    _assert_modal_calls_are_native(runtime.calls)


@pytest.mark.asyncio
async def test_failed_attachment_detaches_without_terminating_remote_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _install_runtime(monkeypatch)
    runtime.listed = [runtime.by_id]

    async def fail_readiness(_client: AsyncDaemonClient, **_kwargs: object) -> None:
        raise TimeoutError("daemon readiness failed")

    monkeypatch.setattr(AsyncDaemonClient, "wait_until_ready", fail_readiness)

    with pytest.raises(TimeoutError, match="daemon readiness failed"):
        async with AsyncComputerSandbox.attach(
            sandbox_id="sb-by-id", ingress="connect"
        ):
            raise AssertionError("unreachable")

    assert runtime.by_id.terminate_calls == []
    assert runtime.by_id.detach_calls == 1
    _assert_modal_calls_are_native(runtime.calls)


@pytest.mark.asyncio
async def test_attached_app_tag_rejection_detaches_without_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _install_runtime(monkeypatch)
    runtime.by_name.tags[APP_ID_TAG] = "ap-another-application"

    with pytest.raises(SandboxUnavailableError, match="no app-owned name=desktop-one"):
        async with AsyncComputerSandbox.attach(name="desktop-one", ingress="connect"):
            raise AssertionError("unreachable")

    assert runtime.by_name.detach_calls == 1
    assert runtime.by_name.terminate_calls == []
    assert "sandbox.create_connect_token.aio" not in runtime.calls
    _assert_modal_calls_are_native(runtime.calls)


@pytest.mark.asyncio
async def test_run_id_tag_read_failure_detaches_single_match_without_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _install_runtime(monkeypatch)
    runtime.listed = [runtime.by_id]

    async def fail_tags() -> object:
        raise ConnectionError("tag read failed")

    runtime.by_id.get_tags = _AioCall(
        "sandbox.get_tags", fail_tags, runtime.calls
    )

    with pytest.raises(ConnectionError, match="tag read failed"):
        async with AsyncComputerSandbox.attach(run_id="run-async", ingress="connect"):
            raise AssertionError("unreachable")

    assert runtime.by_id.detach_calls == 1
    assert runtime.by_id.terminate_calls == []
    assert "sandbox.wait_until_ready.aio" not in runtime.calls
    assert "sandbox.create_connect_token.aio" not in runtime.calls
    _assert_modal_calls_are_native(runtime.calls)


@pytest.mark.asyncio
async def test_run_id_tag_read_cancellation_detaches_single_match_without_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _install_runtime(monkeypatch)
    runtime.listed = [runtime.by_id]
    tag_read_started = asyncio.Event()

    async def blocked_tags() -> object:
        tag_read_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    runtime.by_id.get_tags = _AioCall(
        "sandbox.get_tags", blocked_tags, runtime.calls
    )

    async def enter() -> None:
        async with AsyncComputerSandbox.attach(run_id="run-async", ingress="connect"):
            raise AssertionError("unreachable")

    task = asyncio.create_task(enter())
    await tag_read_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    assert runtime.by_id.detach_calls == 1
    assert runtime.by_id.terminate_calls == []
    assert "sandbox.wait_until_ready.aio" not in runtime.calls
    assert "sandbox.create_connect_token.aio" not in runtime.calls
    _assert_modal_calls_are_native(runtime.calls)


@pytest.mark.asyncio
async def test_cancellation_during_allocation_waits_for_handle_then_cleans_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _install_runtime(monkeypatch)
    allocation_started = asyncio.Event()
    allow_allocation_to_return = asyncio.Event()

    async def allocate() -> _FakeSandbox:
        allocation_started.set()
        await allow_allocation_to_return.wait()
        return runtime.created

    runtime.create_hook = allocate

    async def enter() -> None:
        async with AsyncComputerSandbox.create(config=_connect_config(), image=object()):
            raise AssertionError("unreachable")

    task = asyncio.create_task(enter())
    await allocation_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    allow_allocation_to_return.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    assert runtime.created.terminate_calls == [True]
    assert runtime.created.detach_calls == 1
    _assert_modal_calls_are_native(runtime.calls)


@pytest.mark.asyncio
async def test_cancellation_after_allocation_finishes_owned_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _install_runtime(monkeypatch)
    readiness_started = asyncio.Event()

    async def blocked_readiness(_client: AsyncDaemonClient, **_kwargs: object) -> None:
        readiness_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(AsyncDaemonClient, "wait_until_ready", blocked_readiness)

    async def enter() -> None:
        async with AsyncComputerSandbox.create(config=_connect_config(), image=object()):
            raise AssertionError("unreachable")

    task = asyncio.create_task(enter())
    await readiness_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    assert runtime.created.terminate_calls == [True]
    assert runtime.created.detach_calls == 1
    _assert_modal_calls_are_native(runtime.calls)


@pytest.mark.asyncio
async def test_cancellation_during_context_exit_finishes_terminate_and_detach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _install_runtime(monkeypatch)
    termination_started = asyncio.Event()
    allow_termination = asyncio.Event()

    async def ready(_client: AsyncDaemonClient, **_kwargs: object) -> None:
        return None

    async def terminate(*, wait: bool = False) -> object:
        runtime.created.terminate_calls.append(wait)
        termination_started.set()
        await allow_termination.wait()
        return None

    runtime.created.terminate = _AioCall(
        "sandbox.terminate", terminate, runtime.calls
    )
    monkeypatch.setattr(AsyncDaemonClient, "wait_until_ready", ready)

    async def use_computer() -> None:
        async with AsyncComputerSandbox.create(config=_connect_config(), image=object()):
            pass

    task = asyncio.create_task(use_computer())
    await termination_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    allow_termination.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    assert runtime.created.terminate_calls == [True]
    assert runtime.created.detach_calls == 1
    _assert_modal_calls_are_native(runtime.calls)


@pytest.mark.asyncio
async def test_context_cleanup_errors_preserve_the_user_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _install_runtime(monkeypatch)
    body_error = RuntimeError("user workload failed")

    async def ready(_client: AsyncDaemonClient, **_kwargs: object) -> None:
        return None

    async def fail_termination(*, wait: bool = False) -> object:
        runtime.created.terminate_calls.append(wait)
        raise OSError("termination cleanup failed")

    runtime.created.terminate = _AioCall(
        "sandbox.terminate", fail_termination, runtime.calls
    )
    monkeypatch.setattr(AsyncDaemonClient, "wait_until_ready", ready)

    with pytest.raises(RuntimeError, match="user workload failed") as raised:
        async with AsyncComputerSandbox.create(config=_connect_config(), image=object()):
            raise body_error

    assert raised.value is body_error
    assert getattr(raised.value, "__notes__", []) == [
        "resource cleanup also failed: sandbox.terminate.aio (OSError)"
    ]
    assert runtime.created.terminate_calls == [True]
    assert runtime.created.detach_calls == 1
    _assert_modal_calls_are_native(runtime.calls)


@pytest.mark.asyncio
async def test_async_provisioning_never_hops_to_a_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _install_runtime(monkeypatch)

    async def ready(_client: AsyncDaemonClient, **_kwargs: object) -> None:
        return None

    async def prohibited_to_thread(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("async provisioning must not use asyncio.to_thread")

    monkeypatch.setattr(AsyncDaemonClient, "wait_until_ready", ready)
    monkeypatch.setattr(asyncio, "to_thread", prohibited_to_thread)

    async with AsyncComputerSandbox.create(config=_connect_config(), image=object()):
        pass

    _assert_modal_calls_are_native(runtime.calls)


def test_async_computer_sandbox_core_import_does_not_require_modal() -> None:
    code = """
import importlib.abc
import inspect
import sys

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.', 1)[0] == "modal":
            raise ImportError(f"blocked optional dependency: {fullname}")
        return None

sys.meta_path.insert(0, Blocker())
from modal_computer_use import AsyncComputerSandbox, ComputerConfig

context = AsyncComputerSandbox.create(
    config=ComputerConfig(ingress="connect", expose_vnc="off"),
    image=object(),
)
assert not inspect.isawaitable(context)
"""
    result = subprocess.run(  # noqa: S603 - fixed interpreter and source string
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
