from __future__ import annotations

import json
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any

import pytest

from modal_computer_use.benchmarks.input_capacity_gate import (
    CAPACITY_BENCHMARK,
    INPUT_RATE_LIMIT_POLICY,
    InputCapacityGateError,
    InputCapacitySettings,
    build_mixed_input_workload,
    execute_input_capacity_gate,
    run_input_capacity_measurement,
    validate_input_capacity_artifact,
)


def _settings(**overrides: Any) -> InputCapacitySettings:
    values: dict[str, Any] = {
        "requested_cloud": "aws",
        "requested_region": "us-west-2",
        "source_sha": "a" * 40,
        "batches": 8,
        "warmup_batches": 1,
        "cycles_per_batch": 1,
    }
    values.update(overrides)
    return InputCapacitySettings(**values)


def _configuration(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "requested_placement": {"cloud": "aws", "region": "us-west-2"},
        "caller_topology": "one-application-owned-modal-function",
        "observed_placement": {
            "target": {"cloud": "aws", "region": "us-west-2"},
            "function": {"cloud": "aws", "region": "us-west-2"},
        },
        "resources": {
            "sandbox": {"cpu": 1.0, "memory_mib": 2048},
            "function": {"cpu": 1.0, "memory_mib": 2048},
        },
        "image_identity": f"inline-source-{'a' * 40}",
        "ingress": "attested-tunnel",
        "http_version": "1.1",
        "input_backend": "xtest",
        "input_rate_limit_policy": INPUT_RATE_LIMIT_POLICY,
        "input_rate_limit_per_sec": 2_000,
        "input_rate_limit_burst": 4_000,
        "warm_capacity": {"function_min_containers": 0, "sandbox_pool_capacity": 0},
    }
    value.update(overrides)
    return value


class _Clock:
    def __init__(self, elapsed_ms: float = 1.0, tail_elapsed_ms: float | None = None) -> None:
        self.calls = 0
        self.elapsed_ms = elapsed_ms
        self.tail_elapsed_ms = tail_elapsed_ms

    def __call__(self) -> float:
        self.calls += 1
        if self.tail_elapsed_ms is not None and self.calls > 2 + (8 * 2):
            elapsed = self.tail_elapsed_ms
        else:
            elapsed = self.elapsed_ms
        # Each measured pair calls the clock twice.  The exact origin is not
        # important; the delta is deterministic and keeps tests offline.
        return (self.calls * elapsed) / 1000.0


class _FakeLifecycle:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready

    async def status(self) -> dict[str, bool]:
        return {"ready": self.ready}


class _FakeMouse:
    def __init__(self, owner: _FakeComputer) -> None:
        self.owner = owner

    async def position(self) -> dict[str, int]:
        return dict(self.owner.final_point)


class _FakeActions:
    def __init__(self, owner: _FakeComputer, *, reorder: bool = False) -> None:
        self.owner = owner
        self.reorder = reorder

    async def run(self, actions: list[dict[str, Any]], **_: Any) -> dict[str, Any]:
        self.owner.calls += 1
        results: list[dict[str, Any]] = []
        for index, action in enumerate(actions):
            output: dict[str, Any] = {"input_backend": "xtest"}
            if action["type"] in {"move", "click", "drag"}:
                output.update(
                    {
                        "x": action.get("x", action.get("end_x")),
                        "y": action.get("y", action.get("end_y")),
                    }
                )
            if action["type"] == "move":
                self.owner.final_point = {"x": action["x"], "y": action["y"]}
            results.append(
                {
                    "index": index,
                    "type": action["type"],
                    "ok": True,
                    "output": output,
                }
            )
        if self.reorder:
            results[0], results[1] = results[1], results[0]
        return {"ok": True, "results": results}


class _FakeCommands:
    def __init__(
        self,
        *,
        cpu_samples: tuple[float, float] = (10.0, 10.004),
        rss_samples: tuple[int, int] = (100_000_000, 100_000_000),
        malformed: bool = False,
    ) -> None:
        self.cpu_samples = cpu_samples
        self.rss_samples = rss_samples
        self.malformed = malformed
        self.calls = 0

    async def run(self, *_: Any, **__: Any) -> dict[str, Any]:
        index = min(self.calls, 1)
        self.calls += 1
        stdout = "not-json" if self.malformed else json.dumps(
            {
                "cpu_seconds": self.cpu_samples[index],
                "rss_bytes": self.rss_samples[index],
            }
        )
        return {"ok": True, "output": {"stdout": stdout}}


