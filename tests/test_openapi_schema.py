from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_checked_in_openapi_schema_is_current() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/export_openapi.py", "--check"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_readyz_openapi_documents_unready_response() -> None:
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "docs" / "openapi.json").read_text(encoding="utf-8"))

    readyz_responses = schema["paths"]["/readyz"]["get"]["responses"]
    assert readyz_responses["503"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReadyStatus"
    }


def test_process_restart_openapi_documents_unknown_process() -> None:
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "docs" / "openapi.json").read_text(encoding="utf-8"))

    responses = schema["paths"]["/v1/processes/{name}/restart"]["post"]["responses"]
    assert responses["404"]["description"] == "Unknown process"


def test_raw_full_screenshot_openapi_documents_binary_metadata() -> None:
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "docs" / "openapi.json").read_text(encoding="utf-8"))

    response = schema["paths"]["/v1/screenshots/full/raw"]["post"]["responses"]["200"]
    assert set(response["content"]) >= {"image/png", "image/jpeg", "image/webp"}
    assert set(response["headers"]) >= {
        "x-computer-use-width",
        "x-computer-use-height",
        "x-computer-use-size-bytes",
        "x-computer-use-sha256",
        "x-computer-use-captured-at",
        "x-computer-use-coordinate-space",
        "x-computer-use-cursor-visible",
        "x-computer-use-cursor-position",
    }
