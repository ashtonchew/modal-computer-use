from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from modal_computer_use.benchmarks.modal_optimized_provider import (
    ModalOptimizedProviderConfig,
    _safe_warm_surfaces,
    run_modal_optimized_provider_benchmark,
    run_modal_optimized_provider_in_runner,
    validate_modal_optimized_provider_artifact,
)

REVISION = "a" * 40


def _config(*, iterations: int = 30, warmup_iterations: int = 1, pilot: bool = False):
    return ModalOptimizedProviderConfig(
        region="us-west-2",
        image_revision=REVISION,
        cpu=4.0,
        memory_mib=8192,
        browser="chromium",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        pilot=pilot,
    )


def _warm_surface(iterations: int) -> dict[str, object]:
    cases = {}
    for name in (
        "screenshot_full",
        "coordinate_click",
        "coordinate_click_sequence",
        "type_100_chars",
        "type_1000_chars",
        "command_nonlogin_shell_echo",
    ):
        cases[name] = {
            "status": "ok",
            "iterations": iterations,
            "successful_iterations": iterations,
            "samples_ms": [1.0] * iterations,
            "summary_ms": {"p50": 1.0, "p95": 1.0},
            "failures": [],
        }
        if name in {"type_100_chars", "type_1000_chars"}:
            cases[name]["resolved_methods"] = ["keystrokes"]
            cases[name]["request"] = {
                "character_count": 100 if name == "type_100_chars" else 1000,
                "method": "keystrokes",
                "delay_ms": 0,
                **({"timeout_ms": 30000} if name == "type_1000_chars" else {}),
            }
    return {
        "ok": True,
        "base_url": "https://must-not-serialize.invalid",
        "failures": [],
        "surfaces": {
            "daemon-http": {
                "status": "ok",
                "metadata": {"base_url": "https://must-not-serialize.invalid"},
                "failures": [],
                "cases": {"unrelated_case": {"status": "ok"}, **cases},
                "verification": {
                    "cursor_position": {"status": "ok"},
                    "type_text": {"status": "ok"},
                },
            }
        },
    }


def test_runner_uses_fresh_targets_and_times_through_validated_frame() -> None:
    events: list[str] = []
    created: list[object] = []
    ticks = iter(float(value) for value in range(200))
    warm_surface_kwargs: dict[str, object] = {}

    class Computer:
        client = SimpleNamespace(base_url="https://must-not-serialize.invalid")

        def __init__(self, index: int) -> None:
            self.index = index
            self.screenshots = SimpleNamespace(full_bytes=self.full_bytes)

        def full_bytes(self, **kwargs) -> bytes:
            events.append(f"frame:{self.index}")
            assert kwargs == {"format": "png", "processing": "daemon"}
            return _png_bytes()

        def runtime_placement(self) -> dict[str, str]:
            events.append(f"placement:{self.index}")
            return {"cloud": "CLOUD_PROVIDER_AWS", "region": "us-west-2"}

        def terminate(self, *, wait: bool = False) -> None:
            assert wait is True
            events.append(f"terminate:{self.index}")

        def detach(self) -> None:
            events.append(f"detach:{self.index}")

    def create_computer(**kwargs):
        events.append(f"create:{len(created)}")
        computer = Computer(len(created))
        created.append(computer)
        assert kwargs["wait"] is True
        return computer

    def benchmark_warm_surface(**kwargs):
        warm_surface_kwargs.update(kwargs)
        return _warm_surface(kwargs["iterations"])

    result = run_modal_optimized_provider_in_runner(
        _config(),
        runner_placement={"cloud": "CLOUD_PROVIDER_AWS", "region": "us-west-2"},
        create_computer=create_computer,
        surface_benchmark=benchmark_warm_surface,
        clock=lambda: next(ticks),
    )

    lifecycle = result["product_create"]
    assert lifecycle["successful_warmup_iterations"] == 1
    assert lifecycle["successful_iterations"] == 30
    assert lifecycle["replacement_samples"] == 0
    assert lifecycle["targets_created"] == 31
    assert lifecycle["targets_reused"] == 0
    assert lifecycle["target_placements_verified"] == 31
    assert result["warm_target_placement_verified"] is True
    assert len({id(item) for item in created[:31]}) == 31
    assert len(created) == 32  # one separate warm-operation target
    assert events.index("create:0") < events.index("frame:0") < events.index("placement:0")
    assert events.index("placement:0") < events.index("terminate:0") < events.index("detach:0")
    assert lifecycle["samples_ms"] == [1000.0] * 30
    assert warm_surface_kwargs["typing_method"] == "keystrokes"
    assert warm_surface_kwargs["typing_delay_ms"] == 0
    assert set(result["surfaces"]["daemon-http"]["cases"]) == {
        "screenshot_full",
        "coordinate_click",
        "coordinate_click_sequence",
        "type_100_chars",
        "type_1000_chars",
        "command_nonlogin_shell_echo",
    }
    assert "https://" not in json.dumps(result)


