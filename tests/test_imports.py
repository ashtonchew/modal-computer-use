from __future__ import annotations

import subprocess
import sys
from importlib import import_module
from importlib.util import find_spec

import pytest


def test_core_import_does_not_import_providers() -> None:
    sys.modules.pop("openai", None)
    sys.modules.pop("anthropic", None)
    import modal_computer_use  # noqa: F401

    assert "openai" not in sys.modules
    assert "anthropic" not in sys.modules


def test_daemon_app_import_does_not_load_modal_orchestration_modules() -> None:
    code = """
import sys

import modal_computer_use.daemon.app

unexpected = {
    "modal_computer_use.image",
    "modal_computer_use.manager",
    "modal_computer_use.registry",
    "modal_computer_use.sandbox",
}.intersection(sys.modules)
assert not unexpected, sorted(unexpected)
"""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr


def test_root_package_exports_are_unique_and_importable() -> None:
    import modal_computer_use

    assert len(modal_computer_use.__all__) == len(set(modal_computer_use.__all__))
    assert "ComputerSandboxManager" in modal_computer_use.__all__
    for name in modal_computer_use.__all__:
        assert hasattr(modal_computer_use, name), name


@pytest.mark.parametrize(
    ("module_name", "attribute"),
    [
        ("modal_computer_use", "SandboxManager"),
        ("modal_computer_use.manager", "SandboxManager"),
        ("modal_computer_use.sandbox", "modal_workspace_billing_report"),
        ("modal_computer_use.daemon.desktop.xtest", "XTestPointerController"),
        ("modal_computer_use.image", "browser_image"),
        ("modal_computer_use.actions", "transform_point"),
        ("modal_computer_use.state", "sandbox_ref_from_values"),
        ("modal_computer_use.errors", "ProcessExecutionError"),
        ("modal_computer_use.errors", "ErrorInfo"),
        ("modal_computer_use.daemon.desktop.browser", "BrowserKind"),
    ],
)
def test_retired_compatibility_attributes_stay_absent(
    module_name: str,
    attribute: str,
) -> None:
    module = import_module(module_name)

    assert not hasattr(module, attribute)
    if hasattr(module, "__all__"):
        assert attribute not in module.__all__


@pytest.mark.parametrize(
    "module_name",
    [
        "modal_computer_use.transports.local",
        "modal_computer_use.adapters.anthropic.schemas",
        "modal_computer_use.daemon.desktop.processes",
        "modal_computer_use.daemon.trace",
    ],
)
def test_retired_compatibility_modules_stay_absent(module_name: str) -> None:
    assert find_spec(module_name) is None


def test_http_transport_package_export_uses_canonical_implementation() -> None:
    from modal_computer_use.transports import HTTPTransport
    from modal_computer_use.transports.http import HTTPTransport as CanonicalHTTPTransport

    assert HTTPTransport is CanonicalHTTPTransport


@pytest.mark.parametrize(
    ("module_name", "attribute"),
    [
        ("modal_computer_use.tracing", "TraceWriter"),
        ("modal_computer_use.daemon.supervisor", "Supervisor"),
    ],
)
def test_canonical_internal_imports_remain_available(
    module_name: str,
    attribute: str,
) -> None:
    assert hasattr(import_module(module_name), attribute)


def test_cli_import_does_not_require_optional_provider_packages() -> None:
    code = """
import importlib.abc
import sys

blocked = {"modal", "openai", "anthropic", "daytona", "e2b_desktop", "tzafon"}

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.', 1)[0] in blocked:
            raise ImportError(f"blocked optional dependency: {fullname}")
        return None

sys.meta_path.insert(0, Blocker())
import modal_computer_use.cli
"""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr


def test_session_handle_import_and_validation_do_not_require_modal() -> None:
    code = """
import importlib.abc
import sys

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.', 1)[0] == "modal":
            raise ImportError(f"blocked optional dependency: {fullname}")
        return None

sys.meta_path.insert(0, Blocker())
from modal_computer_use import ComputerSessionHandle

handle = ComputerSessionHandle(
    sandbox_id="sb-placeholder",
    session_id="b" * 32,
    app_name="desktop-app",
    modal_environment="prod",
    requested_modal_region="us-west",
    ingress="attested-tunnel",
    daemon_http_version="1.1",
    vnc_mode="off",
    config_hash="a" * 16,
)
assert handle.schema_version == 2
assert handle.handoff_protocol == "computer-use.session-handoff.v2"
"""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr


def test_async_daemon_client_composition_does_not_require_modal() -> None:
    code = """
import asyncio
import importlib.abc
import sys

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.', 1)[0] == "modal":
            raise ImportError(f"blocked optional dependency: {fullname}")
        return None

sys.meta_path.insert(0, Blocker())
from modal_computer_use import AsyncDaemonClient

async def main():
    client = AsyncDaemonClient.local(token="dev")
    assert client.mouse is client.mouse
    await client.aclose()

asyncio.run(main())
"""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr


def test_no_network_filesystem_usage() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "modal_computer_use"
    text = "\n".join(path.read_text() for path in root.rglob("*.py"))
    assert "NetworkFileSystem" not in text


def test_region_colocation_example_builds_config() -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "examples" / "region_colocation.py"
    spec = importlib.util.spec_from_file_location("region_colocation", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    config = module.computer_config_for_model_loop(modal_region="us-west")

    assert config.ingress == "attested-tunnel"
    assert config.runtime.modal_region == "us-west"