class _FakeComputer:
    def __init__(
        self,
        *,
        ready: bool = True,
        reorder: bool = False,
        commands: _FakeCommands | None = None,
    ) -> None:
        self.calls = 0
        self.final_point = {"x": 0, "y": 0}
        self.lifecycle = _FakeLifecycle(ready=ready)
        self.actions = _FakeActions(self, reorder=reorder)
        self.mouse = _FakeMouse(self)
        self.commands = commands or _FakeCommands()


@pytest.mark.asyncio
async def test_workload_is_mixed_ordered_and_below_batch_limit() -> None:
    workload = build_mixed_input_workload(batches=2, cycles_per_batch=6)

    assert len(workload) == 2
    assert len(workload[0].actions) == 48
    assert workload[0].weighted_tokens > 0
    assert {action["type"] for action in workload[0].actions} >= {
        "move",
        "click",
        "type",
        "scroll",
        "drag",
        "hotkey",
        "keypress",
    }
    assert workload[0].final_point == (
        workload[0].actions[-1]["x"],
        workload[0].actions[-1]["y"],
    )


def test_settings_require_minimum_capacity_and_native_backend() -> None:
    _settings().validate(batch_cost=10)

    with pytest.raises(ValueError, match="1000-token"):
        _settings(input_rate_limit_per_sec=999).validate(batch_cost=10)
    with pytest.raises(ValueError, match="native XTest"):
        _settings(input_backend="xdotool").validate(batch_cost=10)
    with pytest.raises(ValueError, match="minimum 1 CPU"):
        _settings(cpu=2.0).validate(batch_cost=10)
    with pytest.raises(ValueError, match="exceeds the configured burst"):
        _settings(input_rate_limit_burst=1).validate(batch_cost=10)
    with pytest.raises(ValueError, match="exact provider region"):
        _settings(requested_region="us-west").validate(batch_cost=10)


@pytest.mark.asyncio
async def test_measurement_rejects_configuration_drift_before_input() -> None:
    computer = _FakeComputer()
    with pytest.raises(InputCapacityGateError, match="input_rate_limit_burst") as exc_info:
        await run_input_capacity_measurement(
            computer,
            settings=_settings(),
            configuration=_configuration(input_rate_limit_burst=3_999),
            clock=_Clock(),
        )
    assert computer.calls == 0
    assert exc_info.value.category == "configuration_mismatch"


@pytest.mark.asyncio
async def test_measurement_passes_mixed_input_at_target_and_records_sanitized_observations(
) -> None:
    computer = _FakeComputer()
    artifact = await run_input_capacity_measurement(
        computer,
        settings=_settings(),
        configuration=_configuration(),
        clock=_Clock(),
    )

    assert artifact["benchmark"] == CAPACITY_BENCHMARK
    assert artifact["status"] == "complete"
    assert artifact["summary"]["weighted_tokens_per_sec"] >= 1_000
    assert artifact["summary"]["final_point"] == artifact["observations"][-1]["final_point"]
    assert artifact["retries"] == 0
    assert artifact["configuration"]["connection_reuse"] == "one-pooled-async-client"
    assert artifact["summary"]["resources"] == {
        "cpu_seconds_delta": 0.004,
        "cpu_utilization_percent": 50.0,
        "rss_bytes_before": 100_000_000,
        "rss_bytes_after": 100_000_000,
        "rss_growth_bytes": 0,
    }
    rendered = str(artifact).lower()
    assert "authorization" not in rendered
    assert "bearer " not in rendered
    assert "typed_text" not in rendered


@pytest.mark.asyncio
async def test_measurement_rejects_reordered_input() -> None:
    artifact = await run_input_capacity_measurement(
        _FakeComputer(reorder=True),
        settings=_settings(batches=2),
        configuration=_configuration(),
        clock=_Clock(),
    )

    assert artifact["status"] == "failed"
    assert artifact["failures"][0]["category"] == "misordered_input"


