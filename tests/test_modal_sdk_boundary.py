from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

from modal_computer_use import ComputerConfig, ComputerSandbox, ComputerSandboxManager, DaemonClient
from modal_computer_use.config import BrowserConfig
from modal_computer_use.errors import (
    ConfigConflictError,
    SandboxAmbiguousError,
    SandboxUnavailableError,
)
from modal_computer_use.registry import SandboxRegistry
from modal_computer_use.sandbox import _connect_token_parts, modal_sandbox_exec_runner_from_id
from modal_computer_use.state import compute_config_hash

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeProbe:
    calls: ClassVar[list[int]] = []

    @classmethod
    def with_tcp(cls, port: int) -> str:
        cls.calls.append(port)
        return f"tcp:{port}"


class FakeApp:
    lookups: ClassVar[list[tuple[str, bool]]] = []
    objects: ClassVar[list[FakeAppObject]] = []

    @classmethod
    def lookup(cls, app_name: str, *, create_if_missing: bool) -> FakeAppObject:
        cls.lookups.append((app_name, create_if_missing))
        app = FakeAppObject(app_name)
        cls.objects.append(app)
        return app


class FakeAppObject:
    def __init__(self, app_name: str) -> None:
        self.app_name = app_name
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
        self.terminated = False

    def set_tags(self, tags: dict[str, str]) -> None:
        self.set_tags_calls.append(tags)
        self._tags = tags

    def get_tags(self) -> dict[str, str]:
        return self._tags

    def wait_until_ready(self, *, timeout: int) -> None:
        self.wait_until_ready_calls.append(timeout)

    def create_connect_token(self, *, user_metadata: dict[str, str]) -> FakeConnectToken:
        assert user_metadata["sdk"] == "modal-computer-use"
        return FakeConnectToken()

    def exec(self, *args: str, timeout: int | None = None) -> object:
        return SimpleNamespace(args=args, timeout=timeout, returncode=0)

    def terminate(self) -> None:
        self.terminated = True

    def tunnels(self) -> dict[int, object]:
        return {6080: SimpleNamespace(url="https://novnc.example")}

    def snapshot_filesystem(self) -> object:
        return SimpleNamespace(object_id="im-snapshot")

    def snapshot_directory(self, path: str) -> object:
        return SimpleNamespace(object_id="im-dir-snapshot", path=path)

    def mount_image(self, path: str, image: object) -> None:
        self.mount_image_calls.append((path, image))


class FakeSandbox:
    create_calls: ClassVar[list[tuple[tuple[str, ...], dict[str, object]]]] = []
    from_name_calls: ClassVar[list[tuple[str, str]]] = []
    from_id_calls: ClassVar[list[str]] = []
    list_calls: ClassVar[list[dict[str, str] | None]] = []
    created: ClassVar[FakeSandboxObject | None] = None
    listed: ClassVar[list[FakeSandboxObject]] = []
    from_name_result: ClassVar[FakeSandboxObject | None] = None
    from_name_error: ClassVar[Exception | None] = None
    from_id_result: ClassVar[FakeSandboxObject | None] = None

    @classmethod
    def create(cls, *args: str, **kwargs: object) -> FakeSandboxObject:
        cls.create_calls.append((args, kwargs))
        name = kwargs.get("name")
        cls.created = FakeSandboxObject(name=name if isinstance(name, str) else None)
        return cls.created

    @classmethod
    def from_name(cls, app_name: str, name: str) -> FakeSandboxObject:
        cls.from_name_calls.append((app_name, name))
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
    def list(cls, *, tags: dict[str, str] | None = None) -> list[FakeSandboxObject]:
        cls.list_calls.append(tags)
        return cls.listed


