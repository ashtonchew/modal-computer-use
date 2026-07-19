from __future__ import annotations

import os
from typing import Any

from ..constants import MOVE_CLICK_SEQUENCE_ACTIONS, PROVIDER_BENCHMARK_TEXT, TYPE_1000_CHARS_TEXT
from ..lifecycle import CleanupError
from .live import (
    cleanup_provider_sandbox,
    run_product_provider_cases,
    wait_for_provider_screenshot_ready,
)
from .payloads import describe_screenshot_payload
from .provider_sdk import (
    call_first_available,
    import_provider_module,
    package_version,
    provider_exit_code,
    provider_stdout,
    safe_provider_metadata_value,
)
from .results import provider_not_measured, provider_unavailable
from .verification import (
    TYPE_READBACK_TEXT,
    verification_step,
    verify_provider_cursor_position,
    verify_provider_type_readback,
)


def run_e2b_provider(*, iterations: int, warmup_iterations: int) -> dict[str, Any]:
    provider = "e2b"
    api_key = os.environ.get("E2B_API_KEY")
    if not api_key:
        return provider_not_measured(provider, "E2B_API_KEY is not set")
    try:
        e2b_module = import_provider_module("e2b_desktop", "Sandbox")
    except ImportError:
        return provider_unavailable(provider, "install the bench-e2b extra to run E2B benchmarks")

    template = os.environ.get("E2B_TEMPLATE")
    metadata = {
        "sdk_package": "e2b-desktop",
        "sdk_version": package_version("e2b-desktop"),
        "template": safe_provider_metadata_value(template),
        "template_source": "explicit" if template else "default_desktop",
        "startup_model": "desktop_template_snapshot",
        "uses_snapshot_or_template": True,
        "readiness_contract": "Sandbox.create -> first non-empty screenshot",
        "setup_included": True,
        "ingress_included": False,
        "first_observation_api": "Sandbox.screenshot",
        "target_kind": "product",
        "resolution": "1024x768",
        "dpi": 96,
        "display": ":0",
        "cpu_count": 2,
        "cpu_count_source": "public_default_desktop_pricing",
        "memory_gib": 1,
        "memory_gib_source": "public_default_desktop_pricing",
    }
    driver = E2BDriver(e2b_module, template=template)
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


class E2BDriver:
    def __init__(self, e2b_module: Any, *, template: str | None) -> None:
        self._sandbox_cls = e2b_module.Sandbox
        self._template = template
        self._move_click_count = 0

    def create_lifecycle_session(self) -> Any:
        return self._create_sandbox()

    def observe_first_screenshot(self, sandbox: Any) -> dict[str, Any]:
        wait_for_provider_screenshot_ready(self.screenshot_full, sandbox)
        return {"status": "ready"}

    def cleanup_session(self, sandbox: Any) -> list[CleanupError]:
        return cleanup_provider_sandbox(sandbox)

    def screenshot_full(self, sandbox: Any) -> dict[str, Any]:
        screenshot = sandbox.screenshot()
        payload = describe_screenshot_payload(screenshot)
        size_bytes = payload.get("decoded_size_bytes") or payload.get("transport_size_bytes") or 0
        if size_bytes <= 0:
            raise RuntimeError("E2B screenshot was empty")
        return {"size_bytes": size_bytes, "payload": payload}

    def move_click(self, sandbox: Any) -> dict[str, Any]:
        offset = self._move_click_count % 2
        self._move_click_count += 1
        call_first_available(sandbox, ("move_mouse", "moveMouse"), 24 + offset, 24 + offset)
        call_first_available(sandbox, ("left_click", "leftClick"))
        return {"action_count": 2}

    def move_click_sequence(self, sandbox: Any) -> dict[str, Any]:
        for action in MOVE_CLICK_SEQUENCE_ACTIONS:
            if action["type"] == "move":
                call_first_available(
                    sandbox,
                    ("move_mouse", "moveMouse"),
                    action["x"],
                    action["y"],
                )
            elif action["type"] == "click":
                call_first_available(sandbox, ("left_click", "leftClick"))
        return {"action_count": len(MOVE_CLICK_SEQUENCE_ACTIONS)}

    def type_100_chars(self, sandbox: Any) -> dict[str, Any]:
        call_first_available(sandbox, ("write", "type"), PROVIDER_BENCHMARK_TEXT)
        return {"character_count": len(PROVIDER_BENCHMARK_TEXT), "method": "provider_default"}

    def type_1000_chars(self, sandbox: Any) -> dict[str, Any]:
        call_first_available(sandbox, ("write", "type"), TYPE_1000_CHARS_TEXT)
        return {"character_count": len(TYPE_1000_CHARS_TEXT), "method": "provider_default"}

    def command_echo(self, sandbox: Any) -> dict[str, Any]:
        commands = getattr(sandbox, "commands", None)
        if commands is None:
            raise RuntimeError("E2B sandbox did not expose commands")
        run = getattr(commands, "run", None)
        if not callable(run):
            raise RuntimeError("E2B sandbox commands did not expose run")
        try:
            result = run("sh -lc 'printf 42'", timeout=30)
        except TypeError:
            result = run("sh -lc 'printf 42'")
        exit_code = provider_exit_code(result)
        if exit_code not in (None, 0):
            raise RuntimeError("E2B command exited nonzero")
        return {"exit_code": exit_code}

    def _create_sandbox(self) -> Any:
        create = self._sandbox_cls.create
        create_kwargs: dict[str, Any] = {
            "resolution": (1024, 768),
            "dpi": 96,
            "display": ":0",
            "timeout": 300,
        }
        if self._template:
            create_kwargs["template"] = self._template
        return create(**create_kwargs)

    def verify_readbacks(self, sandbox: Any) -> dict[str, Any]:
        def run_command(command: str, timeout: int) -> str:
            return self._run_command(sandbox, command, timeout=timeout)

        def type_text(text: str) -> Any:
            return call_first_available(sandbox, ("write", "type"), text)

        return {
            "cursor_position": verification_step(
                lambda: verify_provider_cursor_position(run_command),
                redacted_text=None,
            ),
            "type_text": verification_step(
                lambda: verify_provider_type_readback(
                    type_text=type_text,
                    run_command=run_command,
                ),
                redacted_text=TYPE_READBACK_TEXT,
            ),
        }

    def _run_command(self, sandbox: Any, command: str, *, timeout: int) -> str:
        commands = getattr(sandbox, "commands", None)
        if commands is None:
            raise RuntimeError("E2B sandbox did not expose commands")
        run = getattr(commands, "run", None)
        if not callable(run):
            raise RuntimeError("E2B sandbox commands did not expose run")
        try:
            result = run(command, timeout=timeout)
        except TypeError:
            result = run(command)
        exit_code = provider_exit_code(result)
        if exit_code not in (None, 0):
            raise RuntimeError("provider readback command exited nonzero")
        return provider_stdout(result)
