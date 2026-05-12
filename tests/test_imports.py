from __future__ import annotations

import sys


def test_core_import_does_not_import_providers() -> None:
    sys.modules.pop("openai", None)
    sys.modules.pop("anthropic", None)
    import modal_computer_use  # noqa: F401

    assert "openai" not in sys.modules
    assert "anthropic" not in sys.modules


def test_no_network_filesystem_usage() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "modal_computer_use"
    text = "\n".join(path.read_text() for path in root.rglob("*.py"))
    assert "NetworkFileSystem" not in text