def fake_modal() -> SimpleNamespace:
    FakeProbe.calls = []
    FakeApp.lookups = []
    FakeApp.objects = []
    FakeSandbox.create_calls = []
    FakeSandbox.from_name_calls = []
    FakeSandbox.from_id_calls = []
    FakeSandbox.list_calls = []
    FakeSandbox.created = None
    FakeSandbox.listed = []
    FakeSandbox.from_name_result = None
    FakeSandbox.from_name_error = None
    FakeSandbox.from_id_result = None
    return SimpleNamespace(App=FakeApp, Probe=FakeProbe, Sandbox=FakeSandbox)


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
        app_tags={"benchmark": "provider-compare"},
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
    assert kwargs["env"]["COMPUTER_USE_INPUT_RATE_LIMIT_PER_SEC"] == "20"
    assert kwargs["env"]["COMPUTER_USE_MAX_BATCH_DURATION_MS"] == "30000"
    assert kwargs["env"]["COMPUTER_USE_TRUST_PRIVATE_CONNECT_PROXY"] == "true"
    assert kwargs["encrypted_ports"] == [6080]
    assert kwargs["readiness_probe"] == "tcp:8080"
    assert "environment_variables" not in kwargs
    assert "tags" not in kwargs
    assert FakeSandbox.created is not None
    assert FakeSandbox.created.wait_until_ready_calls == [120]
    assert readiness_calls == [120]
    assert FakeSandbox.created.set_tags_calls[0]["computer-use.run_id"] == "run-123"
    assert FakeSandbox.created.set_tags_calls[0]["computer-use.owner"] == "alice"
    assert FakeSandbox.created.set_tags_calls[0]["computer-use.artifacts_dir"] == (
        "/home/desktop/artifacts"
    )
    assert "computer-use.created_at" in FakeSandbox.created.set_tags_calls[0]
    assert FakeSandbox.created.set_tags_calls[0]["custom"] == "tag"
    assert FakeApp.objects[0].set_tags_calls == [
        {"existing": "app-tag", "benchmark": "provider-compare"}
    ]
    assert computer.metadata().owner == "alice"
    assert computer.metadata().created_at is not None


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
    applied_tags = FakeSandbox.created.set_tags_calls[0]
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


