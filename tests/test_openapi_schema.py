from __future__ import annotations

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
