from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

from modal_computer_use import ComputerConfig, ComputerSandbox
from modal_computer_use.config import BrowserConfig
from modal_computer_use.registry import SandboxRegistry


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

    def tunnels(self) -> dict[int, object]:
        return {6080: SimpleNamespace(url="https://novnc.example")}


class FakeSandbox:
    create_calls: ClassVar[list[tuple[tuple[str, ...], dict[str, object]]]] = []
    from_name_calls: ClassVar[list[tuple[str, str]]] = []
    from_id_calls: ClassVar[list[str]] = []
    list_calls: ClassVar[list[dict[str, str] | None]] = []
    created: ClassVar[FakeSandboxObject | None] = None
    listed: ClassVar[list[FakeSandboxObject]] = []

    @classmethod
    def create(cls, *args: str, **kwargs: object) -> FakeSandboxObject:
        cls.create_calls.append((args, kwargs))
        name = kwargs.get("name")
        cls.created = FakeSandboxObject(name=name if isinstance(name, str) else None)
        return cls.created

    @classmethod
    def from_name(cls, app_name: str, name: str) -> FakeSandboxObject:
        cls.from_name_calls.append((app_name, name))
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
