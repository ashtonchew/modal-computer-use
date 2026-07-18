from __future__ import annotations

import os
import shlex
from typing import Any

from ..constants import MOVE_CLICK_SEQUENCE_ACTIONS, PROVIDER_BENCHMARK_TEXT, TYPE_1000_CHARS_TEXT
from ..lifecycle import CleanupError
from ..safety import _safe_base_url
from .live import (
    cleanup_provider_sandbox,
    run_product_provider_cases,
    wait_for_provider_screenshot_ready,
)
from .payloads import describe_screenshot_payload
from .results import provider_not_measured, provider_unavailable
from .sdk_support import (
    call_first_available,
    import_provider_module,
    package_version,
    provider_computer_use,
    provider_exit_code,
    provider_numeric_attr,
    provider_stdout,
    safe_provider_metadata_value,
    sanitize_provider_observation,
)
from .verification import (
    TYPE_READBACK_FOCUS_X,
    TYPE_READBACK_FOCUS_Y,
    TYPE_READBACK_TEXT,
    verification_step,
    verify_daytona_cursor_position,
    verify_provider_type_readback,
)


def run_daytona_provider(*, iterations: int, warmup_iterations: int) -> dict[str, Any]:
    provider = "daytona"
    api_key = os.environ.get("DAYTONA_API_KEY")
    if not api_key:
        return provider_not_measured(provider, "DAYTONA_API_KEY is not set")
    try:
        daytona_module = import_provider_module("daytona", "Daytona")
    except ImportError:
        return provider_unavailable(
            provider,
            "install the bench-daytona extra to run Daytona benchmarks",
        )

    snapshot = os.environ.get("DAYTONA_SNAPSHOT")
    metadata = {
        "sdk_package": "daytona",
        "sdk_version": package_version("daytona"),
        "target": safe_provider_metadata_value(os.environ.get("DAYTONA_TARGET")),
        "api_url": _safe_base_url(os.environ.get("DAYTONA_API_URL")),
        "sandbox_source": "snapshot" if snapshot else "default_snapshot",
        "snapshot": safe_provider_metadata_value(snapshot),
        "startup_model": "managed_sandbox_snapshot" if snapshot else "managed_default_snapshot",
        "uses_snapshot_or_template": True,
        "readiness_contract": (
            "daytona.create -> computer_use.start -> first non-empty full-screen screenshot"
        ),
        "setup_included": True,
        "ingress_included": False,
        "first_observation_api": "computer_use.screenshot.take_full_screen",
        "target_kind": "product",
    }
    if not snapshot:
        metadata.update(_daytona_default_resource_metadata())
    driver = DaytonaDriver(
        daytona_module,
        api_key=api_key,
        api_url=os.environ.get("DAYTONA_API_URL"),
        target=os.environ.get("DAYTONA_TARGET"),
        snapshot=snapshot,
    )
    return run_product_provider_cases(
        provider=provider,
        driver=driver,
        cold_cases=("cold_create_to_ready",),
        warm_cases=(
            "screenshot_full",
            "move_click",
            "move_click_sequence",
            "type_100_chars",
            "type_1000_chars",
            "command_echo",
        ),
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        metadata=metadata,
    )


