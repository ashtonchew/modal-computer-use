from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

from modal_computer_use import ComputerConfig, ComputerSandbox
from modal_computer_use.config import BrowserConfig
from modal_computer_use.errors import (
    ConfigConflictError,
    SandboxAmbiguousError,
    SandboxUnavailableError,
)
from modal_computer_use.registry import SandboxRegistry
from modal_computer_use.sandbox import modal_sandbox_exec_runner_from_id
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

    @classmethod
    def lookup(cls, app_name: str, *, create_if_missing: bool) -> str:
        cls.lookups.append((app_name, create_if_missing))
        return f"app:{app_name}"


class FakeConnectToken:
    url = "https://sandbox-connect.example"

    @property
    def token(self) -> str:
        return "connect-token"


class FakeSandboxObject:
    def __init__(self, *, name: str | None = None, tags: dict[str, str] | None = None) -> None:
        self.object_id = "sb-123"
        self.name = name
        self._tags = tags or {}
        self.set_tags_calls: list[dict[str, str]] = []
        self.wait_until_ready_calls: list[int] = []

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

    def tunnels(self) -> dict[int, object]:
        return {6080: SimpleNamespace(url="https://novnc.example")}


class FakeSandbox:
    create_calls: ClassVar[list[tuple[tuple[str, ...], dict[str, object]]]] = []
    from_name_calls: ClassVar[list[tuple[str, str]]] = []
    from_id_calls: ClassVar[list[str]] = []
    list_calls: ClassVar[list[dict[str, str] | None]] = []
    created: ClassVar[FakeSandboxObject | None] = None
    listed: ClassVar[list[FakeSandboxObject]] = []
    from_name_result: ClassVar[FakeSandboxObject | None] = None
    from_name_error: ClassVar[Exception | None] = None

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
        return FakeSandboxObject()

    @classmethod
    def list(cls, *, tags: dict[str, str] | None = None) -> list[FakeSandboxObject]:
        cls.list_calls.append(tags)
        return cls.listed


def fake_modal() -> SimpleNamespace:
    FakeProbe.calls = []
    FakeApp.lookups = []
    FakeSandbox.create_calls = []
    FakeSandbox.from_name_calls = []
    FakeSandbox.from_id_calls = []
    FakeSandbox.list_calls = []
    FakeSandbox.created = None
    FakeSandbox.listed = []
    FakeSandbox.from_name_result = None
    FakeSandbox.from_name_error = None
    return SimpleNamespace(App=FakeApp, Probe=FakeProbe, Sandbox=FakeSandbox)


def test_create_uses_current_modal_sandbox_contract(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())
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
        wait=True,
    )

    assert computer.metadata() is not None
    assert computer.metadata().vnc_url == "https://novnc.example"
    assert FakeProbe.calls == [8080]
    args, kwargs = FakeSandbox.create_calls[0]
    assert args == ("python", "-m", "modal_computer_use.daemon")
    assert kwargs["app"] == "app:modal-computer-use"
    assert kwargs["env"]["COMPUTER_USE_RUN_ID"] == "run-123"
    assert kwargs["env"]["COMPUTER_USE_DEFAULT_ACTION_TIMEOUT_MS"] == "5000"
    assert kwargs["env"]["COMPUTER_USE_MAX_ACTION_TIMEOUT_MS"] == "300000"
    assert kwargs["env"]["COMPUTER_USE_MAX_BATCH_DURATION_MS"] == "30000"
    assert kwargs["encrypted_ports"] == [6080]
    assert kwargs["readiness_probe"] == "tcp:8080"
    assert "environment_variables" not in kwargs
    assert "tags" not in kwargs
    assert FakeSandbox.created is not None
    assert FakeSandbox.created.wait_until_ready_calls == [120]
    assert FakeSandbox.created.set_tags_calls[0]["computer-use.run_id"] == "run-123"
    assert FakeSandbox.created.set_tags_calls[0]["computer-use.owner"] == "alice"
    assert FakeSandbox.created.set_tags_calls[0]["custom"] == "tag"


def test_create_keeps_novnc_closed_by_default(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())

    ComputerSandbox.create(config=ComputerConfig(run_id="run-123"), image=object(), wait=False)

    _, kwargs = FakeSandbox.create_calls[0]
    assert kwargs["encrypted_ports"] == []


def test_attach_by_name_uses_current_from_name_signature(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal())

    ComputerSandbox.attach(app_name="computer-app", name="desktop-1")

    assert FakeSandbox.from_name_calls == [("computer-app", "desktop-1")]
    assert FakeApp.lookups == []


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
    assert metadata.config_hash == config_hash
    assert metadata.tags["computer-use"] == "true"
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
            },
        )
    ]

    refs = SandboxRegistry(app_name="computer-app").list()

    assert FakeSandbox.list_calls == [{"computer-use": "true"}]
    assert refs[0].name == "desktop-1"
    assert refs[0].run_id == "run-123"
    assert refs[0].config_hash == "abc"


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
