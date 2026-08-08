from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_v2_release_identity_is_coherent() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = (ROOT / "src/modal_computer_use/_version.py").read_text(encoding="utf-8")
    openapi = json.loads((ROOT / "docs/openapi.json").read_text(encoding="utf-8"))
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert project["project"]["version"] == "2.0.0"
    assert '__version__ = "2.0.0"' in runtime
    assert openapi["info"]["version"] == "2.0.0"
    assert "## 2.0.0 - 2026-08-08" in changelog


def test_v2_changelog_contains_the_exact_migration_contract() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    release = changelog.split("## 2.0.0 - 2026-08-08", maxsplit=1)[1].split(
        "## 1.1.0", maxsplit=1
    )[0]

    migration_rows = re.findall(r"^\| .* \| .* \| .* \|$", release, flags=re.MULTILINE)
    assert len(migration_rows) >= 9
    for required_term in (
        "ComputerSessionHandle",
        "borrow_async",
        "exact region",
        'screenshots.full(storage="inline")',
        "one `actions.run(...)` HTTP batch",
        "never replay automatically",
        "low-level primitive SDK",
    ):
        assert required_term in release


def test_release_record_keeps_publication_gated_and_records_rollback() -> None:
    record = (ROOT / "docs/v2-release-candidate.md").read_text(encoding="utf-8")

    assert "Status: live-verified branch candidate; not published" in record
    assert "modal-computer-use==1.1.0" in record
    assert "docs-v1.1.0-last-known-good" in record
    assert "never silently downgrades" in record
    assert "runtime artifacts → package → hosted documentation" in record
    assert "2026-08-08 optimized-default report" in record
    assert "31bcafefbba2ba75653075a04b12ce2eb816c838" in record
    assert "test_x11_clipboard_daemon_child_preserves_long_text_and_restores_state" in record
    assert "Neither" in record
    assert "test may skip" in record
