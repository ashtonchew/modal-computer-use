from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from ..client import DaemonClient
from . import core
from .billing import reconcile_modal_billing_from_metadata
from .surface_result import _surface_not_measured, _surface_result
from .surface_verification import _run_daemon_http_verification


def _run_daemon_http_surface(
    *,
    client: DaemonClient | None,
    mode: str,
    base_url: str | None,
    iterations: int,
    warmup_iterations: int,
    environment_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    ingress = _daemon_ingress_metadata(
        mode=mode,
        base_url=base_url,
        environment_metadata=environment_metadata,
    )
    if client is None:
        return _surface_not_measured(
            ingress["canonical_name"],
            "daemon HTTP benchmark surface requires --mock-local or --base-url",
        )
    browser_status = _safe_browser_status_metadata(client)
    action_batch = core.run_action_batch_benchmark(
        client=client,
        mode="mock-local" if mode == "mock-local" else "http",
        iterations=iterations,
        base_url=base_url,
        warmup_iterations=warmup_iterations,
    )
    cases = {
        "action_batch": core._report_action_batch(action_batch),
        "screenshot_full": core.run_screenshot_benchmark(
            client=client,
            name="screenshot_full",
            request={"format": "png", "storage": "inline", "show_cursor": False},
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "screenshot_full_raw": core.run_screenshot_benchmark(
            client=client,
            name="screenshot_full_raw",
            request={"format": "png", "show_cursor": False},
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            raw=True,
        ),
        "move_click": core.run_move_click_benchmark(
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "click_screenshot_raw": core.run_click_screenshot_raw_benchmark(
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "move_click_sequence": core.run_move_click_sequence_benchmark(
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "type_100_chars": core.run_type_100_chars_benchmark(
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "type_1000_chars": core.run_type_1000_chars_benchmark(
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "command_echo": core.run_command_echo_benchmark(
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "recording_start_stop": core.run_recording_start_stop_benchmark(
            client=client,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        ),
        "cold_create_to_ready": _modal_cold_create_to_ready_case(environment_metadata),
        "warm_attach_to_health": core._future_benchmark(
            "not_measured",
            "warm attach requires Modal orchestration metadata",
        ),
    }
    if browser_status.get("configured_browser") == "chromium":
        cases["browser_render_metrics"] = core.run_browser_render_metrics_benchmark(
            client=client,
            url="https://example.com",
            iterations=iterations,
            warmup_iterations=warmup_iterations,
        )
    else:
        cases["browser_render_metrics"] = core._future_benchmark(
            "not_measured",
            "browser render metrics require configured chromium",
        )
    metadata = {
        "transport": "daemon-http",
        "canonical_name": ingress["canonical_name"],
        "ingress": ingress,
        "base_url": core._safe_base_url(base_url),
        "environment": {
            key: value for key, value in (environment_metadata or {}).items() if value is not None
        },
        "browser": browser_status,
    }
    return _surface_result(
        "daemon-http",
        cases=cases,
        metadata=metadata,
        runtime_seconds=_modal_surface_runtime_seconds(environment_metadata),
        verification=_run_daemon_http_verification(client),
        billing_reconciliation=reconcile_modal_billing_from_metadata(environment_metadata),
    )


def _daemon_ingress_metadata(
    *,
    mode: str,
    base_url: str | None,
    environment_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    safe_base_url = core._safe_base_url(base_url)
    if mode == "mock-local":
        return {
            "canonical_name": "modal-daemon-local",
            "kind": "local",
            "auth": "testclient",
            "description": "in-process local daemon benchmark path",
        }
    modal_ingress = (
        None if environment_metadata is None else environment_metadata.get("modal_ingress")
    )
    daemon_http_version = (
        None if environment_metadata is None else environment_metadata.get("daemon_http_version")
    )
    if modal_ingress == "attested-tunnel":
        if daemon_http_version == "2":
            return {
                "canonical_name": "modal-daemon-attested-h2-tunnel",
                "kind": "modal-attested-encrypted-h2-tunnel",
                "auth": "Modal Connect Token attestation plus short-lived daemon bearer token",
                "description": (
                    "Modal Connect-authenticated token exchange followed by encrypted HTTP/2 "
                    "tunnel ingress"
                ),
            }
        return {
            "canonical_name": "modal-daemon-attested-tunnel",
            "kind": "modal-attested-encrypted-tunnel",
            "auth": "Modal Connect Token attestation plus short-lived daemon bearer token",
            "description": (
                "Modal Connect-authenticated token exchange followed by encrypted tunnel ingress"
            ),
        }
    if modal_ingress == "connect":
        return {
            "canonical_name": "modal-daemon-connect",
            "kind": "modal-connect-token",
            "auth": "Modal Sandbox Connect Token",
            "description": "authenticated Modal connect-token ingress to the daemon",
        }
    if modal_ingress == "tunnel":
        if daemon_http_version == "2":
            return {
                "canonical_name": "modal-daemon-h2-tunnel",
                "kind": "modal-encrypted-h2-tunnel",
                "auth": "static daemon bearer token",
                "description": "Modal encrypted HTTP/2 tunnel ingress to the daemon",
            }
        return {
            "canonical_name": "modal-daemon-tunnel",
            "kind": "modal-encrypted-tunnel",
            "auth": "static daemon bearer token",
            "description": "Modal encrypted tunnel ingress to the daemon",
        }
    if safe_base_url == "https://connect.modal.run":
        return {
            "canonical_name": "modal-daemon-connect",
            "kind": "modal-connect-token",
            "auth": "Modal Sandbox Connect Token",
            "description": "authenticated Modal connect-token ingress to the daemon",
        }
    if safe_base_url and urlsplit(safe_base_url).netloc.endswith(".modal.host"):
        return {
            "canonical_name": "modal-daemon-tunnel",
            "kind": "modal-encrypted-tunnel",
            "auth": "daemon bearer token",
            "description": "Modal encrypted tunnel ingress to the daemon",
        }
    if safe_base_url:
        return {
            "canonical_name": "modal-daemon-http",
            "kind": "http",
            "auth": "caller-provided",
            "description": "caller-provided daemon HTTP ingress",
        }
    return {
        "canonical_name": "modal-daemon-http",
        "kind": "unknown",
        "auth": "unknown",
        "description": "daemon HTTP ingress could not be inferred",
    }

def _safe_browser_status_metadata(client: DaemonClient) -> dict[str, Any]:
    try:
        status = client.get_json("/v1/browser/status")
    except Exception as exc:
        return {"status_error": type(exc).__name__}
    return {
        "configured_browser": status.get("configured_browser"),
        "prewarm": status.get("prewarm"),
        "profile_dir": status.get("profile_dir"),
        "gpu_mode": status.get("gpu_mode"),
        "launch_args": status.get("launch_args"),
        "open_url_on_start": status.get("open_url_on_start"),
        "prewarm_result": status.get("prewarm_result"),
        "windows": status.get("windows"),
    }

def _modal_surface_runtime_seconds(environment_metadata: dict[str, Any] | None) -> float | None:
    if not environment_metadata:
        return None
    value = environment_metadata.get("modal_cold_create_to_ready_ms")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value <= 0:
        return None
    return float(value) / 1000.0

def _modal_cold_create_to_ready_case(environment_metadata: dict[str, Any] | None) -> dict[str, Any]:
    value = (
        None
        if not environment_metadata
        else environment_metadata.get("modal_cold_create_to_ready_ms")
    )
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return core._future_benchmark(
            "not_measured",
            "cold Modal Sandbox creation is measured by a live orchestration runner, "
            "not this daemon target",
        )
    result = core._case_result("cold_create_to_ready", 1, [float(value)], [])
    result["source"] = "live_orchestration_metadata"
    return result
