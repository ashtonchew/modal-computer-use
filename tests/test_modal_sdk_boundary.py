from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from modal_computer_use import (
    ComputerConfig,
    ComputerSandbox,
    ComputerSandboxManager,
    DaemonClient,
    SandboxRef,
)
from modal_computer_use.config import BrowserConfig
from modal_computer_use.errors import (
    ConfigConflictError,
    SandboxAmbiguousError,
    SandboxUnavailableError,
)
from modal_computer_use.registry import SandboxRegistry
from modal_computer_use.sandbox import (
    MODAL_SNAPSHOT_RETENTION_SECONDS,
    ModalBenchmarkAllocationContext,
    ModalVolumeMount,
    _connect_token_parts,
    cleanup_modal_benchmark_run,
    create_modal_benchmark_computer,
    create_modal_benchmark_runner,
    create_modal_v2_tunnel_computer,
    modal_daemon_endpoint,
    modal_daemon_env,
    modal_sandbox_exec_once,
    modal_sandbox_exec_runner_from_id,
    probe_modal_candidate_placement,
    run_modal_daemon_command,
)
from modal_computer_use.state import APP_ID_TAG, compute_config_hash

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeProbe:
    calls: ClassVar[list[int]] = []

    @classmethod
    def with_tcp(cls, port: int) -> str:
        cls.calls.append(port)
        return f"tcp:{port}"


class FakeApp:
    lookups: ClassVar[list[tuple[str, bool, str | None]]] = []
    objects: ClassVar[list[FakeAppObject]] = []

    @classmethod
    def lookup(
        cls,
        app_name: str,
        *,
        create_if_missing: bool,
        environment_name: str | None = None,
    ) -> FakeAppObject:
        cls.lookups.append((app_name, create_if_missing, environment_name))
        app = FakeAppObject(app_name)
        cls.objects.append(app)
        return app


class FakeAppObject:
    def __init__(self, app_name: str) -> None:
        self.app_name = app_name
        self.app_id = f"ap-{app_name}"
        self._tags = {"existing": "app-tag"}
        self.set_tags_calls: list[dict[str, str]] = []

    def __eq__(self, value: object) -> bool:
        return value == f"app:{self.app_name}"

    def __repr__(self) -> str:
        return f"app:{self.app_name}"

    def set_tags(self, tags: dict[str, str]) -> None:
        self.set_tags_calls.append(tags)
        self._tags = tags

    def get_tags(self) -> dict[str, str]:
        return self._tags


class FakeConnectToken:
    url = "https://sandbox-connect.example"

    @property
    def token(self) -> str:
        return "connect-token"


