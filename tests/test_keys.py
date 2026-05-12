from __future__ import annotations

from modal_computer_use.actions import normalize_key, normalize_key_combo


def test_key_normalization() -> None:
    assert normalize_key("enter") == "Return"
    assert normalize_key("cmd") == "super"
    assert normalize_key("f5") == "F5"
    assert normalize_key_combo("ctrl+c") == ["ctrl", "c"]
