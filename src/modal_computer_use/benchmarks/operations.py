from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..client import DaemonClient
from .constants import (
    ACTION_BATCH_ACTIONS,
    COMMAND_ECHO_COMMAND,
    MOVE_CLICK_ACTIONS,
    MOVE_CLICK_SEQUENCE_ACTIONS,
    SANDBOX_EXEC_MOVE_CLICK_COMMAND,
)
from .safety import (
    _ensure_ok_result,
    _extract_daemon_ms,
    _is_timeout_exception,
    _recording_id,
    _safe_recording_result,
    _safe_screenshot_result,
    _SandboxExecBenchmarkError,
)


class _ActionBatchBenchmark:
    def __init__(self, client: DaemonClient) -> None:
        self._client = client

    def run_batch(self) -> dict[str, float | None]:
        result = self._client.post_json(
            "/v1/actions/run",
            json={"actions": ACTION_BATCH_ACTIONS, "source": "benchmark"},
        )
        _ensure_ok_result(result)
        return {"daemon_ms": _extract_daemon_ms(result)}

    def run_separate(self) -> dict[str, float | None]:
        daemon_samples: list[float | None] = []
        for action in ACTION_BATCH_ACTIONS:
            result = self._client.post_json(
                "/v1/actions/run",
                json={"actions": [action], "source": "benchmark"},
            )
            _ensure_ok_result(result)
            daemon_samples.append(_extract_daemon_ms(result))
        if any(sample is None for sample in daemon_samples):
            return {"daemon_ms": None}
        return {"daemon_ms": sum(sample for sample in daemon_samples if sample is not None)}

class _MoveClickBenchmark:
    def __init__(self, client: DaemonClient) -> None:
        self._client = client

    def run(self) -> dict[str, float | None]:
        result = self._client.post_json(
            "/v1/actions/run",
            json={"actions": MOVE_CLICK_ACTIONS, "source": "benchmark"},
        )
        _ensure_ok_result(result)
        return {"daemon_ms": _extract_daemon_ms(result)}

class _MoveClickSequenceBenchmark:
    def __init__(self, client: DaemonClient) -> None:
        self._client = client

    def run(self) -> dict[str, float | None]:
        result = self._client.post_json(
            "/v1/actions/run",
            json={"actions": MOVE_CLICK_SEQUENCE_ACTIONS, "source": "benchmark"},
        )
        _ensure_ok_result(result)
        return {"daemon_ms": _extract_daemon_ms(result)}

class _TypeCharsBenchmark:
    def __init__(
        self,
        client: DaemonClient,
        text: str,
        *,
        method: str,
        timeout_ms: int | None = None,
    ) -> None:
        self._client = client
        self._text = text
        self._method = method
        self._timeout_ms = timeout_ms

    def run(self) -> dict[str, float | None]:
        action: dict[str, Any] = {
            "type": "type",
            "text": self._text,
            "method": self._method,
        }
        if self._timeout_ms is not None:
            action["timeout_ms"] = self._timeout_ms
        result = self._client.post_json(
            "/v1/actions/run",
            json={
                "actions": [action],
                "source": "benchmark",
            },
        )
        _ensure_ok_result(result)
        return {"daemon_ms": _extract_daemon_ms(result)}

class _CommandEchoBenchmark:
    def __init__(self, client: DaemonClient) -> None:
        self._client = client

    def run(self) -> dict[str, Any]:
        result = self._client.post_json(
            "/v1/commands/run",
            json={"command": list(COMMAND_ECHO_COMMAND), "timeout": 30},
        )
        _ensure_ok_result(result)
        output = result.get("output") if isinstance(result, dict) else {}
        return {"exit_code": output.get("returncode") if isinstance(output, dict) else None}

class _ScreenshotBenchmark:
    def __init__(self, client: DaemonClient, request: dict[str, Any]) -> None:
        self._client = client
        self._request = request

    def run(self) -> dict[str, Any]:
        result = self._client.post_json("/v1/screenshots/full", json=self._request)
        return _safe_screenshot_result(result)

class _RecordingStartStopBenchmark:
    def __init__(self, client: DaemonClient) -> None:
        self._client = client

    def run(self) -> dict[str, Any]:
        started = self.start()
        return self.stop(started)

    def start(self) -> Any:
        return self._client.post_json(
            "/v1/recordings",
            json={"name": "benchmark", "fps": 5, "format": "mp4"},
        )

    def stop(self, started: Any) -> dict[str, Any]:
        recording_id = _recording_id(started)
        stopped = self._client.post_json(f"/v1/recordings/{recording_id}/stop")
        return _safe_recording_result(stopped)

class _SandboxExecBenchmark:
    def __init__(self, runner: Callable[[tuple[str, ...], int], object]) -> None:
        self._runner = runner

    def run(self) -> None:
        try:
            process = self._runner(SANDBOX_EXEC_MOVE_CLICK_COMMAND, 10)
        except Exception as exc:
            if _is_timeout_exception(exc):
                raise _SandboxExecBenchmarkError(
                    "sandbox_exec_timeout",
                    "Sandbox.exec command timed out",
                ) from exc
            raise _SandboxExecBenchmarkError(
                "sandbox_exec_start_failed",
                "Sandbox.exec failed before returning a process handle",
            ) from exc
        try:
            wait = getattr(process, "wait", None)
            wait_result = wait() if callable(wait) else None
        except Exception as exc:
            if not _is_timeout_exception(exc):
                raise _SandboxExecBenchmarkError(
                    "sandbox_exec_wait_failed",
                    "Sandbox.exec process wait failed",
                ) from exc
            raise _SandboxExecBenchmarkError(
                "sandbox_exec_timeout",
                "Sandbox.exec command timed out",
            ) from exc
        return_code = getattr(process, "returncode", None)
        if return_code is None and isinstance(wait_result, int):
            return_code = wait_result
        if return_code == 127:
            raise _SandboxExecBenchmarkError(
                "sandbox_exec_missing_tool",
                "Sandbox.exec command could not find xdotool in the sandbox",
            )
        if return_code not in (None, 0):
            raise _SandboxExecBenchmarkError(
                "sandbox_exec_nonzero_exit",
                "Sandbox.exec command exited nonzero",
            )