def test_runner_does_not_replace_failed_sample_and_cleanup_is_terminal() -> None:
    calls = 0

    class Computer:
        client = SimpleNamespace(base_url="unused")

        def __init__(self, index: int) -> None:
            self.index = index
            self.screenshots = SimpleNamespace(full_bytes=self.full_bytes)

        def full_bytes(self, **_kwargs) -> bytes:
            if self.index == 2:
                raise RuntimeError("token=do-not-serialize")
            return _png_bytes()

        def runtime_placement(self):
            return {"cloud": "CLOUD_PROVIDER_AWS", "region": "us-west-2"}

        def terminate(self, *, wait: bool = False) -> None:
            if self.index == 3:
                raise RuntimeError("cleanup endpoint https://secret.invalid")

        def detach(self) -> None:
            pass

    def create_computer(**_kwargs):
        nonlocal calls
        result = Computer(calls)
        calls += 1
        return result

    result = run_modal_optimized_provider_in_runner(
        _config(iterations=3, warmup_iterations=1, pilot=True),
        runner_placement={"cloud": "CLOUD_PROVIDER_AWS", "region": "us-west-2"},
        create_computer=create_computer,
        surface_benchmark=lambda **kwargs: _warm_surface(kwargs["iterations"]),
        clock=iter(range(100)).__next__,
    )

    assert calls == 5  # fixed 1+3 lifecycle attempts plus one warm target
    assert result["product_create"]["successful_iterations"] == 1
    assert result["product_create"]["replacement_samples"] == 0
    assert result["ok"] is False
    encoded = json.dumps(result)
    assert "do-not-serialize" not in encoded
    assert "secret.invalid" not in encoded
    assert {failure["phase"] for failure in result["product_create"]["failures"]} == {
        "first_valid_frame",
        "cleanup",
    }


def test_runner_rejects_observed_placement_mismatch() -> None:
    class Computer:
        client = SimpleNamespace(base_url="unused")
        screenshots = SimpleNamespace(full_bytes=lambda **_kwargs: _png_bytes())

        def runtime_placement(self):
            return {"cloud": "gcp", "region": "us-central1-a"}

        def terminate(self, *, wait=False):
            pass

        def detach(self):
            pass

    result = run_modal_optimized_provider_in_runner(
        _config(iterations=1, warmup_iterations=0, pilot=True),
        runner_placement={"cloud": "CLOUD_PROVIDER_AWS", "region": "us-west-2"},
        create_computer=lambda **_kwargs: Computer(),
        surface_benchmark=lambda **kwargs: _warm_surface(kwargs["iterations"]),
        clock=iter(range(100)).__next__,
    )

    assert result["ok"] is False
    assert result["product_create"]["failures"][0] == {
        "phase": "placement_validation",
        "iteration": 0,
        "exception_type": "PlacementMismatchError",
    }


def test_outer_benchmark_dispatches_once_and_pilot_is_ineligible() -> None:
    dispatches = 0

    def launch(entrypoint, *, config, **kwargs):
        nonlocal dispatches
        dispatches += 1
        assert kwargs["retries"] == 0
        return entrypoint(
            config,
            runner_placement={"cloud": "CLOUD_PROVIDER_AWS", "region": "us-west-2"},
            create_computer=lambda **_kwargs: pytest.fail("remote body is stubbed"),
            surface_benchmark=lambda **_kwargs: {},
        )

    remote_result = {
        "ok": True,
        "failures": [],
        "product_create": {
            "name": "product_create_to_first_screenshot",
            "status": "ok",
            "definition": "create through validated frame",
            "iterations": 1,
            "successful_iterations": 1,
            "warmup_iterations": 0,
            "successful_warmup_iterations": 0,
            "replacement_samples": 0,
            "fresh_target_per_attempt": True,
            "targets_created": 1,
            "target_attempts": 1,
            "targets_reused": 0,
            "target_placements_verified": 1,
            "samples_ms": [10.0],
            "summary_ms": {"p50": 10.0, "p95": 10.0},
            "cleanup": {"attempted": 1, "succeeded": 1, "failures": []},
            "failures": [],
        },
        "surfaces": _safe_warm_surfaces(_warm_surface(1)),
        "warm_target_cleanup": {"attempted": True, "succeeded": True, "error_type": None},
        "warm_target_placement_verified": True,
        "runner_placement": {"cloud": "CLOUD_PROVIDER_AWS", "region": "us-west-2"},
    }

    def fake_launch(_entrypoint, **kwargs):
        nonlocal dispatches
        dispatches += 1
        assert kwargs["retries"] == 0
        return remote_result

    result = run_modal_optimized_provider_benchmark(
        _config(iterations=1, warmup_iterations=0, pilot=True),
        function_launcher=fake_launch,
        cleanup_sweep=lambda **_kwargs: {
            "cleanup_succeeded": True,
            "remaining_sandboxes": 0,
        },
        clock=iter(range(10)).__next__,
    )

    assert dispatches == 1
    assert result["eligibility"] == "pilot_ineligible"
    assert result["runner_dispatch"]["included_in_product_create_samples"] is False
    validate_modal_optimized_provider_artifact(result, require_publishable=False)

    failed_product = copy.deepcopy(result)
    failed_product["runs"]["modal_optimized_runner"]["product_create"]["status"] = "failed"
    with pytest.raises(ValueError, match="product create success contract"):
        validate_modal_optimized_provider_artifact(
            failed_product, require_publishable=False
        )

    failed_warm_case = copy.deepcopy(result)
    failed_warm_case["runs"]["modal_optimized_runner"]["surfaces"]["daemon-http"][
        "cases"
    ]["screenshot_full"]["status"] = "failed"
    with pytest.raises(ValueError, match="success contract"):
        validate_modal_optimized_provider_artifact(
            failed_warm_case, require_publishable=False
        )

    with pytest.raises(ValueError, match="publishable"):
        validate_modal_optimized_provider_artifact(result, require_publishable=True)