class DaytonaDriver:
    def __init__(
        self,
        daytona_module: Any,
        *,
        api_key: str,
        api_url: str | None,
        target: str | None,
        snapshot: str | None,
    ) -> None:
        self._module = daytona_module
        self._snapshot = snapshot
        config_cls = getattr(daytona_module, "DaytonaConfig", None)
        client_cls = daytona_module.Daytona
        if config_cls is None:
            self._client = client_cls()
        else:
            config_kwargs = {"api_key": api_key}
            if api_url:
                config_kwargs["api_url"] = api_url
            if target:
                config_kwargs["target"] = target
            self._client = client_cls(config_cls(**config_kwargs))

    def create_lifecycle_session(self) -> Any:
        return self._create_sandbox()

    def observe_first_screenshot(self, sandbox: Any) -> dict[str, Any]:
        computer_use = provider_computer_use(sandbox)
        call_first_available(computer_use, ("start",))
        wait_for_provider_screenshot_ready(self.screenshot_full, sandbox)
        return self._status(sandbox)

    def cleanup_session(self, sandbox: Any) -> list[CleanupError]:
        stop_error: Exception | None = None
        try:
            computer_use = provider_computer_use(sandbox)
            call_first_available(computer_use, ("stop",))
        except Exception as exc:
            stop_error = exc
        client_delete_error: Exception | None = None
        try:
            self._client.delete(sandbox)
            return []
        except Exception as exc:
            client_delete_error = exc
        cleanup_errors = cleanup_provider_sandbox(sandbox)
        if cleanup_errors and client_delete_error is not None:
            cleanup_errors.insert(0, ("client.delete", client_delete_error))
        if cleanup_errors and stop_error is not None:
            cleanup_errors.insert(0, ("computer_use.stop", stop_error))
        return cleanup_errors

    def screenshot_full(self, sandbox: Any) -> dict[str, Any]:
        screenshot = call_first_available(
            provider_computer_use(sandbox).screenshot,
            ("take_full_screen", "full_screen", "take"),
        )
        payload = describe_screenshot_payload(screenshot)
        size_bytes = payload.get("decoded_size_bytes") or payload.get("transport_size_bytes") or 0
        if size_bytes <= 0:
            raise RuntimeError("Daytona screenshot was empty")
        return {"size_bytes": size_bytes, "payload": payload}

    def move_click(self, sandbox: Any) -> dict[str, Any]:
        mouse = provider_computer_use(sandbox).mouse
        call_first_available(mouse, ("move", "move_to"), 24, 24)
        call_first_available(mouse, ("click", "left_click"), 24, 24)
        return {"action_count": 2}

    def move_click_sequence(self, sandbox: Any) -> dict[str, Any]:
        mouse = provider_computer_use(sandbox).mouse
        for action in MOVE_CLICK_SEQUENCE_ACTIONS:
            if action["type"] == "move":
                call_first_available(mouse, ("move", "move_to"), action["x"], action["y"])
            elif action["type"] == "click":
                call_first_available(
                    mouse,
                    ("click", "left_click"),
                    action["x"],
                    action["y"],
                )
        return {"action_count": len(MOVE_CLICK_SEQUENCE_ACTIONS)}

    def type_100_chars(self, sandbox: Any) -> dict[str, Any]:
        keyboard = provider_computer_use(sandbox).keyboard
        call_first_available(keyboard, ("type", "write"), PROVIDER_BENCHMARK_TEXT)
        return {"character_count": len(PROVIDER_BENCHMARK_TEXT), "method": "provider_default"}

    def type_1000_chars(self, sandbox: Any) -> dict[str, Any]:
        keyboard = provider_computer_use(sandbox).keyboard
        call_first_available(keyboard, ("type", "write"), TYPE_1000_CHARS_TEXT)
        return {"character_count": len(TYPE_1000_CHARS_TEXT), "method": "provider_default"}

    def command_echo(self, sandbox: Any) -> dict[str, Any]:
        result = sandbox.process.exec("sh -lc 'printf 42'", timeout=30)
        exit_code = provider_exit_code(result)
        if exit_code not in (None, 0):
            raise RuntimeError("Daytona command exited nonzero")
        return {"exit_code": exit_code}

    def _create_sandbox(self) -> Any:
        create = self._client.create
        if self._snapshot:
            params_cls = getattr(self._module, "CreateSandboxFromSnapshotParams", None)
            if params_cls is None:
                raise RuntimeError("Daytona SDK did not expose snapshot creation params")
            return create(params_cls(snapshot=self._snapshot))
        return create()

    def _status(self, sandbox: Any) -> dict[str, Any]:
        computer_use = provider_computer_use(sandbox)
        status_method = getattr(computer_use, "get_status", None)
        if callable(status_method):
            return {
                "status": "ready",
                "computer_use": sanitize_provider_observation(status_method()),
            }
        return {"status": "ready"}

    def resource_metadata(self, sandbox: Any) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        cpu = provider_numeric_attr(sandbox, ("cpu", "cpu_count"))
        memory = provider_numeric_attr(sandbox, ("memory", "memory_gib"))
        disk = provider_numeric_attr(sandbox, ("disk", "storage_gib"))
        if cpu is not None:
            metadata["cpu_count"] = cpu
            metadata["cpu_count_source"] = "provider_sandbox_metadata"
        if memory is not None:
            metadata["memory_gib"] = memory
            metadata["memory_gib_source"] = "provider_sandbox_metadata"
        if disk is not None:
            metadata["storage_gib"] = disk
            metadata["storage_gib_source"] = "provider_sandbox_metadata"
        return metadata

    def verify_readbacks(self, sandbox: Any) -> dict[str, Any]:
        def run_command(command: str, timeout: int) -> str:
            return self._run_command(sandbox, command, timeout=timeout)

        def focus_target() -> None:
            call_first_available(
                provider_computer_use(sandbox).mouse,
                ("click", "left_click"),
                TYPE_READBACK_FOCUS_X,
                TYPE_READBACK_FOCUS_Y,
            )

        def type_text(text: str) -> Any:
            return call_first_available(
                provider_computer_use(sandbox).keyboard,
                ("type", "write"),
                text,
            )

        return {
            "cursor_position": verification_step(
                lambda: verify_daytona_cursor_position(sandbox),
                redacted_text=None,
            ),
            "type_text": verification_step(
                lambda: verify_provider_type_readback(
                    type_text=type_text,
                    focus_target=focus_target,
                    run_command=run_command,
                ),
                redacted_text=TYPE_READBACK_TEXT,
            ),
        }

    def _run_command(self, sandbox: Any, command: str, *, timeout: int) -> str:
        result = sandbox.process.exec(f"sh -lc {shlex.quote(command)}", timeout=timeout)
        exit_code = provider_exit_code(result)
        if exit_code not in (None, 0):
            raise RuntimeError("provider readback command exited nonzero")
        return provider_stdout(result)


def _daytona_default_resource_metadata() -> dict[str, Any]:
    return {
        "cpu_count": 1,
        "cpu_count_source": "public_default_sandbox_resources",
        "memory_gib": 1,
        "memory_gib_source": "public_default_sandbox_resources",
        "storage_gib": 3,
        "storage_gib_source": "public_default_sandbox_resources",
        "cost_notes": [
            "Daytona default sandbox resources are documented as 1 vCPU, 1 GiB memory, "
            "and 3 GiB disk; storage estimate does not account for account-level free allowance"
        ],
    }