class FakeSandboxObject:
    def __init__(
        self,
        *,
        sandbox_id: str = "sb-123",
        name: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> None:
        self.object_id = sandbox_id
        self.name = name
        self._tags = tags or {}
        self.set_tags_calls: list[dict[str, str]] = []
        self.wait_until_ready_calls: list[int] = []
        self.mount_image_calls: list[tuple[str, object]] = []
        self.snapshot_filesystem_calls: list[dict[str, object]] = []
        self.snapshot_directory_calls: list[dict[str, object]] = []
        self.reload_volumes_calls: list[int] = []
        self.terminate_wait_calls: list[bool] = []
        self.exec_calls: list[dict[str, object]] = []
        self.terminated = False

    def set_tags(self, tags: dict[str, str]) -> None:
        self.set_tags_calls.append(tags)
        self._tags = tags

    def get_tags(self) -> dict[str, str]:
        return self._tags

    def wait_until_ready(self, *, timeout: int) -> None:
        self.wait_until_ready_calls.append(timeout)

    def create_connect_token(
        self,
        *,
        user_metadata: dict[str, str],
        port: int,
    ) -> FakeConnectToken:
        assert user_metadata["sdk"] == "modal-computer-use"
        assert port == 8080
        return FakeConnectToken()

    def exec(
        self,
        *args: str,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> object:
        self.exec_calls.append({"args": args, "timeout": timeout, "env": env})
        return SimpleNamespace(
            args=args,
            timeout=timeout,
            env=env,
            returncode=0,
            stdout=SimpleNamespace(read=lambda: "bootstrap-token"),
        )

    def terminate(self, *, wait: bool = False) -> None:
        self.terminate_wait_calls.append(wait)
        self.terminated = True

    def tunnels(self) -> dict[int, object]:
        return {
            6080: SimpleNamespace(url="https://novnc.example"),
            8080: SimpleNamespace(url="https://daemon.example.modal.host"),
        }

    def snapshot_filesystem(self, timeout: int, *, ttl: int | None) -> object:
        self.snapshot_filesystem_calls.append({"timeout": timeout, "ttl": ttl})
        return SimpleNamespace(object_id="im-snapshot")

    def snapshot_directory(self, path: str, *, timeout: int, ttl: int | None) -> object:
        self.snapshot_directory_calls.append({"path": path, "timeout": timeout, "ttl": ttl})
        return SimpleNamespace(object_id="im-dir-snapshot", path=path)

    def reload_volumes(self, *, timeout: int) -> None:
        self.reload_volumes_calls.append(timeout)

    def mount_image(self, path: str, image: object) -> None:
        self.mount_image_calls.append((path, image))


class FakeSandbox:
    create_calls: ClassVar[list[tuple[tuple[str, ...], dict[str, object]]]] = []
    experimental_create_calls: ClassVar[list[tuple[tuple[str, ...], dict[str, object]]]] = []
    from_name_calls: ClassVar[list[tuple[str, str, str | None]]] = []
    from_id_calls: ClassVar[list[str]] = []
    list_calls: ClassVar[list[dict[str, str] | None]] = []
    experimental_list_calls: ClassVar[list[dict[str, str] | None]] = []
    created: ClassVar[FakeSandboxObject | None] = None
    listed: ClassVar[list[FakeSandboxObject]] = []
    experimental_listed: ClassVar[list[FakeSandboxObject]] = []
    from_name_result: ClassVar[FakeSandboxObject | None] = None
    from_name_error: ClassVar[Exception | None] = None
    from_id_result: ClassVar[FakeSandboxObject | None] = None

    @classmethod
    def create(cls, *args: str, **kwargs: object) -> FakeSandboxObject:
        cls.create_calls.append((args, kwargs))
        name = kwargs.get("name")
        tags = kwargs.get("tags")
        cls.created = FakeSandboxObject(
            name=name if isinstance(name, str) else None,
            tags=tags if isinstance(tags, dict) else None,
        )
        return cls.created

    @classmethod
    def _experimental_create(cls, *args: str, **kwargs: object) -> FakeSandboxObject:
        cls.experimental_create_calls.append((args, kwargs))
        name = kwargs.get("name")
        tags = kwargs.get("tags")
        cls.created = FakeSandboxObject(
            name=name if isinstance(name, str) else None,
            tags=tags if isinstance(tags, dict) else None,
        )
        return cls.created

    @classmethod
    def from_name(
        cls,
        app_name: str,
        name: str,
        *,
        environment_name: str | None = None,
    ) -> FakeSandboxObject:
        cls.from_name_calls.append((app_name, name, environment_name))
        if cls.from_name_error is not None:
            raise cls.from_name_error
        if cls.from_name_result is not None:
            return cls.from_name_result
        return FakeSandboxObject(name=name)

    @classmethod
    def from_id(cls, sandbox_id: str) -> FakeSandboxObject:
        cls.from_id_calls.append(sandbox_id)
        if cls.from_id_result is not None:
            return cls.from_id_result
        return FakeSandboxObject()

    @classmethod
    def list(
        cls,
        *,
        tags: dict[str, str] | None = None,
        app_id: str | None = None,
    ) -> list[FakeSandboxObject]:
        call: dict[str, object] = {}
        if app_id is not None:
            call["app_id"] = app_id
        if tags is not None:
            call["tags"] = tags
        cls.list_calls.append(call)
        return [
            sandbox
            for sandbox in cls.listed
            if not sandbox.terminated
            and (
                tags is None
                or all(sandbox.get_tags().get(key) == value for key, value in tags.items())
            )
        ]

    @classmethod
    def _experimental_list(
        cls,
        *,
        tags: dict[str, str] | None = None,
        app_id: str | None = None,
    ) -> list[FakeSandboxObject]:
        call: dict[str, object] = {}
        if app_id is not None:
            call["app_id"] = app_id
        if tags is not None:
            call["tags"] = tags
        cls.experimental_list_calls.append(call)
        return [
            sandbox
            for sandbox in cls.experimental_listed
            if not sandbox.terminated
            and (
                tags is None
                or all(sandbox.get_tags().get(key) == value for key, value in tags.items())
            )
        ]


def fake_modal() -> SimpleNamespace:
    FakeProbe.calls = []
    FakeApp.lookups = []
    FakeApp.objects = []
    FakeSandbox.create_calls = []
    FakeSandbox.experimental_create_calls = []
    FakeSandbox.from_name_calls = []
    FakeSandbox.from_id_calls = []
    FakeSandbox.list_calls = []
    FakeSandbox.experimental_list_calls = []
    FakeSandbox.created = None
    FakeSandbox.listed = []
    FakeSandbox.experimental_listed = []
    FakeSandbox.from_name_result = None
    FakeSandbox.from_name_error = None
    FakeSandbox.from_id_result = None
    return SimpleNamespace(App=FakeApp, Probe=FakeProbe, Sandbox=FakeSandbox)


def fake_sandbox_ref(sandbox_id: str = "sb-target") -> SandboxRef:
    return SandboxRef(
        sandbox_id=sandbox_id,
        app_name="computer-app",
        status="ready",
    )


def test_computer_sandbox_context_manager_terminates_modal_sandbox() -> None:
    sandbox = FakeSandboxObject()
    computer = ComputerSandbox(DaemonClient(base_url="http://127.0.0.1:1"), sandbox=sandbox)

    with computer as active:
        assert active is computer

    assert sandbox.terminated is True


def test_create_uses_current_modal_sandbox_contract(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    readiness_calls: list[float] = []
    monkeypatch.setattr(
        ComputerSandbox,
        "wait_until_ready",
        lambda self, timeout=120.0, interval=1.0: readiness_calls.append(timeout),
    )
    monkeypatch.setattr(
        "modal_computer_use.sandbox._attested_tunnel_parts",
        lambda sandbox, *, connect_base_url, connect_token: (
            "https://daemon.example.modal.host",
            "attested-token",
        ),
    )
    config = ComputerConfig(
        run_id="run-123",
        browser=BrowserConfig(kind="firefox"),
        expose_vnc="control",
    )

    computer = ComputerSandbox.create(
        config=config,
        image=object(),
        name="desktop-1",
        owner="alice",
        tags={"custom": "tag"},
        app_tags={"benchmark": "sdk-surfaces"},
        wait=True,
    )

    assert computer.metadata() is not None
    assert computer.metadata().vnc_url is None
    assert computer.debug_urls().vnc == "https://novnc.example"
    assert FakeProbe.calls == [8080]
    args, kwargs = FakeSandbox.create_calls[0]
    assert args == ("python", "-m", "modal_computer_use.daemon")
    assert kwargs["app"] == "app:modal-computer-use"
    assert kwargs["env"]["COMPUTER_USE_RUN_ID"] == "run-123"
    assert kwargs["env"]["COMPUTER_USE_ARTIFACTS_PERSISTENT"] == "false"
    assert kwargs["env"]["COMPUTER_USE_IMAGE_PROFILE"] == "standard"
    assert kwargs["env"]["COMPUTER_USE_DEFAULT_ACTION_TIMEOUT_MS"] == "5000"
    assert kwargs["env"]["COMPUTER_USE_MAX_ACTION_TIMEOUT_MS"] == "300000"
    assert kwargs["env"]["COMPUTER_USE_DESKTOP_WIDTH"] == "1024"
    assert kwargs["env"]["COMPUTER_USE_DESKTOP_HEIGHT"] == "768"
    assert kwargs["env"]["COMPUTER_USE_DESKTOP_DPI"] == "96"
    assert kwargs["env"]["COMPUTER_USE_POST_ACTION_DELAY_MS"] == "0"
    assert kwargs["env"]["COMPUTER_USE_DAEMON_HTTP_VERSION"] == "1.1"
    assert kwargs["env"]["COMPUTER_USE_INPUT_RATE_LIMIT_PER_SEC"] == "20"
    assert kwargs["env"]["COMPUTER_USE_MAX_BATCH_DURATION_MS"] == "30000"
    assert kwargs["env"]["COMPUTER_USE_TRUST_PRIVATE_CONNECT_PROXY"] == "false"
    assert kwargs["env"]["COMPUTER_USE_REQUIRE_CONNECT_USER"] == "false"
    assert kwargs["encrypted_ports"] == [8080, 6080]
    assert "h2_ports" not in kwargs
    assert kwargs["readiness_probe"] == "tcp:8080"
    assert "environment_variables" not in kwargs
    assert kwargs["tags"]["computer-use.run_id"] == "run-123"
    assert "computer-use.session_id" not in kwargs["tags"]
    assert kwargs["tags"]["computer-use"] == "true"
    assert kwargs["tags"]["computer-use.owner"] == "alice"
    assert kwargs["tags"][APP_ID_TAG] == "ap-modal-computer-use"
    assert kwargs["tags"]["computer-use.artifacts_dir"] == "/home/desktop/artifacts"
    assert "computer-use.created_at" in kwargs["tags"]
    assert kwargs["tags"]["custom"] == "tag"
    assert FakeSandbox.created is not None
    assert FakeSandbox.created.wait_until_ready_calls == [120]
    assert readiness_calls == [120, 120]
    assert computer.client.base_url == "https://daemon.example.modal.host"
    assert FakeSandbox.created.set_tags_calls == []
    assert FakeApp.objects[0].set_tags_calls == [
        {"existing": "app-tag", "benchmark": "sdk-surfaces"}
    ]
    assert computer.metadata().owner == "alice"
    assert computer.metadata().created_at is not None


def test_create_passes_modal_region_when_set(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    config = ComputerConfig(run_id="run-123", runtime={"modal_region": "us-west"})

    computer = ComputerSandbox.create(config=config, image=object(), wait=False)

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["region"] == "us-west"
    assert computer._requested_modal_region == "us-west"


@pytest.mark.parametrize(
    "field",
    [
        "app",
        "block_network",
        "encrypted_ports",
        "env",
        "h2_ports",
        "inbound_cidr_allowlist",
        "outbound_cidr_allowlist",
        "outbound_domain_allowlist",
        "readiness_probe",
    ],
)
def test_create_rejects_security_owned_sandbox_kwargs(field: str) -> None:
    with pytest.raises(ConfigConflictError, match="security-owned fields"):
        ComputerSandbox.create(image=object(), wait=False, **{field: object()})


def test_create_preserves_ordinary_modal_sandbox_kwargs(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())

    ComputerSandbox.create(image=object(), wait=False, cloud="aws")

    assert FakeSandbox.create_calls[0][1]["cloud"] == "aws"


def test_create_omits_modal_region_by_default(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())

    ComputerSandbox.create(config=ComputerConfig(run_id="run-123"), image=object(), wait=False)

    _, kwargs = FakeSandbox.create_calls[0]
    assert "region" not in kwargs


def test_candidate_v2_i6pn_target_uses_matched_named_image_and_private_network(
    monkeypatch,
) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    monkeypatch.setattr(
        "modal_computer_use.sandbox.named_image",
        lambda **_kwargs: "named-image",
    )
    monkeypatch.setattr(
        "modal_computer_use.sandbox._sandbox_i6pn_address",
        lambda _sandbox: "fdaa::1234",
    )
    config = ComputerConfig(
        run_id="candidate-v2",
        runtime={"modal_region": "us-west"},
        resources={"profile": "browser", "cpu": 4.0, "memory_mib": 8192},
        image={"source": "named", "revision": "a" * 40},
        browser={"kind": "chromium", "prewarm": True},
        ingress="tunnel",
    )

    computer = create_modal_benchmark_computer(
        config=config,
        backend="v2",
        transport="workspace-private-i6pn",
        cloud="aws",
        tags={"benchmark_arm": "v2-i6pn-direct-optimized"},
        wait=True,
    )

    args, kwargs = FakeSandbox.experimental_create_calls[0]
    assert args == ("python", "-m", "modal_computer_use.daemon")
    assert kwargs["image"] == "named-image"
    assert kwargs["cpu"] == 4.0
    assert kwargs["memory"] == 8192
    assert kwargs["cloud"] == "aws"
    assert kwargs["region"] == "us-west"
    assert kwargs["i6pn"] is True
    assert kwargs["encrypted_ports"] == []
    assert "readiness_probe" not in kwargs
    assert kwargs["env"]["COMPUTER_USE_DAEMON_HOST"] == "::"
    assert kwargs["env"]["COMPUTER_USE_TUNNEL_TOKEN"]
    assert len(kwargs["tags"]) == 10
    assert computer.client.base_url == "http://[fdaa::1234]:8080"


def test_candidate_v1_connect_uses_public_product_endpoint_without_tunnel(
    monkeypatch,
) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    monkeypatch.setattr(
        "modal_computer_use.sandbox.named_image",
        lambda **_kwargs: "named-image",
    )
    config = ComputerConfig(
        run_id="candidate-v1",
        runtime={"modal_region": "us-west"},
        resources={"profile": "browser", "cpu": 4.0, "memory_mib": 8192},
        image={"source": "named", "revision": "a" * 40},
        browser={"kind": "chromium", "prewarm": True},
        ingress="connect",
    )

    computer = create_modal_benchmark_computer(
        config=config,
        backend="v1",
        transport="connect-endpoint",
        cloud="aws",
        wait=False,
    )

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["cloud"] == "aws"
    assert kwargs["encrypted_ports"] == []
    assert "i6pn" not in kwargs
    assert kwargs["env"]["COMPUTER_USE_TRUST_PRIVATE_CONNECT_PROXY"] == "true"
    assert kwargs["env"]["COMPUTER_USE_REQUIRE_CONNECT_USER"] == "true"
    assert computer.client.base_url == "https://sandbox-connect.example"


def test_benchmark_v1_tunnel_binds_ipv4_for_modal_tcp_readiness(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    monkeypatch.setattr(
        "modal_computer_use.sandbox.named_image",
        lambda **_kwargs: "named-image",
    )
    config = ComputerConfig(
        run_id="frontier-v1-tunnel",
        runtime={"modal_region": "us-west"},
        resources={"profile": "browser", "cpu": 4.0, "memory_mib": 8192},
        image={"source": "named", "revision": "a" * 40},
        browser={"kind": "chromium", "prewarm": True},
        ingress="tunnel",
    )

    create_modal_benchmark_computer(
        config=config,
        backend="v1",
        transport="encrypted-tunnel",
        cloud="oci",
        wait=False,
    )

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["env"]["COMPUTER_USE_DAEMON_HOST"] == "0.0.0.0"  # noqa: S104
    assert kwargs["encrypted_ports"] == [8080]
    assert "i6pn" not in kwargs


def test_candidate_runner_caches_named_image_and_uses_v2_i6pn(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    monkeypatch.setattr(
        "modal_computer_use.sandbox.named_image",
        lambda **_kwargs: "named-image",
    )
    monkeypatch.setattr(
        "modal_computer_use.sandbox._sandbox_runtime_placement",
        lambda _sandbox: {"cloud": "aws", "region": "us-west-2"},
    )

    runner = create_modal_benchmark_runner(
        app_name="candidate-app",
        cloud="aws",
        region="us-west",
        image_revision="a" * 40,
        runner_label="modal-v2-candidate",
    )

    args, kwargs = FakeSandbox.experimental_create_calls[0]
    assert args == ("sleep", "infinity")
    assert kwargs["image"] == "named-image"
    assert kwargs["i6pn"] is True
    assert kwargs["cloud"] == "aws"
    assert kwargs["cpu"] == 1.0
    assert kwargs["memory"] == 1024
    assert FakeSandbox.created is not None
    assert FakeSandbox.created.wait_until_ready_calls == []
    assert runner.placement == {"cloud": "aws", "region": "us-west-2"}
    assert runner.terminate() is True


def test_optimized_frontier_v1_runner_uses_v1_create_without_i6pn(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    monkeypatch.setattr(
        "modal_computer_use.sandbox.named_image",
        lambda **_kwargs: "named-image",
    )
    monkeypatch.setattr(
        "modal_computer_use.sandbox._sandbox_runtime_placement",
        lambda _sandbox: {"cloud": "CLOUD_PROVIDER_OCI", "region": "us-phoenix-1"},
    )

    runner = create_modal_benchmark_runner(
        app_name="frontier-app",
        cloud="oci",
        region="us-west",
        image_revision="a" * 40,
        backend="v1",
        i6pn=False,
        cpu=0.5,
        memory_mib=512,
        runner_label="modal-optimized-frontier",
    )

    args, kwargs = FakeSandbox.create_calls[0]
    assert args == ("sleep", "infinity")
    assert kwargs["image"] == "named-image"
    assert kwargs["cloud"] == "oci"
    assert kwargs["cpu"] == 0.5
    assert kwargs["memory"] == 512
    assert "i6pn" not in kwargs
    assert FakeSandbox.experimental_create_calls == []
    assert runner.placement == {
        "cloud": "CLOUD_PROVIDER_OCI",
        "region": "us-phoenix-1",
    }


def test_candidate_constructor_terminates_target_on_keyboard_interrupt(monkeypatch) -> None:
    runtime = fake_modal()
    monkeypatch.setattr(
        FakeSandboxObject,
        "wait_until_ready",
        lambda _self, *, timeout: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    config = ComputerConfig(
        run_id="candidate-interrupt",
        runtime={"modal_region": "us-west"},
        resources={"profile": "browser", "cpu": 4.0, "memory_mib": 8192},
        image={"source": "named", "revision": "a" * 40},
        browser={"kind": "chromium", "prewarm": True},
        ingress="tunnel",
    )

    with pytest.raises(KeyboardInterrupt):
        create_modal_benchmark_computer(
            config=config,
            backend="v1",
            transport="encrypted-tunnel",
            cloud="aws",
            image=object(),
            wait=True,
            modal_runtime=runtime,
        )

    assert FakeSandbox.created is not None
    assert FakeSandbox.created.terminated is True
    assert FakeSandbox.created.terminate_wait_calls == [True]


def test_candidate_run_cleanup_terminates_only_exact_run_tags() -> None:
    runtime = fake_modal()
    target = FakeSandboxObject(
        sandbox_id="sb-target",
        tags={"computer-use.run_id": "run_exact-pilot-001"},
    )
    runner = FakeSandboxObject(sandbox_id="sb-runner", tags={"benchmark_run": "run_exact"})
    unrelated = FakeSandboxObject(
        sandbox_id="sb-unrelated",
        tags={"computer-use.run_id": "run_other-pilot-001"},
    )
    FakeSandbox.listed = [target, unrelated]
    FakeSandbox.experimental_listed = [runner, unrelated]

    result = cleanup_modal_benchmark_run(
        app_name="candidate-app",
        run_id="run_exact",
        modal_runtime=runtime,
    )

    assert result == {
        "matched_sandboxes": 2,
        "terminated_sandboxes": 2,
        "termination_failures": 0,
        "remaining_sandboxes": 0,
        "cleanup_succeeded": True,
    }
    assert target.terminated is True
    assert runner.terminated is True
    assert unrelated.terminated is False
    assert FakeSandbox.list_calls == [
        {"app_id": "ap-candidate-app"},
        {"app_id": "ap-candidate-app"},
    ]
    assert FakeSandbox.experimental_list_calls == [
        {"app_id": "ap-candidate-app"},
        {"app_id": "ap-candidate-app"},
    ]


def test_candidate_run_cleanup_can_attest_both_listing_inventories() -> None:
    runtime = fake_modal()
    FakeSandbox.listed = []
    FakeSandbox.experimental_listed = []

    result = cleanup_modal_benchmark_run(
        app_name="candidate-app",
        run_id="frontier-run",
        modal_runtime=runtime,
        include_inventory=True,
    )

    assert result["enumeration"] == {
        "before": {"list": 0, "_experimental_list": 0},
        "after": {"list": 0, "_experimental_list": 0},
        "apis": ["Sandbox.list", "Sandbox._experimental_list"],
    }


def test_candidate_run_cleanup_treats_disappeared_handles_as_absent() -> None:
    modal_exception = pytest.importorskip("modal.exception")
    runtime = fake_modal()
    matched = FakeSandboxObject(
        sandbox_id="sb-matched",
        tags={"computer-use.run_id": "run_exact-child"},
    )

    class DisappearedSandbox:
        object_id = "sb-disappeared"

        def get_tags(self) -> dict[str, str]:
            raise modal_exception.NotFoundError("sandbox disappeared after listing")

    class RaceSandbox:
        list_calls = 0

        @classmethod
        def list(cls, *, app_id: str) -> list[object]:
            assert app_id == "ap-candidate-app"
            cls.list_calls += 1
            return [matched] if cls.list_calls == 1 else [DisappearedSandbox()]

        @classmethod
        def _experimental_list(cls, *, app_id: str) -> list[object]:
            assert app_id == "ap-candidate-app"
            return []

    runtime.Sandbox = RaceSandbox

    result = cleanup_modal_benchmark_run(
        app_name="candidate-app",
        run_id="run_exact",
        modal_runtime=runtime,
        include_inventory=True,
    )

    assert result["cleanup_succeeded"] is True
    assert result["matched_sandboxes"] == 1
    assert result["terminated_sandboxes"] == 1
    assert result["remaining_sandboxes"] == 0
    assert matched.terminate_wait_calls == [True]
    assert result["enumeration"] == {
        "before": {"list": 1, "_experimental_list": 0},
        "after": {"list": 0, "_experimental_list": 0},
        "apis": ["Sandbox.list", "Sandbox._experimental_list"],
    }


def test_candidate_run_cleanup_propagates_other_tag_read_errors() -> None:
    runtime = fake_modal()

    class BrokenSandbox:
        object_id = "sb-broken"

        def get_tags(self) -> dict[str, str]:
            raise RuntimeError("tag service unavailable")

    class BrokenListing:
        @classmethod
        def list(cls, *, app_id: str) -> list[object]:
            return [BrokenSandbox()]

        @classmethod
        def _experimental_list(cls, *, app_id: str) -> list[object]:
            return []

    runtime.Sandbox = BrokenListing

    with pytest.raises(RuntimeError, match="tag service unavailable"):
        cleanup_modal_benchmark_run(
            app_name="candidate-app",
            run_id="run_exact",
            modal_runtime=runtime,
            include_inventory=True,
        )


def test_candidate_placement_probe_can_observe_unpinned_cloud_and_cleans_up(
    monkeypatch,
) -> None:
    runtime = fake_modal()
    monkeypatch.setitem(__import__("sys").modules, "modal", runtime)
    monkeypatch.setattr("modal_computer_use.sandbox.named_image", lambda **_kwargs: "image")
    monkeypatch.setattr(
        "modal_computer_use.sandbox._sandbox_runtime_placement",
        lambda _sandbox: {"cloud": "CLOUD_PROVIDER_AWS", "region": "us-west-2"},
    )

    result = probe_modal_candidate_placement(
        app_name="candidate-placement-probe",
        image_revision="a" * 40,
        run_id="run-placement",
        backend="v1",
        cloud=None,
        region="us-west",
        cpu=4.0,
        memory_mib=8192,
        i6pn=False,
    )

    assert result.status == "valid"
    assert result.run_id == "run-placement"
    assert result.requested_cloud is None
    assert result.actual_cloud == "CLOUD_PROVIDER_AWS"
    assert result.actual_region == "us-west-2"
    assert result.cleanup_succeeded is True
    assert FakeSandbox.create_calls[0][1].get("cloud") is None
    assert FakeSandbox.create_calls[0][1]["tags"] == {
        "computer-use.benchmark": "modal-v2-placement-probe",
        "computer-use.run_id": "run-placement-placement-probe-v1",
        APP_ID_TAG: "ap-candidate-placement-probe",
    }
    assert FakeSandbox.created is not None
    assert FakeSandbox.created.terminate_wait_calls == [True]


def test_candidate_throughput_tags_every_allocation_for_run_scoped_cleanup(
    monkeypatch,
) -> None:
    create_calls: list[dict[str, object]] = []
    terminate_calls: list[bool] = []

    async def read_placement() -> str:
        return '{"cloud": "CLOUD_PROVIDER_AWS", "region": "us-west-2"}'

    async def execute_placement(*_args: str, **_kwargs: object) -> object:
        return SimpleNamespace(stdout=SimpleNamespace(read=SimpleNamespace(aio=read_placement)))

    async def terminate(*, wait: bool) -> None:
        terminate_calls.append(wait)

    async def create(*_args: str, **kwargs: object) -> object:
        create_calls.append(kwargs)
        return SimpleNamespace(
            exec=SimpleNamespace(aio=execute_placement),
            terminate=SimpleNamespace(aio=terminate),
        )

    runtime = SimpleNamespace(
        Sandbox=SimpleNamespace(
            create=SimpleNamespace(aio=create),
            _experimental_create=SimpleNamespace(aio=create),
        )
    )
    monkeypatch.setitem(__import__("sys").modules, "modal", runtime)
    with pytest.raises(ValueError, match="non-empty"):
        ModalBenchmarkAllocationContext(
            app=SimpleNamespace(app_id="ap-throughput"),
            image=object(),
            run_id="run-123-throughput",
            cloud="",
            region="us-west",
            cpu=4.0,
            memory_mib=8192,
            benchmark_tag="modal-v2-candidate-throughput",
        )
    context = ModalBenchmarkAllocationContext(
        app=SimpleNamespace(app_id="ap-throughput"),
        image=object(),
        run_id="run-123-throughput",
        cloud="aws",
        region="us-west",
        cpu=4.0,
        memory_mib=8192,
        benchmark_tag="modal-v2-candidate-throughput",
    )

    result = asyncio.run(context.run_batch(backend="v1", concurrency=1))

    assert result["status"] == "valid"
    assert create_calls[0]["tags"] == {
        "computer-use.benchmark": "modal-v2-candidate-throughput",
        "computer-use.backend": "v1",
        "computer-use.run_id": "run-123-throughput-v1-1-0",
        APP_ID_TAG: "ap-throughput",
    }
    assert terminate_calls == [True]


def test_create_forwards_current_network_allowlist_arguments(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    config = ComputerConfig(
        run_id="run-123",
        network={
            "outbound_cidr_allowlist": ["10.0.0.0/8"],
            "outbound_domain_allowlist": ["api.openai.com"],
            "inbound_cidr_allowlist": ["203.0.113.0/24"],
        },
    )

    ComputerSandbox.create(config=config, image=object(), wait=False)

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["outbound_cidr_allowlist"] == ["10.0.0.0/8"]
    assert kwargs["outbound_domain_allowlist"] == ["api.openai.com"]
    assert kwargs["inbound_cidr_allowlist"] == ["203.0.113.0/24"]
    assert "cidr_allowlist" not in kwargs


def test_create_selects_named_image_without_inline_fallback(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    revision = "0123456789abcdef0123456789abcdef01234567"
    selected: list[dict[str, object]] = []

    def fake_named_image(**kwargs: object) -> object:
        selected.append(kwargs)
        return "named-image"

    monkeypatch.setattr("modal_computer_use.sandbox.named_image", fake_named_image)
    monkeypatch.setattr(
        "modal_computer_use.sandbox.default_image",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("inline fallback was used")),
    )
    config = ComputerConfig(
        run_id="run-123",
        resources={"profile": "browser"},
        browser={"kind": "chromium"},
        image={"source": "named", "revision": revision, "environment_name": "prod"},
    )

    ComputerSandbox.create(config=config, wait=False)

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["image"] == "named-image"
    assert kwargs["tags"]["computer-use.image_identity"] == (
        f"modal-computer-use-chromium:{revision}"
    )
    assert FakeApp.lookups == [("modal-computer-use", True, None)]
    assert selected == [
        {
            "revision": revision,
            "profile": "browser",
            "browser": "chromium",
            "environment_name": "prod",
        }
    ]


def test_create_uses_runtime_modal_environment_for_app_lookup(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    config = ComputerConfig(
        run_id="run-123",
        runtime={"modal_environment": "runtime-prod", "modal_region": "us-west"},
        image={
            "source": "named",
            "revision": "a" * 40,
            "environment_name": "image-prod",
        },
    )
    monkeypatch.setattr("modal_computer_use.sandbox.named_image", lambda **_kwargs: object())

    ComputerSandbox.create(config=config, wait=False)

    assert FakeApp.lookups == [("modal-computer-use", True, "runtime-prod")]
    _, create_kwargs = FakeSandbox.create_calls[0]
    assert "environment_name" not in create_kwargs


def test_create_preserves_reserved_computer_use_tags(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    config = ComputerConfig(run_id="run-123")

    computer = ComputerSandbox.create(
        config=config,
        image=object(),
        tags={
            "computer-use.run_id": "wrong",
            "computer-use.config_hash": "wrong",
            "custom": "tag",
        },
        wait=False,
    )

    assert FakeSandbox.created is not None
    _, create_kwargs = FakeSandbox.create_calls[0]
    applied_tags = create_kwargs["tags"]
    assert applied_tags["computer-use.run_id"] == "run-123"
    assert applied_tags["computer-use.config_hash"] == compute_config_hash(config)
    assert applied_tags["custom"] == "tag"
    assert computer.metadata().tags["computer-use.run_id"] == "run-123"


def test_connect_token_parts_extracts_query_token_from_url_only_response() -> None:
    base_url, token = _connect_token_parts(
        SimpleNamespace(
            url="https://sandbox-connect.example/path?_modal_connect_token=query-value#frag"
        )
    )

    assert base_url == "https://sandbox-connect.example/path"
    assert token == "query-value"  # noqa: S105 - synthetic connect-token fixture.


def test_connect_token_parts_prefers_explicit_token_and_strips_query() -> None:
    base_url, token = _connect_token_parts(
        SimpleNamespace(
            url="https://sandbox-connect.example/path?_modal_connect_token=query-value",
            token="explicit-value",
        )
    )

    assert base_url == "https://sandbox-connect.example/path"
    assert token == "explicit-value"  # noqa: S105 - synthetic connect-token fixture.


def test_modal_daemon_endpoint_inherits_client_details() -> None:
    computer = ComputerSandbox(
        DaemonClient(base_url="https://daemon.example.modal.host", token="attested-token"),
        sandbox=FakeSandboxObject(sandbox_id="sb-target"),
        metadata=fake_sandbox_ref(),
    )

    endpoint = modal_daemon_endpoint(computer, "inherited")

    assert endpoint.path == "inherited"
    assert endpoint.base_url == "https://daemon.example.modal.host"
    assert endpoint.token == "attested-token"  # noqa: S105 - synthetic token fixture.
    assert endpoint.target_sandbox_id == "sb-target"
    assert endpoint.execute_in_target is False


def test_modal_daemon_endpoint_creates_connect_token() -> None:
    computer = ComputerSandbox(
        DaemonClient(base_url="https://daemon.example.modal.host", token="attested-token"),
        sandbox=FakeSandboxObject(sandbox_id="sb-target"),
        metadata=fake_sandbox_ref(),
    )

    endpoint = modal_daemon_endpoint(computer, "connect")

    assert endpoint.path == "connect"
    assert endpoint.base_url == "https://sandbox-connect.example"
    assert endpoint.token == "connect-token"  # noqa: S105 - synthetic token fixture.
    assert endpoint.target_sandbox_id == "sb-target"
    assert endpoint.execute_in_target is False


def test_modal_daemon_endpoint_target_loopback_executes_in_target(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    computer = ComputerSandbox.create(
        config=ComputerConfig(ingress="connect"),
        image=object(),
        wait=False,
    )

    endpoint = modal_daemon_endpoint(computer, "target-loopback")

    assert endpoint.path == "target-loopback"
    assert endpoint.base_url == "http://127.0.0.1:8080"
    assert endpoint.token
    assert endpoint.target_sandbox_id == computer.metadata().sandbox_id
    assert endpoint.execute_in_target is True


def test_create_keeps_connect_and_loopback_bearers_distinct(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())

    computer = ComputerSandbox.create(
        config=ComputerConfig(ingress="connect"),
        image=object(),
        wait=False,
    )

    assert FakeSandbox.created is not None
    _, create_kwargs = FakeSandbox.create_calls[0]
    assert "COMPUTER_USE_LOCAL_TOKEN" not in create_kwargs["env"]
    daemon_bearer = create_kwargs["env"]["COMPUTER_USE_TUNNEL_TOKEN"]
    assert daemon_bearer
    assert daemon_bearer != "connect-token"
    endpoint = modal_daemon_endpoint(computer, "target-loopback")
    assert endpoint.token == daemon_bearer


def test_modal_daemon_endpoint_modal_paths_require_modal_sandbox() -> None:
    computer = ComputerSandbox(
        DaemonClient(base_url="https://daemon.example.modal.host", token="attested-token")
    )

    for path in ("connect", "target-loopback"):
        try:
            modal_daemon_endpoint(computer, path)  # type: ignore[arg-type]
        except SandboxUnavailableError as exc:
            assert f"{path} requires a Modal-backed sandbox" in str(exc)
        else:
            raise AssertionError(f"expected {path} without Modal sandbox to fail")


def test_modal_daemon_env_rejects_reserved_key_overrides() -> None:
    endpoint = modal_daemon_endpoint(
        ComputerSandbox(
            DaemonClient(base_url="https://daemon.example.modal.host", token="attested-token"),
            sandbox=FakeSandboxObject(sandbox_id="sb-target"),
            metadata=fake_sandbox_ref(),
        ),
        "inherited",
    )

    try:
        modal_daemon_env(
            endpoint,
            {"COMPUTER_USE_DAEMON_BASE_URL": "https://wrong.example", "WORKLOAD": "ok"},
        )
    except ValueError as exc:
        assert "COMPUTER_USE_DAEMON_BASE_URL" in str(exc)
    else:
        raise AssertionError("expected reserved daemon env override to fail")


def test_run_modal_daemon_command_uses_separate_runner_for_inherited_path() -> None:
    computer = ComputerSandbox(
        DaemonClient(base_url="https://daemon.example.modal.host", token="attested-token"),
        sandbox=FakeSandboxObject(sandbox_id="sb-target"),
        metadata=fake_sandbox_ref(),
    )
    calls: list[dict[str, object]] = []

    def fake_exec_once(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return SimpleNamespace(sandbox_id="sb-runner", returncode=0, stdout="ok", stderr="")

    result = run_modal_daemon_command(
        computer,
        ["python", "-m", "worker"],
        path="inherited",
        app_name="computer-app",
        modal_region="us-west",
        runner_name="runner",
        env={"WORKLOAD": "benchmark"},
        runner_cpu=0.5,
        runner_memory_mib=512,
        exec_timeout_seconds=60,
        exec_once=fake_exec_once,
    )

    assert result.sandbox_id == "sb-runner"
    assert calls == [
        {
            "command": ("python", "-m", "worker"),
            "app_name": "computer-app",
            "name": "runner",
            "region": "us-west",
            "env": {
                "COMPUTER_USE_DAEMON_BASE_URL": "https://daemon.example.modal.host",
                "COMPUTER_USE_DAEMON_RUNNER_PATH": "inherited",
                "COMPUTER_USE_DAEMON_TOKEN": "attested-token",
                "COMPUTER_USE_TARGET_SANDBOX_ID": "sb-target",
                "WORKLOAD": "benchmark",
            },
            "app_tags": None,
            "tags": {
                "computer-use.runner": "colocated",
                "computer-use.runner_path": "inherited",
            },
            "cpu": 0.5,
            "memory_mib": 512,
            "exec_timeout_seconds": 60,
        }
    ]


def test_run_modal_daemon_command_uses_target_sandbox_for_loopback_path(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    computer = ComputerSandbox.create(
        config=ComputerConfig(ingress="connect"),
        image=object(),
        wait=False,
    )
    assert FakeSandbox.created is not None
    calls: list[dict[str, object]] = []

    def fake_exec_in_target(sandbox, command, **kwargs):
        calls.append({"sandbox": sandbox, "command": command, **kwargs})
        return SimpleNamespace(sandbox_id="sb-target", returncode=0, stdout="ok", stderr="")

    result = run_modal_daemon_command(
        computer,
        ("python", "-m", "worker"),
        path="target-loopback",
        env={"WORKLOAD": "benchmark"},
        exec_timeout_seconds=60,
        exec_in_target=fake_exec_in_target,
    )

    assert result.sandbox_id == "sb-target"
    assert len(calls) == 1
    assert calls[0]["command"] == ("python", "-m", "worker")
    assert calls[0]["env"] == modal_daemon_env(
        modal_daemon_endpoint(computer, "target-loopback"),
        {"WORKLOAD": "benchmark"},
    )
    assert calls[0]["exec_timeout_seconds"] == 60


def test_create_passes_browser_profile_prewarm_and_gpu_env(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    config = ComputerConfig(
        run_id="run-123",
        browser=BrowserConfig(
            kind="chromium",
            prewarm=False,
            profile_dir="/home/desktop/browser-profile",
            launch_args=["--force-device-scale-factor=1"],
            open_url_on_start="https://example.com",
        ),
    )
    config.resources.profile = "browser-gpu"
    config.resources.gpu = "T4"

    ComputerSandbox.create(config=config, image=object(), wait=False)

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["gpu"] == "T4"
    assert kwargs["env"]["COMPUTER_USE_IMAGE_PROFILE"] == "browser-gpu"
    assert kwargs["env"]["COMPUTER_USE_BROWSER"] == "chromium"
    assert kwargs["env"]["COMPUTER_USE_BROWSER_PREWARM"] == "false"
    assert kwargs["env"]["COMPUTER_USE_BROWSER_PROFILE_DIR"] == ("/home/desktop/browser-profile")
    assert kwargs["env"]["COMPUTER_USE_BROWSER_LAUNCH_ARGS"] == (
        '["--force-device-scale-factor=1"]'
    )
    assert kwargs["env"]["COMPUTER_USE_BROWSER_OPEN_URL_ON_START"] == "https://example.com"
    assert kwargs["env"]["COMPUTER_USE_BROWSER_GPU_MODE"] == "auto"
    assert kwargs["env"]["COMPUTER_USE_VNC_PASSWORD"] == ""


def test_create_passes_explicit_browser_gpu_mode(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    config = ComputerConfig(
        run_id="run-123",
        browser=BrowserConfig(kind="chromium", gpu_mode="chromium-vulkan"),
    )
    config.resources.profile = "browser-gpu"
    config.resources.gpu = "T4"

    ComputerSandbox.create(config=config, image=object(), wait=False)

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["env"]["COMPUTER_USE_BROWSER_GPU_MODE"] == "chromium-vulkan"


def test_create_rejects_persistent_artifacts_without_volume_mount(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    config = ComputerConfig(storage={"persist_artifacts": True})

    try:
        ComputerSandbox.create(config=config, image=object(), wait=False)
    except ConfigConflictError as exc:
        assert "persist_artifacts=True requires a Volume" in str(exc)
    else:
        raise AssertionError("expected missing artifact volume mount to fail")


def test_create_marks_persistent_artifact_volume_mount(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    config = ComputerConfig(storage={"persist_artifacts": True})

    ComputerSandbox.create(
        config=config,
        image=object(),
        volumes={"/home/desktop": object()},
        wait=False,
    )

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["env"]["COMPUTER_USE_ARTIFACTS_PERSISTENT"] == "true"
    assert kwargs["env"]["COMPUTER_USE_ARTIFACTS_VOLUME_MOUNTED"] == "true"


def test_create_keeps_novnc_closed_by_default(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    monkeypatch.setattr(
        "modal_computer_use.sandbox._attested_tunnel_parts",
        lambda sandbox, *, connect_base_url, connect_token: (
            connect_base_url,
            "minted-token",
        ),
    )

    computer = ComputerSandbox.create(
        config=ComputerConfig(run_id="run-123"), image=object(), wait=False
    )

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["encrypted_ports"] == [8080]
    assert computer.client.transport.token == "minted-token"  # noqa: S105
    assert computer.client.transport.token != kwargs["env"]["COMPUTER_USE_TUNNEL_TOKEN"]


def test_create_connect_ingress_keeps_daemon_tunnel_closed(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())

    ComputerSandbox.create(
        config=ComputerConfig(run_id="run-123", ingress="connect"),
        image=object(),
        wait=False,
    )

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["encrypted_ports"] == []


def test_create_tunnel_ingress_uses_static_daemon_token(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())

    computer = ComputerSandbox.create(
        config=ComputerConfig(run_id="run-123", ingress="tunnel"),
        image=object(),
        wait=False,
    )

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["encrypted_ports"] == [8080]
    assert kwargs["env"]["COMPUTER_USE_TUNNEL_TOKEN"]
    assert computer.client.base_url == "https://daemon.example.modal.host"


def test_v2_benchmark_create_uses_encrypted_tunnel_and_application_auth(monkeypatch) -> None:
    runtime = fake_modal()
    monkeypatch.setitem(__import__("sys").modules, "modal", runtime)
    config = ComputerConfig(
        run_id="v2-run",
        ingress="tunnel",
        image={"source": "named", "revision": "a" * 40},
        resources={"profile": "browser", "cpu": 4.0, "memory_mib": 8192},
        runtime={"modal_region": "us-west", "timeout_seconds": 900},
        browser={"kind": "chromium"},
    )
    client = SimpleNamespace(
        base_url="https://daemon.example.modal.host",
        transport=SimpleNamespace(token=None),
        close=lambda: None,
    )

    computer = create_modal_v2_tunnel_computer(
        config=config,
        image=object(),
        wait=False,
        modal_runtime=runtime,
        client_factory=lambda **_kwargs: client,
    )

    assert FakeSandbox.create_calls == []
    assert len(FakeSandbox.experimental_create_calls) == 1
    args, kwargs = FakeSandbox.experimental_create_calls[0]
    assert args == ("python", "-m", "modal_computer_use.daemon")
    assert kwargs["encrypted_ports"] == [8080]
    assert "gpu" not in kwargs
    assert kwargs["region"] == "us-west"
    assert kwargs["env"]["COMPUTER_USE_TUNNEL_TOKEN"]
    assert kwargs["tags"]["computer-use.modal_backend"] == "v2"
    assert computer.client.base_url == "https://daemon.example.modal.host"


def test_create_h2_tunnel_uses_modal_h2_port(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    config = ComputerConfig(run_id="run-123", network={"daemon_http_version": "2"})

    ComputerSandbox.create(config=config, image=object(), wait=False)

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["encrypted_ports"] == []
    assert kwargs["h2_ports"] == [8080]
    assert kwargs["env"]["COMPUTER_USE_DAEMON_HTTP_VERSION"] == "2"


def test_create_passes_input_backend_to_daemon(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    config = ComputerConfig(run_id="run-123", actions={"input_backend": "xtest"})

    ComputerSandbox.create(config=config, image=object(), wait=False)

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["env"]["COMPUTER_USE_INPUT_BACKEND"] == "xtest"


def test_create_passes_subprocess_backend_to_daemon(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    config = ComputerConfig(
        run_id="run-123",
        actions={"subprocess_backend": "isolated-asyncio"},
    )

    ComputerSandbox.create(config=config, image=object(), wait=False)

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["env"]["COMPUTER_USE_SUBPROCESS_BACKEND"] == "isolated-asyncio"


def test_create_h2_with_vnc_keeps_novnc_on_encrypted_port(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    config = ComputerConfig(
        run_id="run-123",
        network={"daemon_http_version": "2"},
        expose_vnc="control",
    )

    ComputerSandbox.create(config=config, image=object(), wait=False)

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["encrypted_ports"] == [6080]
    assert kwargs["h2_ports"] == [8080]


def test_create_generates_vnc_password_without_exposing_it(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    config = ComputerConfig(run_id="run-123", expose_vnc="view_only")

    computer = ComputerSandbox.create(config=config, image=object(), wait=False)

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["env"]["COMPUTER_USE_VNC_MODE"] == "view_only"
    assert kwargs["env"]["COMPUTER_USE_VNC_PASSWORD"]
    assert "COMPUTER_USE_VNC_PASSWORD" not in computer.metadata().tags


def test_create_uses_configured_vnc_password(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    configured_value = "known-value"
    config = ComputerConfig(
        run_id="run-123",
        expose_vnc="view_only",
        vnc_password=configured_value,
    )

    ComputerSandbox.create(config=config, image=object(), wait=False)

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["env"]["COMPUTER_USE_VNC_PASSWORD"] == configured_value


def test_attach_by_name_uses_current_from_name_signature(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    FakeSandbox.from_name_result = FakeSandboxObject(
        name="desktop-1", tags={APP_ID_TAG: "ap-computer-app"}
    )

    ComputerSandbox.attach(app_name="computer-app", name="desktop-1")

    assert FakeSandbox.from_name_calls == [("computer-app", "desktop-1", None)]
    assert FakeApp.lookups == [("computer-app", False, None)]


def test_attach_attested_wait_false_never_exposes_bootstrap_token(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    FakeSandbox.from_name_result = FakeSandboxObject(
        name="desktop-1", tags={APP_ID_TAG: "ap-computer-app"}
    )
    monkeypatch.setattr(
        "modal_computer_use.sandbox._attested_tunnel_parts",
        lambda sandbox, *, connect_base_url, connect_token: (
            connect_base_url,
            "minted-token",
        ),
    )

    computer = ComputerSandbox.attach(
        app_name="computer-app",
        name="desktop-1",
        ingress="attested-tunnel",
        wait=False,
    )

    assert computer.client.transport.token == "minted-token"  # noqa: S105
    assert computer.client.transport.token != "bootstrap-token"  # noqa: S105


def test_attach_scopes_name_and_registry_lookups_to_modal_environment(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    FakeSandbox.from_name_result = FakeSandboxObject(
        name="desktop-1", tags={APP_ID_TAG: "ap-computer-app"}
    )

    ComputerSandbox.attach(
        app_name="computer-app",
        name="desktop-1",
        modal_environment="production",
    )

    assert FakeSandbox.from_name_calls == [("computer-app", "desktop-1", "production")]
    assert FakeApp.lookups == [("computer-app", False, "production")]


def test_attach_legacy_name_requires_explicit_compatibility(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    FakeSandbox.from_name_result = FakeSandboxObject(name="desktop-1")

    with pytest.raises(SandboxUnavailableError, match="app-owned"):
        ComputerSandbox.attach(app_name="computer-app", name="desktop-1")

    computer = ComputerSandbox.attach(
        app_name="computer-app",
        name="desktop-1",
        allow_legacy_unscoped=True,
    )

    assert computer.metadata() is not None
    assert computer.metadata().name == "desktop-1"


def test_attach_rejects_wrong_app_tag_even_in_legacy_mode(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    FakeSandbox.from_name_result = FakeSandboxObject(
        name="desktop-1", tags={APP_ID_TAG: "ap-other-app"}
    )

    with pytest.raises(SandboxUnavailableError, match="app-owned"):
        ComputerSandbox.attach(
            app_name="computer-app",
            name="desktop-1",
            allow_legacy_unscoped=True,
        )


def test_attach_wait_polls_daemon_when_requested(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    FakeSandbox.from_name_result = FakeSandboxObject(
        name="desktop-1", tags={APP_ID_TAG: "ap-computer-app"}
    )
    readiness_calls: list[float] = []
    monkeypatch.setattr(
        ComputerSandbox,
        "wait_until_ready",
        lambda self, timeout=120.0, interval=1.0: readiness_calls.append(timeout),
    )

    ComputerSandbox.attach(
        app_name="computer-app",
        name="desktop-1",
        ingress="connect",
        wait=True,
        readiness_timeout=7,
    )

    assert readiness_calls == [7]


def test_wait_until_ready_retries_transient_failures() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def get_json(self, path: str) -> dict[str, object]:
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("not listening yet")
            return {"ready": True, "errors": []}

    client = FakeClient()
    ComputerSandbox(client).wait_until_ready(timeout=1, interval=0)

    assert client.calls == 2


def test_wait_until_ready_timeout_reports_last_readyz_errors() -> None:
    class FakeClient:
        def get_json(self, path: str) -> dict[str, object]:
            return {"ready": False, "errors": ["window manager is not responding"]}

    try:
        ComputerSandbox(FakeClient()).wait_until_ready(timeout=0, interval=0)
    except TimeoutError as exc:
        assert "last /readyz reported 1 error" in str(exc)
        assert "window manager is not responding" not in str(exc)
    else:
        raise AssertionError("expected readiness timeout")


def test_wait_until_ready_timeout_reports_transient_error() -> None:
    class FakeClient:
        def get_json(self, path: str) -> dict[str, object]:
            raise ConnectionError("connection refused at https://fixture.invalid?detail=raw-value")

    try:
        ComputerSandbox(FakeClient()).wait_until_ready(timeout=0, interval=0)
    except TimeoutError as exc:
        assert "last error type: ConnectionError" in str(exc)
        assert "fixture.invalid" not in str(exc)
        assert "detail=raw-value" not in str(exc)
    else:
        raise AssertionError("expected readiness timeout")


def test_attach_by_run_id_lists_by_tags(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    FakeSandbox.listed = [
        FakeSandboxObject(
            tags={
                "computer-use.run_id": "run-123",
                "computer-use": "true",
                APP_ID_TAG: "ap-modal-computer-use",
            }
        )
    ]

    ComputerSandbox.attach(run_id="run-123")

    assert FakeSandbox.list_calls == [
        {
            "app_id": "ap-modal-computer-use",
            "tags": {
                "computer-use.run_id": "run-123",
                APP_ID_TAG: "ap-modal-computer-use",
            },
        }
    ]


def test_attach_closes_new_client_when_readiness_fails_without_terminating_target(
    monkeypatch,
) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    target = FakeSandboxObject(sandbox_id="sb-target", tags={APP_ID_TAG: "ap-modal-computer-use"})
    FakeSandbox.from_id_result = target
    FakeSandbox.listed = [target]
    clients: list[object] = []

    class FailingClient:
        def __init__(self, base_url: str, **kwargs: object) -> None:
            self.base_url = base_url
            self.transport = SimpleNamespace(token=kwargs.get("token"))
            self.closed = False
            clients.append(self)

        def get_json(self, path: str) -> dict[str, object]:
            raise ConnectionError("diagnostic endpoint failed")

        def close(self) -> None:
            self.closed = True

    ticks = iter((0.0, 1.0))
    monkeypatch.setattr("modal_computer_use.sandbox.DaemonClient", FailingClient)
    monkeypatch.setattr("modal_computer_use.sandbox.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("modal_computer_use.sandbox.time.sleep", lambda *_: None)

    with pytest.raises(TimeoutError):
        ComputerSandbox.attach(
            sandbox_id="sb-target",
            ingress="connect",
            wait=True,
            readiness_timeout=0.5,
        )

    assert len(clients) == 1
    assert clients[0].closed is True
    assert target.terminate_wait_calls == []


def test_attach_by_base_url_closes_new_client_when_readiness_fails(monkeypatch) -> None:
    clients: list[object] = []

    class FailingClient:
        def __init__(self, base_url: str, **kwargs: object) -> None:
            self.base_url = base_url
            self.closed = False
            clients.append(self)

        def get_json(self, path: str) -> dict[str, object]:
            raise ConnectionError("diagnostic endpoint failed")

        def close(self) -> None:
            self.closed = True

    ticks = iter((0.0, 1.0))
    monkeypatch.setattr("modal_computer_use.sandbox.DaemonClient", FailingClient)
    monkeypatch.setattr("modal_computer_use.sandbox.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("modal_computer_use.sandbox.time.sleep", lambda *_: None)

    with pytest.raises(TimeoutError):
        ComputerSandbox.attach(
            base_url="https://fixture.invalid",
            wait=True,
            readiness_timeout=0.5,
        )

    assert len(clients) == 1
    assert clients[0].closed is True


def test_attach_preserves_readiness_timeout_when_client_cleanup_fails(
    monkeypatch,
) -> None:
    class CleanupFailure(RuntimeError):
        pass

    class FailingClient:
        def __init__(self, base_url: str, **kwargs: object) -> None:
            self.base_url = base_url

        def get_json(self, path: str) -> dict[str, object]:
            raise ConnectionError("diagnostic endpoint failed")

        def close(self) -> None:
            raise CleanupFailure("cleanup diagnostic payload")

    ticks = iter((0.0, 1.0))
    monkeypatch.setattr("modal_computer_use.sandbox.DaemonClient", FailingClient)
    monkeypatch.setattr("modal_computer_use.sandbox.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("modal_computer_use.sandbox.time.sleep", lambda *_: None)

    with pytest.raises(TimeoutError) as raised:
        ComputerSandbox.attach(
            base_url="https://fixture.invalid",
            wait=True,
            readiness_timeout=0.5,
        )

    notes = getattr(raised.value, "__notes__", [])
    assert notes == ["readiness cleanup also failed: client.close (CleanupFailure)"]
    assert "cleanup diagnostic payload" not in " ".join(notes)


def test_attach_by_run_id_ambiguous_matches_fail(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    FakeSandbox.listed = [
        FakeSandboxObject(
            tags={
                "computer-use.run_id": "run-123",
                "computer-use": "true",
                APP_ID_TAG: "ap-modal-computer-use",
            }
        ),
        FakeSandboxObject(
            tags={
                "computer-use.run_id": "run-123",
                "computer-use": "true",
                APP_ID_TAG: "ap-modal-computer-use",
            }
        ),
    ]

    try:
        ComputerSandbox.attach(run_id="run-123")
    except SandboxAmbiguousError as exc:
        assert "multiple matching run_id=run-123" in str(exc)
    else:
        raise AssertionError("expected ambiguous run_id attach to fail")


def test_attach_metadata_includes_safe_tags_run_id_and_config_hash(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    config = ComputerConfig(run_id="run-123")
    config_hash = compute_config_hash(config)
    FakeSandbox.listed = [
        FakeSandboxObject(
            name="desktop-1",
            tags={
                "computer-use": "true",
                "computer-use.run_id": "run-123",
                "computer-use.config_hash": config_hash,
                "computer-use.owner": "alice",
                "computer-use.created_at": "2026-05-12T12:00:00Z",
                APP_ID_TAG: "ap-computer-app",
                "computer-use.artifacts_dir": "/home/desktop/artifacts",
            },
        )
    ]

    computer = ComputerSandbox.attach(run_id="run-123", app_name="computer-app")

    metadata = computer.metadata()
    assert metadata is not None
    assert metadata.sandbox_id == "sb-123"
    assert metadata.app_name == "computer-app"
    assert metadata.name == "desktop-1"
    assert metadata.run_id == "run-123"
    assert metadata.owner == "alice"
    assert metadata.created_at == datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    assert metadata.config_hash == config_hash
    assert metadata.tags["computer-use"] == "true"
    assert metadata.artifacts_dir == "/home/desktop/artifacts"
    assert metadata.vnc_url is None


def test_modal_sandbox_exec_runner_attaches_by_id(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())

    runner = modal_sandbox_exec_runner_from_id("sb-123")
    process = runner(("xdotool", "mousemove", "24", "24"), 10)

    assert FakeSandbox.from_id_calls == ["sb-123"]
    assert process.args == ("xdotool", "mousemove", "24", "24")
    assert process.timeout == 10
    assert process.returncode == 0


def test_modal_sandbox_exec_once_creates_ephemeral_runner(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())

    result = modal_sandbox_exec_once(
        ("python", "-c", "print('ok')"),
        app_name="computer-app",
        name="runner",
        image=object(),
        region="us-west",
        env={"TOKEN": "secret"},
        app_tags={"benchmark": "modal-colocated-client"},
        tags={"role": "runner"},
        cpu=0.5,
        memory_mib=512,
    )

    args, kwargs = FakeSandbox.create_calls[0]
    assert args == ("sleep", "infinity")
    assert kwargs["app"] == "app:computer-app"
    assert kwargs["region"] == "us-west"
    assert kwargs["encrypted_ports"] == []
    assert kwargs["cpu"] == 0.5
    assert kwargs["memory"] == 512
    assert kwargs["tags"] == {"role": "runner", APP_ID_TAG: "ap-computer-app"}
    assert FakeSandbox.created is not None
    assert FakeSandbox.created.set_tags_calls == []
    assert FakeSandbox.created.exec_calls == [
        {
            "args": ("python", "-c", "print('ok')"),
            "timeout": 240,
            "env": {"TOKEN": "secret"},
        }
    ]
    assert FakeSandbox.created.terminated is True
    assert FakeSandbox.created.terminate_wait_calls == [True]
    assert result.returncode == 0


def test_modal_sandbox_exec_once_keeps_runner_alive_for_exec_timeout(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())

    modal_sandbox_exec_once(
        ("python", "-c", "print('ok')"),
        app_name="computer-app",
        image=object(),
        timeout_seconds=300,
        exec_timeout_seconds=450,
    )

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["timeout"] >= 450


def test_modal_sandbox_exec_once_preserves_command_failure_when_cleanup_fails(
    monkeypatch,
) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())

    class CommandFailure(RuntimeError):
        pass

    class CleanupFailure(RuntimeError):
        pass

    def fail_command(self: FakeSandboxObject, *args: str, **kwargs: object) -> object:
        raise CommandFailure("command failed")

    def fail_cleanup(self: FakeSandboxObject, *, wait: bool = False) -> None:
        raise CleanupFailure("cleanup diagnostic payload")

    monkeypatch.setattr(FakeSandboxObject, "exec", fail_command)
    monkeypatch.setattr(FakeSandboxObject, "terminate", fail_cleanup)

    with pytest.raises(CommandFailure) as raised:
        modal_sandbox_exec_once(
            ("python", "-c", "raise SystemExit(1)"),
            app_name="computer-app",
            image=object(),
        )

    notes = getattr(raised.value, "__notes__", [])
    assert notes == ["runner cleanup also failed: terminate (CleanupFailure)"]
    assert "cleanup diagnostic payload" not in " ".join(notes)


def test_registry_lists_sandboxes_with_tags(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    FakeSandbox.listed = [
        FakeSandboxObject(
            name="desktop-1",
            tags={
                "computer-use": "true",
                "computer-use.run_id": "run-123",
                "computer-use.config_hash": "abc",
                "computer-use.owner": "alice",
                "computer-use.created_at": "2026-05-12T12:00:00Z",
                APP_ID_TAG: "ap-computer-app",
            },
        )
    ]

    refs = SandboxRegistry(app_name="computer-app").list()

    assert FakeSandbox.list_calls == [
        {
            "app_id": "ap-computer-app",
            "tags": {APP_ID_TAG: "ap-computer-app"},
        }
    ]
    assert refs[0].name == "desktop-1"
    assert refs[0].run_id == "run-123"
    assert refs[0].owner == "alice"
    assert refs[0].created_at == datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    assert refs[0].config_hash == "abc"


def test_registry_invalid_created_at_does_not_crash_list(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    FakeSandbox.listed = [
        FakeSandboxObject(
            tags={
                "computer-use": "true",
                "computer-use.created_at": "not-a-date",
                APP_ID_TAG: "ap-computer-app",
            },
        )
    ]

    refs = SandboxRegistry(app_name="computer-app").list()

    assert refs[0].created_at is None
    assert refs[0].tags["computer-use.created_at"] == "not-a-date"


def test_registry_list_older_than_filters_valid_created_at(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    FakeSandbox.listed = [
        FakeSandboxObject(
            sandbox_id="old",
            tags={
                "computer-use": "true",
                "computer-use.created_at": "2026-05-12T10:00:00Z",
                APP_ID_TAG: "ap-computer-app",
            },
        ),
        FakeSandboxObject(
            sandbox_id="new",
            tags={
                "computer-use": "true",
                "computer-use.created_at": "2026-05-12T12:00:00Z",
                APP_ID_TAG: "ap-computer-app",
            },
        ),
        FakeSandboxObject(
            sandbox_id="unknown",
            tags={"computer-use": "true", APP_ID_TAG: "ap-computer-app"},
        ),
    ]

    refs = SandboxRegistry(app_name="computer-app").list_older_than(
        datetime(2026, 5, 12, 11, 0, tzinfo=UTC)
    )

    assert [ref.sandbox_id for ref in refs] == ["old"]


def test_manager_cleanup_expired_dry_run_does_not_terminate(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    old = FakeSandboxObject(
        sandbox_id="old",
        tags={
            "computer-use": "true",
            "computer-use.owner": "alice",
            "computer-use.run_id": "run-old",
            "computer-use.created_at": "2026-05-12T10:00:00Z",
            APP_ID_TAG: "ap-computer-app",
        },
    )
    new = FakeSandboxObject(
        sandbox_id="new",
        tags={
            "computer-use": "true",
            "computer-use.owner": "alice",
            "computer-use.created_at": "2026-05-12T12:00:00Z",
            APP_ID_TAG: "ap-computer-app",
        },
    )
    FakeSandbox.listed = [old, new]

    result = ComputerSandboxManager(app_name="computer-app").cleanup_expired(
        ttl_seconds=3600,
        owner="alice",
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    assert FakeSandbox.list_calls == [
        {
            "app_id": "ap-computer-app",
            "tags": {
                "computer-use.owner": "alice",
                APP_ID_TAG: "ap-computer-app",
            },
        }
    ]
    assert result.dry_run is True
    assert result.inspected_count == 2
    assert result.matched_count == 1
    assert result.terminated_count == 0
    assert result.candidates[0].sandbox_id == "old"
    assert result.candidates[0].reason == "expired"
    assert result.skipped[0].sandbox_id == "new"
    assert result.skipped[0].reason == "not_expired"
    assert old.terminated is False
    assert new.terminated is False


def test_manager_cleanup_expired_execute_terminates_only_expired(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    old = FakeSandboxObject(
        sandbox_id="old",
        tags={
            "computer-use": "true",
            "computer-use.created_at": "2026-05-12T10:00:00Z",
            APP_ID_TAG: "ap-computer-app",
        },
    )
    new = FakeSandboxObject(
        sandbox_id="new",
        tags={
            "computer-use": "true",
            "computer-use.created_at": "2026-05-12T11:30:00Z",
            APP_ID_TAG: "ap-computer-app",
        },
    )
    FakeSandbox.listed = [old, new]

    result = ComputerSandboxManager(app_name="computer-app").cleanup_expired(
        ttl_seconds=3600,
        dry_run=False,
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    assert result.dry_run is False
    assert result.matched_count == 1
    assert result.terminated_count == 1
    assert result.candidates[0].status == "terminated"
    assert old.terminated is True
    assert new.terminated is False


def test_manager_cleanup_never_terminates_unscoped_legacy_sandboxes(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    scoped = FakeSandboxObject(
        sandbox_id="scoped",
        tags={
            "computer-use": "true",
            "computer-use.created_at": "2026-05-12T10:00:00Z",
            APP_ID_TAG: "ap-computer-app",
        },
    )
    legacy = FakeSandboxObject(
        sandbox_id="legacy",
        tags={
            "computer-use": "true",
            "computer-use.created_at": "2026-05-12T10:00:00Z",
        },
    )
    FakeSandbox.listed = [scoped, legacy]

    result = ComputerSandboxManager(app_name="computer-app").cleanup_expired(
        ttl_seconds=3600,
        dry_run=False,
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    assert result.inspected_count == 1
    assert scoped.terminated is True
    assert legacy.terminated is False


def test_manager_cleanup_skips_missing_and_invalid_created_at(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    missing = FakeSandboxObject(
        sandbox_id="missing",
        tags={"computer-use": "true", APP_ID_TAG: "ap-computer-app"},
    )
    invalid = FakeSandboxObject(
        sandbox_id="invalid",
        tags={
            "computer-use": "true",
            "computer-use.created_at": "not-a-date",
            APP_ID_TAG: "ap-computer-app",
        },
    )
    FakeSandbox.listed = [missing, invalid]

    result = ComputerSandboxManager(app_name="computer-app").cleanup_expired(
        ttl_seconds=3600,
        dry_run=False,
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    assert result.matched_count == 0
    assert result.terminated_count == 0
    assert [(item.sandbox_id, item.reason) for item in result.skipped] == [
        ("missing", "missing_created_at"),
        ("invalid", "invalid_created_at"),
    ]
    assert missing.terminated is False
    assert invalid.terminated is False


def test_manager_terminate_uses_modal_sandbox_id_without_connect_token(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    sandbox = FakeSandboxObject(sandbox_id="sb-terminate", tags={APP_ID_TAG: "ap-computer-app"})
    FakeSandbox.from_id_result = sandbox
    FakeSandbox.listed = [sandbox]

    ComputerSandboxManager(app_name="computer-app").terminate("sb-terminate")

    assert FakeSandbox.from_id_calls == ["sb-terminate"]
    assert sandbox.terminated is True


def test_snapshot_filesystem_delegates_to_modal_sandbox(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())

    computer = ComputerSandbox.create(
        config=ComputerConfig(run_id="run-123"),
        image=object(),
        wait=False,
    )

    snapshot = computer.snapshot_filesystem()

    assert snapshot.object_id == "im-snapshot"
    assert FakeSandbox.created is not None
    assert FakeSandbox.created.snapshot_filesystem_calls == [
        {"timeout": 55, "ttl": MODAL_SNAPSHOT_RETENTION_SECONDS}
    ]


def test_snapshot_filesystem_requires_modal_backing() -> None:
    computer = ComputerSandbox.local(token="dev")

    try:
        computer.snapshot_filesystem()
    except SandboxUnavailableError as exc:
        assert "filesystem snapshots require" in str(exc)
    else:
        raise AssertionError("expected local snapshot helper to fail")


def test_snapshot_directory_and_mount_image_delegate_to_modal_sandbox(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())

    computer = ComputerSandbox.create(
        config=ComputerConfig(run_id="run-123"),
        image=object(),
        wait=False,
    )

    snapshot = computer.snapshot_directory(
        "/home/desktop/artifacts/snapshots",
        timeout=90,
        ttl=None,
    )
    computer.mount_image("/home/desktop/artifacts/snapshots", snapshot)

    assert snapshot.object_id == "im-dir-snapshot"
    assert snapshot.path == "/home/desktop/artifacts/snapshots"
    assert FakeSandbox.created is not None
    assert FakeSandbox.created.mount_image_calls == [
        ("/home/desktop/artifacts/snapshots", snapshot)
    ]
    assert FakeSandbox.created.snapshot_directory_calls == [
        {
            "path": "/home/desktop/artifacts/snapshots",
            "timeout": 90,
            "ttl": None,
        }
    ]


def test_snapshot_and_volume_reload_reject_invalid_timeouts(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    computer = ComputerSandbox.create(
        config=ComputerConfig(run_id="run-123"),
        image=object(),
        wait=False,
    )

    for operation in (
        lambda: computer.snapshot_filesystem(timeout=0),
        lambda: computer.snapshot_directory("/project", timeout=0),
        lambda: computer.reload_volumes(timeout=0),
    ):
        with pytest.raises(ValueError, match="timeout must be positive"):
            operation()
    with pytest.raises(ValueError, match="ttl must be positive"):
        computer.snapshot_filesystem(ttl=0)


def test_reload_volumes_blocks_with_explicit_timeout(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    computer = ComputerSandbox.create(
        config=ComputerConfig(run_id="run-123"),
        image=object(),
        wait=False,
    )

    computer.reload_volumes(timeout=75)

    assert FakeSandbox.created is not None
    assert FakeSandbox.created.reload_volumes_calls == [75]


def test_reload_volumes_requires_modal_backing() -> None:
    computer = ComputerSandbox.local(token="dev")

    with pytest.raises(SandboxUnavailableError, match="Volume reload requires"):
        computer.reload_volumes()


def test_terminate_can_wait_for_modal_shutdown(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    computer = ComputerSandbox.create(
        config=ComputerConfig(run_id="run-123"),
        image=object(),
        wait=False,
    )

    computer.terminate(wait=True)

    assert FakeSandbox.created is not None
    assert FakeSandbox.created.terminate_wait_calls == [True]


def test_modal_volume_mount_applies_opt_in_options(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())

    class FakeVolume:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def with_mount_options(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            return "configured-volume"

    volume = FakeVolume()
    ComputerSandbox.create(
        config=ComputerConfig(run_id="run-123"),
        image=object(),
        volumes={
            "/data": ModalVolumeMount(
                volume=volume,
                read_only=True,
                sub_path="/users/alice",
            )
        },
        wait=False,
    )

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["volumes"] == {"/data": "configured-volume"}
    assert volume.calls == [{"read_only": True, "sub_path": "/users/alice"}]


def test_modal_volume_mount_rejects_unsupported_volume_options(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())

    with pytest.raises(ConfigConflictError, match=r"Volume\.with_mount_options"):
        ComputerSandbox.create(
            config=ComputerConfig(run_id="run-123"),
            image=object(),
            volumes={"/data": ModalVolumeMount(volume=object(), read_only=True)},
            wait=False,
        )


def test_snapshot_directory_requires_modal_backing() -> None:
    computer = ComputerSandbox.local(token="dev")

    try:
        computer.snapshot_directory("/home/desktop/artifacts")
    except SandboxUnavailableError as exc:
        assert "directory snapshots require" in str(exc)
    else:
        raise AssertionError("expected local directory snapshot helper to fail")


def test_registry_find_by_run_id_missing_returns_none(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())

    assert SandboxRegistry(app_name="computer-app").find_by_run_id("missing") is None


def test_registry_find_by_run_id_ambiguous_fails(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    FakeSandbox.listed = [
        FakeSandboxObject(tags={"computer-use.run_id": "run-123", APP_ID_TAG: "ap-computer-app"}),
        FakeSandboxObject(tags={"computer-use.run_id": "run-123", APP_ID_TAG: "ap-computer-app"}),
    ]

    try:
        SandboxRegistry(app_name="computer-app").find_by_run_id("run-123")
    except SandboxAmbiguousError as exc:
        assert "multiple matching computer-use sandbox" in str(exc)
    else:
        raise AssertionError("expected ambiguous registry lookup to fail")


def test_attach_or_create_by_run_id_reuses_matching_config(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    config = ComputerConfig(
        run_id="run-123",
        runtime={"modal_region": "us-west"},
    )
    FakeSandbox.listed = [
        FakeSandboxObject(
            tags={
                "computer-use": "true",
                "computer-use.run_id": "run-123",
                "computer-use.config_hash": compute_config_hash(config),
                APP_ID_TAG: "ap-modal-computer-use",
            }
        )
    ]

    computer = ComputerSandbox.attach_or_create(config=config, image=object(), wait=False)

    assert computer.metadata() is not None
    assert computer.metadata().run_id == "run-123"
    assert computer._requested_modal_region == "us-west"
    assert FakeSandbox.list_calls == [
        {
            "app_id": "ap-modal-computer-use",
            "tags": {
                "computer-use.run_id": "run-123",
                APP_ID_TAG: "ap-modal-computer-use",
            },
        }
    ]
    assert FakeSandbox.create_calls == []


def test_attach_or_create_reuse_uses_runtime_modal_environment(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    config = ComputerConfig(
        run_id="run-123",
        runtime={"modal_environment": "production"},
    )
    FakeSandbox.listed = [
        FakeSandboxObject(
            tags={
                "computer-use": "true",
                "computer-use.run_id": "run-123",
                "computer-use.config_hash": compute_config_hash(config),
                APP_ID_TAG: "ap-modal-computer-use",
            }
        )
    ]

    computer = ComputerSandbox.attach_or_create(config=config, image=object(), wait=False)

    assert computer.metadata() is not None
    assert computer.metadata().run_id == "run-123"
    assert FakeApp.lookups == [("modal-computer-use", False, "production")]
    assert FakeSandbox.create_calls == []


def test_attach_or_create_never_reuse_creates_without_listing(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())

    ComputerSandbox.attach_or_create(
        config=ComputerConfig(run_id="run-123"),
        reuse="never",
        image=object(),
        wait=False,
    )

    assert FakeSandbox.list_calls == []
    assert len(FakeSandbox.create_calls) == 1


def test_attach_or_create_by_name_reuses_matching_config(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    config = ComputerConfig(run_id="run-123")
    FakeSandbox.from_name_result = FakeSandboxObject(
        name="desktop-1",
        tags={
            "computer-use": "true",
            "computer-use.run_id": "run-123",
            "computer-use.config_hash": compute_config_hash(config),
            APP_ID_TAG: "ap-modal-computer-use",
        },
    )

    computer = ComputerSandbox.attach_or_create(
        config=config,
        reuse="by_name",
        name="desktop-1",
        image=object(),
        wait=False,
    )

    assert computer.metadata() is not None
    assert computer.metadata().name == "desktop-1"
    assert FakeSandbox.from_name_calls == [("modal-computer-use", "desktop-1", None)]
    assert FakeSandbox.create_calls == []


def test_attach_or_create_config_hash_mismatch_raises_by_default(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    config = ComputerConfig(run_id="run-123")
    FakeSandbox.listed = [
        FakeSandboxObject(
            tags={
                "computer-use": "true",
                "computer-use.run_id": "run-123",
                "computer-use.config_hash": "different",
                APP_ID_TAG: "ap-modal-computer-use",
            }
        )
    ]

    try:
        ComputerSandbox.attach_or_create(config=config, image=object(), wait=False)
    except ConfigConflictError as exc:
        assert exc.requested_hash == compute_config_hash(config)
        assert exc.existing_hash == "different"
        assert exc.sandbox_id == "sb-123"
    else:
        raise AssertionError("expected config mismatch to fail")


def test_attach_or_create_config_hash_mismatch_can_reuse_explicitly(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    FakeSandbox.listed = [
        FakeSandboxObject(
            tags={
                "computer-use": "true",
                "computer-use.run_id": "run-123",
                "computer-use.config_hash": "different",
                APP_ID_TAG: "ap-modal-computer-use",
            }
        )
    ]

    computer = ComputerSandbox.attach_or_create(
        config=ComputerConfig(
            run_id="run-123",
            runtime={"modal_region": "us-west"},
        ),
        on_config_mismatch="reuse",
        image=object(),
        wait=False,
    )

    assert computer.metadata() is not None
    assert computer.metadata().config_hash == "different"
    assert computer._requested_modal_region is None
    assert FakeSandbox.create_calls == []


def test_attach_or_create_missing_run_id_match_creates(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())

    ComputerSandbox.attach_or_create(
        config=ComputerConfig(run_id="run-123"),
        image=object(),
        wait=False,
    )

    assert FakeSandbox.list_calls == [
        {
            "app_id": "ap-modal-computer-use",
            "tags": {
                "computer-use.run_id": "run-123",
                APP_ID_TAG: "ap-modal-computer-use",
            },
        }
    ]
    assert len(FakeSandbox.create_calls) == 1


def test_attach_or_create_by_name_missing_creates(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    FakeSandbox.from_name_error = SandboxUnavailableError("missing")

    ComputerSandbox.attach_or_create(
        config=ComputerConfig(run_id="run-123"),
        reuse="by_name",
        name="desktop-1",
        image=object(),
        wait=False,
    )

    assert FakeSandbox.from_name_calls == [("modal-computer-use", "desktop-1", None)]
    assert len(FakeSandbox.create_calls) == 1
    assert FakeSandbox.create_calls[0][1]["name"] == "desktop-1"


def test_core_does_not_reference_modal_network_file_system() -> None:
    for path in (REPO_ROOT / "src" / "modal_computer_use").rglob("*.py"):
        assert "NetworkFileSystem" not in path.read_text(encoding="utf-8"), path


def test_core_modules_do_not_import_provider_sdks() -> None:
    core_paths = [
        path
        for path in (REPO_ROOT / "src" / "modal_computer_use").rglob("*.py")
        if "adapters" not in path.parts
    ]
    for path in core_paths:
        text = path.read_text(encoding="utf-8")
        assert "import openai" not in text, path
        assert "from openai" not in text, path
        assert "import anthropic" not in text, path
        assert "from anthropic" not in text, path
        assert "import daytona" not in text, path
        assert "from daytona" not in text, path
        assert "import e2b" not in text, path
        assert "from e2b" not in text, path
        assert "import e2b_desktop" not in text, path
        assert "from e2b_desktop" not in text, path
