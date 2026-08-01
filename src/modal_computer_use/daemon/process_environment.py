from __future__ import annotations

import os
from collections.abc import Mapping

_SECRET_MARKERS = ("_CREDENTIAL", "_PASSWORD", "_SECRET", "_TOKEN")


def desktop_process_environment(
    *,
    display: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a desktop child environment without daemon-owned credentials."""
    source = os.environ if environ is None else environ
    env = {key: value for key, value in source.items() if not _is_daemon_secret_name(key)}
    env["DISPLAY"] = display
    return env


def _is_daemon_secret_name(name: str) -> bool:
    return name.startswith("COMPUTER_USE_") and any(marker in name for marker in _SECRET_MARKERS)
