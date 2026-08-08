from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from modal_computer_use import ComputerConfig

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_resolved_cost_and_placement_reports_only_inspectable_choices() -> None:
    config = ComputerConfig(
        runtime={
            "modal_environment": "production",
            "modal_region": "us-west-2",
            "timeout_seconds": 900,
            "idle_timeout_seconds": 300,
            "readiness_timeout_seconds": 180,
        },
        resources={
            "profile": "browser",
            "cpu": 2.0,
            "memory_mib": 4096,
        },
        image={"source": "inline"},
        browser={
            "kind": "chromium",
            "prewarm": False,
            "gpu_mode": "off",
            "profile_dir": "/private/browser-profile",
            "launch_args": ["--password=token-canary"],
            "open_url_on_start": "https://private.example/task",
        },
    )

    assert config.resolved_cost_and_placement() == {
        "modal_environment": "production",
        "modal_region": "us-west-2",
        "sandbox": {
            "cpu": 2.0,
            "memory_mib": 4096,
            "gpu": None,
            "resource_profile": "browser",
            "timeout_seconds": 900,
            "idle_timeout_seconds": 300,
            "readiness_timeout_seconds": 180,
            "image": {
                "source": "inline",
                "revision": None,
                "environment_name": None,
            },
            "browser": {
                "kind": "chromium",
                "prewarm": False,
                "gpu_mode": "off",
            },
        },
    }

    rendered = json.dumps(config.resolved_cost_and_placement(), sort_keys=True)
    assert "token-canary" not in rendered
    assert "private.example" not in rendered
    assert "browser-profile" not in rendered


def test_config_repr_and_validation_errors_hide_secret_bearing_inputs() -> None:
    secret_values = (
        "https://private.example/task?token=url-canary",
        "Bearer token-canary",
        "typed-text-canary",
        "clipboard-text-canary",
    )
    config = ComputerConfig(
        browser={
            "kind": "chromium",
            "profile_dir": "/private/token-canary/profile",
            "launch_args": list(secret_values[1:]),
            "open_url_on_start": secret_values[0],
        }
    )

    rendered = repr(config)
    for value in secret_values:
        assert value not in rendered

    rejected_payload = {
        "browser": {
            "kind": "chromium",
            "launch_args": ["valid", "screenshot-bytes-canary\x00"],
        },
        "artifact_bytes": b"artifact-bytes-canary",
    }
    with pytest.raises(ValidationError) as exc_info:
        ComputerConfig.model_validate(rejected_payload)

    validation_error = f"{exc_info.value!s}\n{exc_info.value!r}"
    assert "screenshot-bytes-canary" not in validation_error
    assert "artifact-bytes-canary" not in validation_error


def test_cost_and_warm_capacity_documentation_states_the_primary_contract() -> None:
    configuration = (REPO_ROOT / "docs" / "configuration.md").read_text()
    performance = (REPO_ROOT / "docs" / "performance.md").read_text()

    assert "resolved_cost_and_placement()" in configuration
    assert "browser URLs, launch arguments, or profile paths" in configuration
    assert "Function CPU, memory, image, retries, timeout, and container limits" in configuration
    assert "No SDK default selects a region, CPU, or memory value" in configuration

    assert "Warm capacity is optional spend" in performance
    assert "It is not article parity" in performance
    assert "`min_containers=0`" in performance
    assert "does not create or fill a Sandbox warm pool" in performance