@pytest.mark.asyncio
async def test_measurement_rejects_unhealthy_daemon() -> None:
    artifact = await run_input_capacity_measurement(
        _FakeComputer(ready=False),
        settings=_settings(batches=2),
        configuration=_configuration(),
        clock=_Clock(),
    )

    assert artifact["status"] == "failed"
    assert artifact["failures"][0]["category"] == "unhealthy_daemon"
    assert artifact["failures"][0]["evidence"] == {
        "stage": "initial",
        "outcome": "not_ready",
        "status": "unknown",
    }


@pytest.mark.asyncio
async def test_measurement_rejects_material_tail_regression() -> None:
    class TailClock:
        def __init__(self) -> None:
            self.calls = 0
            self.now = 0.0

        def __call__(self) -> float:
            self.calls += 1
            if self.calls % 2 == 0:
                measured_pair = self.calls // 2
                self.now += 0.001 if measured_pair <= 4 else 0.003
            return self.now

    artifact = await run_input_capacity_measurement(
        _FakeComputer(),
        settings=_settings(batches=8, max_tail_regression=1.5),
        configuration=_configuration(),
        clock=TailClock(),
    )

    assert artifact["status"] == "failed"
    assert artifact["failures"][-1]["category"] == "tail_regression"


@pytest.mark.asyncio
async def test_execute_gate_records_borrow_cleanup_failure() -> None:
    @asynccontextmanager
    async def broken_borrow():
        yield _FakeComputer()
        raise RuntimeError("cleanup failed")

    artifact = await execute_input_capacity_gate(
        broken_borrow,
        settings=_settings(batches=2),
        configuration=_configuration(),
        clock=_Clock(),
    )

    assert artifact["status"] == "failed"
    assert artifact["cleanup"] == {"succeeded": False, "survivors": "unknown"}
    assert artifact["failures"][0]["category"] == "cleanup"


@pytest.mark.asyncio
async def test_measurement_does_not_retry_after_operation_error() -> None:
    class FailingActions(_FakeActions):
        async def run(self, actions: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            self.owner.calls += 1
            raise RuntimeError("X11 failure after dispatch")

    computer = _FakeComputer()
    computer.actions = FailingActions(computer)
    artifact = await run_input_capacity_measurement(
        computer,
        settings=_settings(batches=2),
        configuration=_configuration(),
        clock=_Clock(),
    )

    assert artifact["status"] == "failed"
    assert computer.calls == 1
    assert artifact["failures"][0]["category"] == "x11_error"


@pytest.mark.asyncio
async def test_measurement_rejects_resource_saturation() -> None:
    computer = _FakeComputer(
        commands=_FakeCommands(
            cpu_samples=(10.0, 10.009),
            rss_samples=(100_000_000, 200_000_000),
        )
    )

    artifact = await run_input_capacity_measurement(
        computer,
        settings=_settings(),
        configuration=_configuration(),
        clock=_Clock(),
    )

    assert artifact["status"] == "failed"
    assert artifact["failures"][-1]["category"] == "resource_saturation"


@pytest.mark.asyncio
async def test_measurement_rejects_unverifiable_resource_usage() -> None:
    artifact = await run_input_capacity_measurement(
        _FakeComputer(commands=_FakeCommands(malformed=True)),
        settings=_settings(),
        configuration=_configuration(),
        clock=_Clock(),
    )

    assert artifact["status"] == "failed"
    assert artifact["failures"][0]["category"] == "resource_observation"


@pytest.mark.asyncio
async def test_artifact_validator_rejects_incomplete_or_tampered_promotion_evidence() -> None:
    artifact = await run_input_capacity_measurement(
        _FakeComputer(),
        settings=_settings(),
        configuration=_configuration(),
        clock=_Clock(),
    )

    missing_observation = deepcopy(artifact)
    missing_observation["observations"].pop()
    with pytest.raises(ValueError, match="observation count"):
        validate_input_capacity_artifact(missing_observation)

    saturated = deepcopy(artifact)
    saturated["summary"]["resources"]["cpu_utilization_percent"] = 99.0
    with pytest.raises(ValueError, match="resource limits"):
        validate_input_capacity_artifact(saturated)

    dirty_cleanup = deepcopy(artifact)
    dirty_cleanup["cleanup"] = {"succeeded": False, "survivors": 1}
    with pytest.raises(ValueError, match="cleanup"):
        validate_input_capacity_artifact(dirty_cleanup)
