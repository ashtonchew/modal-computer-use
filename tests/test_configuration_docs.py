from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path
from typing import get_args

from pydantic import BaseModel

from modal_computer_use.config import ComputerConfig
from modal_computer_use.configuration_reference import DOCUMENTED_ENVIRONMENT_VARIABLES

ROOT = Path(__file__).resolve().parents[1]
CONFIGURATION_DOC = ROOT / "docs" / "configuration.md"
ENV_NAME = re.compile(r"(?:COMPUTER_USE_[A-Z0-9_]+|DISPLAY)")
TABLE_ENTRY = re.compile(r"^\| `([^`]+)` \|")
REGISTRY_MODULE = ROOT / "src" / "modal_computer_use" / "configuration_reference.py"


def _documented_table_entries(text: str) -> Counter[str]:
    entries: Counter[str] = Counter()
    for line in text.splitlines():
        match = TABLE_ENTRY.match(line)
        if match:
            entries[match.group(1)] += 1
    return entries


def _configuration_sections() -> tuple[str, str]:
    document = CONFIGURATION_DOC.read_text(encoding="utf-8")
    sdk_section, daemon_section = document.split("## Daemon and operator environment", maxsplit=1)
    return sdk_section, daemon_section


def _public_sdk_fields() -> set[str]:
    fields: set[str] = set()
    for group, field_info in ComputerConfig.model_fields.items():
        candidates = (field_info.annotation, *get_args(field_info.annotation))
        nested_model = next(
            (
                candidate
                for candidate in candidates
                if isinstance(candidate, type)
                and issubclass(candidate, BaseModel)
                and candidate is not ComputerConfig
            ),
            None,
        )
        if nested_model is None:
            fields.add(group)
        else:
            fields.update(f"{group}.{field}" for field in nested_model.model_fields)
    return fields


def _product_environment_names() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "src" / "modal_computer_use").rglob("*.py"):
        if path == REGISTRY_MODULE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if ENV_NAME.fullmatch(node.value):
                names.add(node.value)
    return {name for name in names if not name.startswith("COMPUTER_USE_BENCHMARK_")}


def test_configuration_reference_covers_each_public_sdk_field_once() -> None:
    sdk_section, _ = _configuration_sections()
    documented = _documented_table_entries(sdk_section)
    expected = _public_sdk_fields()

    assert set(documented) == expected
    assert all(documented[name] == 1 for name in expected)


def test_configuration_reference_covers_each_product_environment_name_once() -> None:
    _, daemon_section = _configuration_sections()
    documented = _documented_table_entries(daemon_section)
    expected = set(DOCUMENTED_ENVIRONMENT_VARIABLES)

    assert {name for name in documented if ENV_NAME.fullmatch(name)} == expected
    assert all(documented[name] == 1 for name in expected)


def test_documented_environment_registry_matches_product_source() -> None:
    registered = set(DOCUMENTED_ENVIRONMENT_VARIABLES)
    assert len(registered) == len(DOCUMENTED_ENVIRONMENT_VARIABLES)
    assert registered == _product_environment_names()
