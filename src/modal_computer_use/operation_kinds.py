from __future__ import annotations

STABLE_OPERATION_KINDS = frozenset(
    {
        "actions.run",
        "/v1/apps/launch",
        "/v1/apps/open-artifact",
        "/v1/artifacts/sync",
        "/v1/artifacts/{path:path}",
        "/v1/browser/open-url",
        "/v1/browser/render-metrics",
        "/v1/clipboard/text",
        "/v1/commands/run",
        "/v1/computer/restart",
        "/v1/computer/start",
        "/v1/computer/stop",
        "/v1/input/release-all",
        "/v1/keyboard/hold",
        "/v1/keyboard/hotkey",
        "/v1/keyboard/press",
        "/v1/keyboard/type",
        "/v1/mouse/click",
        "/v1/mouse/down",
        "/v1/mouse/drag",
        "/v1/mouse/move",
        "/v1/mouse/scroll",
        "/v1/mouse/up",
        "/v1/processes/{name}/restart",
        "/v1/recordings",
        "/v1/recordings/{recording_id}",
        "/v1/recordings/{recording_id}/stop",
        "/v1/screenshots/full",
        "/v1/screenshots/region",
        "/v1/screenshots/zoom",
        "/v1/windows/{window_id}/activate",
        "/v1/windows/{window_id}/close",
    }
)


def stable_operation_kind(value: object) -> str | None:
    """Return only a daemon-owned stable route label safe for public metadata."""
    return value if isinstance(value, str) and value in STABLE_OPERATION_KINDS else None
