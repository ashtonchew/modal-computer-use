from __future__ import annotations

import json
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HYGIENE = runpy.run_path(str(ROOT / "scripts" / "check_repository_hygiene.py"))
find_violations = HYGIENE["find_violations"]
tracked_paths = HYGIENE["tracked_paths"]


def test_tracked_repository_has_no_private_runtime_metadata() -> None:
    assert find_violations(ROOT, tracked_paths(ROOT)) == []


def test_hygiene_scan_finds_each_private_artifact_shape(tmp_path: Path) -> None:
    root_output = tmp_path / "benchmark-report.json"
    root_output.write_text("{}\n", encoding="utf-8")

    benchmark_data = tmp_path / "benchmark-data" / "candidate.json"
    benchmark_data.parent.mkdir()
    benchmark_data.write_text(
        json.dumps(
            {
                "endpoint": "https://private.example",
                "url": "https://connect.modal.run/private?_modal_connect_token=secret",
            }
        ),
        encoding="utf-8",
    )

    notes = tmp_path / "docs" / "notes.md"
    notes.parent.mkdir()
    notes.write_text(
        "\n".join(
            (
                "/Users/" + "alice/project/.env",
                "sb-" + "A" * 20,
                "run_" + "a" * 16,
            )
        ),
        encoding="utf-8",
    )

    violations = find_violations(
        tmp_path,
        (Path(root_output.name), Path("benchmark-data/candidate.json"), Path("docs/notes.md")),
    )

    assert any(item.startswith("root benchmark output:") for item in violations)
    assert any(item.startswith("benchmark endpoint field:") for item in violations)
    assert any(item.startswith("Modal endpoint value:") for item in violations)
    assert any(item.startswith("personal home path:") for item in violations)
    assert any(item.startswith("Modal Sandbox ID:") for item in violations)
    assert any(item.startswith("Modal run ID:") for item in violations)


def test_hygiene_scan_allows_sanitized_fields_and_placeholders(tmp_path: Path) -> None:
    benchmark_data = tmp_path / "benchmark-data" / "sanitized.json"
    benchmark_data.parent.mkdir()
    benchmark_data.write_text(
        json.dumps(
            {
                "base_url": None,
                "run_id": "modal-v2-placement-0123456789abcdef-target",
                "url": "https://example.com/",
                "documentation_url": "https://docs.modal.hosting.example/",
                "connect_lookalike": "https://connect.modal.run.example/",
            }
        ),
        encoding="utf-8",
    )
    tests = tmp_path / "tests" / "placeholder.txt"
    tests.parent.mkdir()
    tests.write_text("sb-placeholder run-no-environment /path/to/.env", encoding="utf-8")

    assert find_violations(
        tmp_path,
        (Path("benchmark-data/sanitized.json"), Path("tests/placeholder.txt")),
    ) == []