def test_create_passes_browser_profile_prewarm_and_gpu_env(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    config = ComputerConfig(
        run_id="run-123",
        browser=BrowserConfig(kind="chromium", prewarm=False),
    )
    config.resources.profile = "browser-gpu"
    config.resources.gpu = "T4"

    ComputerSandbox.create(config=config, image=object(), wait=False)

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["gpu"] == "T4"
    assert kwargs["env"]["COMPUTER_USE_IMAGE_PROFILE"] == "browser-gpu"
    assert kwargs["env"]["COMPUTER_USE_BROWSER"] == "chromium"
    assert kwargs["env"]["COMPUTER_USE_BROWSER_PREWARM"] == "false"
    assert kwargs["env"]["COMPUTER_USE_VNC_PASSWORD"] == ""


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

    ComputerSandbox.create(config=ComputerConfig(run_id="run-123"), image=object(), wait=False)

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["encrypted_ports"] == []


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

    ComputerSandbox.attach(app_name="computer-app", name="desktop-1")

    assert FakeSandbox.from_name_calls == [("computer-app", "desktop-1")]
    assert FakeApp.lookups == []


def test_attach_wait_polls_daemon_when_requested(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    readiness_calls: list[float] = []
    monkeypatch.setattr(
        ComputerSandbox,
        "wait_until_ready",
        lambda self, timeout=120.0, interval=1.0: readiness_calls.append(timeout),
    )

    ComputerSandbox.attach(
        app_name="computer-app",
        name="desktop-1",
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
        assert "window manager is not responding" in str(exc)
    else:
        raise AssertionError("expected readiness timeout")


def test_wait_until_ready_timeout_reports_transient_error() -> None:
    class FakeClient:
        def get_json(self, path: str) -> dict[str, object]:
            raise ConnectionError("connection refused")

    try:
        ComputerSandbox(FakeClient()).wait_until_ready(timeout=0, interval=0)
    except TimeoutError as exc:
        assert "ConnectionError: connection refused" in str(exc)
    else:
        raise AssertionError("expected readiness timeout")


def test_attach_by_run_id_lists_by_tags(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    FakeSandbox.listed = [
        FakeSandboxObject(tags={"computer-use.run_id": "run-123", "computer-use": "true"})
    ]

    ComputerSandbox.attach(run_id="run-123")

    assert FakeSandbox.list_calls == [{"computer-use.run_id": "run-123"}]


def test_attach_by_run_id_ambiguous_matches_fail(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    FakeSandbox.listed = [
        FakeSandboxObject(tags={"computer-use.run_id": "run-123", "computer-use": "true"}),
        FakeSandboxObject(tags={"computer-use.run_id": "run-123", "computer-use": "true"}),
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
            },
        )
    ]

    refs = SandboxRegistry(app_name="computer-app").list()

    assert FakeSandbox.list_calls == [{"computer-use": "true"}]
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
            },
        ),
        FakeSandboxObject(
            sandbox_id="new",
            tags={
                "computer-use": "true",
                "computer-use.created_at": "2026-05-12T12:00:00Z",
            },
        ),
        FakeSandboxObject(sandbox_id="unknown", tags={"computer-use": "true"}),
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
        },
    )
    new = FakeSandboxObject(
        sandbox_id="new",
        tags={
            "computer-use": "true",
            "computer-use.owner": "alice",
            "computer-use.created_at": "2026-05-12T12:00:00Z",
        },
    )
    FakeSandbox.listed = [old, new]

    result = ComputerSandboxManager(app_name="computer-app").cleanup_expired(
        ttl_seconds=3600,
        owner="alice",
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )

    assert FakeSandbox.list_calls == [{"computer-use": "true", "computer-use.owner": "alice"}]
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
        },
    )
    new = FakeSandboxObject(
        sandbox_id="new",
        tags={
            "computer-use": "true",
            "computer-use.created_at": "2026-05-12T11:30:00Z",
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


def test_manager_cleanup_skips_missing_and_invalid_created_at(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    missing = FakeSandboxObject(sandbox_id="missing", tags={"computer-use": "true"})
    invalid = FakeSandboxObject(
        sandbox_id="invalid",
        tags={"computer-use": "true", "computer-use.created_at": "not-a-date"},
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
    sandbox = FakeSandboxObject(sandbox_id="sb-terminate")
    FakeSandbox.from_id_result = sandbox

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

    snapshot = computer.snapshot_directory("/home/desktop/artifacts/snapshots")
    computer.mount_image("/home/desktop/artifacts/snapshots", snapshot)

    assert snapshot.object_id == "im-dir-snapshot"
    assert snapshot.path == "/home/desktop/artifacts/snapshots"
    assert FakeSandbox.created is not None
    assert FakeSandbox.created.mount_image_calls == [
        ("/home/desktop/artifacts/snapshots", snapshot)
    ]


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
        FakeSandboxObject(tags={"computer-use.run_id": "run-123"}),
        FakeSandboxObject(tags={"computer-use.run_id": "run-123"}),
    ]

    try:
        SandboxRegistry(app_name="computer-app").find_by_run_id("run-123")
    except SandboxAmbiguousError as exc:
        assert "multiple matching computer-use sandbox" in str(exc)
    else:
        raise AssertionError("expected ambiguous registry lookup to fail")


def test_attach_or_create_by_run_id_reuses_matching_config(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
    config = ComputerConfig(run_id="run-123")
    FakeSandbox.listed = [
        FakeSandboxObject(
            tags={
                "computer-use": "true",
                "computer-use.run_id": "run-123",
                "computer-use.config_hash": compute_config_hash(config),
            }
        )
    ]

    computer = ComputerSandbox.attach_or_create(config=config, image=object(), wait=False)

    assert computer.metadata() is not None
    assert computer.metadata().run_id == "run-123"
    assert FakeSandbox.list_calls == [{"computer-use.run_id": "run-123"}]
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
    assert FakeSandbox.from_name_calls == [("modal-computer-use", "desktop-1")]
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
            }
        )
    ]

    computer = ComputerSandbox.attach_or_create(
        config=ComputerConfig(run_id="run-123"),
        on_config_mismatch="reuse",
        image=object(),
        wait=False,
    )

    assert computer.metadata() is not None
    assert computer.metadata().config_hash == "different"
    assert FakeSandbox.create_calls == []


def test_attach_or_create_missing_run_id_match_creates(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())

    ComputerSandbox.attach_or_create(
        config=ComputerConfig(run_id="run-123"),
        image=object(),
        wait=False,
    )

    assert FakeSandbox.list_calls == [{"computer-use.run_id": "run-123"}]
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

    assert FakeSandbox.from_name_calls == [("modal-computer-use", "desktop-1")]
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