def test_outer_benchmark_sweeps_after_runner_failure_without_serializing_details() -> None:
    sweeps: list[dict[str, object]] = []

    def fail_launch(*_args, **_kwargs):
        raise RuntimeError("token=must-not-serialize")

    def sweep(**kwargs):
        sweeps.append(kwargs)
        return {"cleanup_succeeded": True, "remaining_sandboxes": 0}

    result = run_modal_optimized_provider_benchmark(
        _config(iterations=1, warmup_iterations=0, pilot=True),
        function_launcher=fail_launch,
        cleanup_sweep=sweep,
        clock=iter(range(10)).__next__,
        run_id_factory=lambda: "safe-test-run",
    )

    assert len(sweeps) == 1
    assert sweeps[0]["run_id"] == "safe-test-run"
    assert result["ok"] is False
    assert result["failures"][0] == {
        "phase": "runner_dispatch",
        "iteration": -1,
        "exception_type": "RuntimeError",
    }
    assert "must-not-serialize" not in json.dumps(result)


def test_publishable_run_rejects_malformed_successful_function_result() -> None:
    with pytest.raises(ValueError, match="optimized runner result"):
        run_modal_optimized_provider_benchmark(
            _config(),
            function_launcher=lambda *_args, **_kwargs: {"ok": True},
            cleanup_sweep=lambda **_kwargs: {
                "cleanup_succeeded": True,
                "remaining_sandboxes": 0,
            },
            clock=iter(range(10)).__next__,
        )


@pytest.mark.parametrize(
    "unsafe",
    [
        {"token": "secret"},
        {"sandbox_id": "sb-1"},
        {"stdout": "oops"},
        {"screenshot_bytes": "abc"},
        {"artifact_bytes": "secret"},
        {"typed_text": "secret"},
        {"clipboard_text": "secret"},
        {"endpoint": "https://secret.invalid/path"},
    ],
)
def test_validator_rejects_unsafe_fields(unsafe: dict[str, str]) -> None:
    payload = {
        "schema_version": 1,
        "benchmark": "modal-optimized-provider",
        "ok": False,
        "eligibility": "pilot_ineligible",
        "iterations": 1,
        "warmup_iterations": 0,
        "replacement_samples": 0,
        "metadata": {},
        "runs": {},
        "runner_dispatch": {
            "elapsed_ms": 1.0,
            "included_in_product_create_samples": False,
        },
        "final_cleanup": {"cleanup_succeeded": True, "remaining_sandboxes": 0},
        "failures": [],
    }
    payload.update(unsafe)
    with pytest.raises(ValueError, match="unsafe"):
        validate_modal_optimized_provider_artifact(payload, require_publishable=False)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("samples_ms", [-1.0]),
        ("samples_ms", [float("nan")]),
        ("successful_iterations", 2),
        ("summary_ms", {"p50": 2.0, "p95": 2.0}),
    ],
)
def test_validator_rejects_invalid_case_measurements(field: str, value: object) -> None:
    case = {
        "status": "ok",
        "iterations": 1,
        "successful_iterations": 1,
        "samples_ms": [1.0],
        "summary_ms": {"p50": 1.0, "p95": 1.0},
        "failures": [],
    }
    case[field] = value
    payload = {
        "schema_version": 1,
        "benchmark": "modal-optimized-provider",
        "ok": False,
        "eligibility": "pilot_ineligible",
        "iterations": 1,
        "warmup_iterations": 0,
        "replacement_samples": 0,
        "metadata": {},
        "runs": {
            "modal_optimized_runner": {
                "product_create": case,
                "surfaces": {},
            }
        },
        "runner_dispatch": {
            "elapsed_ms": 1.0,
            "included_in_product_create_samples": False,
        },
        "final_cleanup": {"cleanup_succeeded": True, "remaining_sandboxes": 0},
        "failures": [],
    }
    with pytest.raises(ValueError, match="case"):
        validate_modal_optimized_provider_artifact(payload, require_publishable=False)


def _png_bytes() -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (1024, 768), color=(1, 2, 3)).save(buffer, format="PNG")
    return buffer.getvalue()
