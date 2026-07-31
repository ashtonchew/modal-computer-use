from __future__ import annotations

import inspect
import sys
from datetime import UTC, datetime
from importlib.util import find_spec
from types import ModuleType, SimpleNamespace
from typing import get_type_hints

import modal_computer_use
import modal_computer_use.actions as actions
import modal_computer_use.daemon.actions as daemon_actions
import modal_computer_use.errors as errors
import modal_computer_use.image as image
import modal_computer_use.manager as manager_module
import modal_computer_use.sandbox as sandbox_module
import modal_computer_use.state as state
from modal_computer_use import ComputerSandboxManager
from modal_computer_use.adapters.anthropic import AnthropicAdapter
from modal_computer_use.config import BrowserConfig
from modal_computer_use.daemon.desktop import browser, xtest
from modal_computer_use.models import ActionResult, CoordinateSpace, Point, SandboxRef
from modal_computer_use.transports import HTTPTransport
from modal_computer_use.transports.http import HTTPTransport as CanonicalHTTPTransport


def test_removed_compatibility_names_and_deep_modules_are_absent() -> None:
    assert "SandboxManager" not in modal_computer_use.__all__
    assert not hasattr(modal_computer_use, "SandboxManager")
    assert not hasattr(manager_module, "SandboxManager")
    assert not hasattr(sandbox_module, "modal_workspace_billing_report")
    assert "XTestPointerController" not in xtest.__all__
    assert not hasattr(xtest, "XTestPointerController")
    assert not hasattr(image, "browser_image")
    assert not hasattr(actions, "transform_point")
    assert not hasattr(state, "sandbox_ref_from_values")
    assert not hasattr(errors, "ProcessExecutionError")
    assert not hasattr(errors, "ErrorInfo")
    assert not hasattr(browser, "BrowserKind")
    assert find_spec("modal_computer_use.transports.local") is None
    assert find_spec("modal_computer_use.adapters.anthropic.schemas") is None


def test_root_all_contains_unique_importable_canonical_names() -> None:
    assert len(modal_computer_use.__all__) == len(set(modal_computer_use.__all__))
    assert "ComputerSandboxManager" in modal_computer_use.__all__
    for name in modal_computer_use.__all__:
        assert hasattr(modal_computer_use, name), name


def test_exported_functions_have_complete_annotations() -> None:
    for module in (modal_computer_use, daemon_actions):
        for name in module.__all__:
            value = getattr(module, name)
            if not inspect.isfunction(value):
                continue
            signature = inspect.signature(value)
            assert signature.return_annotation is not inspect.Signature.empty, name
            missing = [
                parameter.name
                for parameter in signature.parameters.values()
                if parameter.annotation is inspect.Signature.empty
            ]
            assert not missing, f"{name} has unannotated parameters: {missing}"


def test_canonical_manager_billing_and_x11_replacements_behave(monkeypatch) -> None:
    manager = ComputerSandboxManager(app_name="cutover-test")
    assert manager.app_name == "cutover-test"

    billing_calls: list[dict[str, object]] = []

    class FakeWorkspace:
        billing = SimpleNamespace(
            report=lambda **kwargs: billing_calls.append(kwargs) or ["workspace-row"]
        )

        @classmethod
        def from_context(cls) -> type[FakeWorkspace]:
            return cls

    monkeypatch.setitem(sys.modules, "modal", SimpleNamespace(Workspace=FakeWorkspace))
    result = sandbox_module.modal_billing_report(
        start=datetime(2026, 7, 1, tzinfo=UTC),
        end=None,
        resolution="day",
        tag_names=None,
    )

    assert result == ["workspace-row"]
    assert billing_calls == [
        {
            "start": datetime(2026, 7, 1, tzinfo=UTC),
            "end": None,
            "resolution": "day",
            "tag_names": None,
        }
    ]
    session = xtest.X11InputSession(display=":99")
    assert session.failure is None


def test_other_canonical_replacements_cover_removed_deep_imports(monkeypatch) -> None:
    class FakeImage:
        def __init__(self) -> None:
            self.packages: tuple[str, ...] = ()
            self.environment: dict[str, str] = {}

        @classmethod
        def debian_slim(cls, *, python_version: str) -> FakeImage:
            assert python_version == "3.12"
            return cls()

        def apt_install(self, *packages: str) -> FakeImage:
            self.packages = packages
            return self

        def pip_install_from_pyproject(self, _path: str) -> FakeImage:
            return self

        def env(self, environment: dict[str, str]) -> FakeImage:
            self.environment = environment
            return self

        def add_local_python_source(self, _package: str) -> FakeImage:
            return self

    monkeypatch.setattr(image, "_modal", lambda: SimpleNamespace(Image=FakeImage))
    browser_image = image.default_image(
        profile="browser",
        browser="firefox",
        browser_prewarm=True,
    )

    assert isinstance(browser_image, FakeImage)
    assert "firefox-esr" in browser_image.packages
    assert browser_image.environment["COMPUTER_USE_BROWSER_PREWARM"] == "true"
    assert HTTPTransport is CanonicalHTTPTransport
    space = CoordinateSpace.from_dimensions(
        desktop_width=200,
        desktop_height=100,
        image_width=100,
        image_height=50,
    )
    assert space.to_desktop(Point(x=20, y=10)) == Point(x=40, y=20)
    ref = SandboxRef.model_validate(
        {"sandbox_id": "sb-1", "app_name": "app", "status": "ready"}
    )
    assert ref.sandbox_id == "sb-1"
    failed = ActionResult(ok=False, message="command failed")
    assert failed.ok is False
    error = errors.DaemonHTTPError("request failed", code="failed", details={"retry": False})
    assert (error.code, str(error), error.details) == (
        "failed",
        "request failed",
        {"retry": False},
    )
    adapter = AnthropicAdapter(object())
    assert adapter.normalize({"action": "mouse_move", "coordinate": [2, 3]})["type"] == "move"
    assert BrowserConfig(kind="chromium").kind == "chromium"
    assert "BrowserGpuMode" in browser.__all__


def test_public_manager_and_sandbox_type_hints_resolve() -> None:
    modules: tuple[ModuleType, ...] = (manager_module, sandbox_module)
    for module in modules:
        for name, value in vars(module).items():
            if name.startswith("_") or not inspect.isfunction(value):
                continue
            if value.__module__ == module.__name__:
                get_type_hints(value)

    for cls in (manager_module.ComputerSandboxManager, sandbox_module.ComputerSandbox):
        for name, descriptor in vars(cls).items():
            if name.startswith("_"):
                continue
            value = (
                descriptor.__func__
                if isinstance(descriptor, classmethod | staticmethod)
                else descriptor
            )
            if inspect.isfunction(value):
                get_type_hints(value)
