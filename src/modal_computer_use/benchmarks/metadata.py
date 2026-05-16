from __future__ import annotations

from typing import Any

from ..client import DaemonClient
from .constants import FutureBenchmarkStatus
from .safety import _failure


def _collect_metadata(client: DaemonClient, failures: list[dict[str, Any]]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    try:
        metadata["version"] = client.get_json("/v1/version")
    except Exception as exc:
        failures.append(_failure("metadata_version", phase="setup", iteration=0, exc=exc))
    try:
        capabilities = client.get_json("/v1/capabilities")
    except Exception as exc:
        failures.append(_failure("metadata_capabilities", phase="setup", iteration=0, exc=exc))
    else:
        metadata["capabilities"] = {
            "primitives": capabilities.get("primitives"),
            "screenshot_formats": capabilities.get("screenshot_formats"),
            "action_types": capabilities.get("action_types"),
            "image_profile": capabilities.get("image_profile"),
            "vnc_enabled": capabilities.get("vnc_enabled"),
        }
    try:
        browser_status = client.get_json("/v1/browser/status")
    except Exception as exc:
        metadata["browser"] = {"status_error": type(exc).__name__}
    else:
        metadata["browser"] = {
            "configured_browser": browser_status.get("configured_browser"),
            "prewarm": browser_status.get("prewarm"),
            "profile_dir": browser_status.get("profile_dir"),
            "gpu_mode": browser_status.get("gpu_mode"),
            "launch_args": browser_status.get("launch_args"),
            "open_url_on_start": browser_status.get("open_url_on_start"),
            "prewarm_result": browser_status.get("prewarm_result"),
            "windows": browser_status.get("windows"),
        }
    return metadata

def _report_action_batch(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "failed" if result.get("failures") else "ok",
        "action_count": result.get("action_count"),
        "actions": result.get("actions"),
        "cases": result.get("cases"),
        "comparison": result.get("comparison"),
        "failures": result.get("failures", []),
    }

def _future_benchmark(status: FutureBenchmarkStatus, reason: str) -> dict[str, str]:
    return {"status": status, "reason": reason}

def _benchmark_failures(benchmark: str, failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(failure, benchmark=benchmark) for failure in failures]
