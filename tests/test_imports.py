from __future__ import annotations

import subprocess
import sys


def test_core_import_does_not_import_providers() -> None:
    sys.modules.pop("openai", None)
    sys.modules.pop("anthropic", None)
    import modal_computer_use  # noqa: F401

    assert "openai" not in sys.modules
    assert "anthropic" not in sys.modules


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
