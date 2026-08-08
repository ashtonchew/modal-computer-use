from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any

import pytest

from modal_computer_use.benchmarks.promotion_gate import (
    CANDIDATE_ARM,
    PRIOR_PUBLIC_ARM,
    build_interleaved_schedule,
    validate_promotion_artifact,
)
from modal_computer_use.benchmarks.promotion_measurement import (
    CandidateDefaultAdapter,
    PriorPublicCompatibilityAdapter,
    measure_interleaved_promotion,
)
from modal_computer_use.errors import DaemonHTTPError
from modal_computer_use.models import (
    ActionBatchResult,
    ActionItemResult,
    CoordinateSpace,
    Screenshot,
)

IMAGE_BYTES = b"\x89PNG\r\n\x1a\nsynthetic-frame"


def _screenshot(*, binary: bool) -> Screenshot:
    fields: dict[str, Any] = (
        {"bytes": IMAGE_BYTES}
        if binary
        else {"data_base64": base64.b64encode(IMAGE_BYTES).decode("ascii")}
    )
    return Screenshot(
        format="png",
        width=1,
        height=1,
        size_bytes=len(IMAGE_BYTES),
        coordinate_space=CoordinateSpace.from_dimensions(
            desktop_width=1,
            desktop_height=1,
        ),
        **fields,
    )


class _RecordingScreenshots:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.full_calls: list[dict[str, Any]] = []
        self.json_compat_calls: list[dict[str, Any]] = []

    async def full(self, **kwargs: Any) -> Screenshot:
        self.order.append(CANDIDATE_ARM)
        self.full_calls.append(kwargs)
        return _screenshot(binary=True)

    async def _full_json_inline_compat(self, **kwargs: Any) -> Screenshot:
        self.order.append(PRIOR_PUBLIC_ARM)
        self.json_compat_calls.append(kwargs)
        return _screenshot(binary=False)


class _RecordingClient:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.post_calls: list[tuple[str, dict[str, Any]]] = []

    async def post_json(self, path: str, *, json: dict[str, Any]) -> dict[str, Any]:
        self.order.append(PRIOR_PUBLIC_ARM)
        self.post_calls.append((path, json))
        return _screenshot(binary=False).model_dump(mode="json")


class _RecordingActions:
    def __init__(self) -> None:
        self.run_calls: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []
        self.failure: Exception | None = None

    async def run(self, actions: list[dict[str, Any]], **kwargs: Any) -> ActionBatchResult:
        self.run_calls.append((actions, kwargs))
        if self.failure is not None:
            raise self.failure
        return ActionBatchResult(
            ok=True,
            results=[
                ActionItemResult(
                    index=index,
                    type=str(action["type"]),
                    ok=True,
                    output={"input_backend": "xtest", "daemon_ms": 1.0},
                )
                for index, action in enumerate(actions)
            ],
        )


class _RecordingComputer:
    def __init__(self) -> None:
        self.order: list[str] = []
        self.client = _RecordingClient(self.order)
        self.screenshots = _RecordingScreenshots(self.order)
        self.actions = _RecordingActions()


def _base_configuration() -> dict[str, Any]:
    return deepcopy(
        {
            "caller_topology": "one-application-owned-modal-function",
            "target_identity": "target-sha-abc123",
            "requested_placement": {"cloud": "aws", "region": "us-west-2"},
            "observed_placement": {
                "function": {"cloud": "aws", "region": "us-west-2"},
                "target": {"cloud": "aws", "region": "us-west-2"},
            },
            "resources": {"cpu": 1, "memory_mib": 2048},
            "image_identity": "image-sha-def456",
            "ingress": "attested-tunnel",
            "http_version": "1.1",
            "input_backend": "xtest",
            "screenshot": {"format": "png", "show_cursor": False},
            "action_payload_sha256": "a" * 64,
            "warmup_iterations": 1,
            "connection_reuse": "one-pooled-async-client",
            "timeout_ms": 5000,
            "warm_capacity": {
                "function_min_containers": 0,
                "sandbox_pool_capacity": 0,
            },
        }
    )


@pytest.mark.asyncio
async def test_measurement_follows_interleaved_schedule_in_one_borrow() -> None:
    computer = _RecordingComputer()
    borrow_calls = 0

    @asynccontextmanager
    async def borrow() -> Any:
        nonlocal borrow_calls
        borrow_calls += 1
        yield computer

    actions = [
        {"type": "click", "x": 10, "y": 20, "button": "left"},
        {"type": "wait", "duration_ms": 0},
    ]
    artifacts = await measure_interleaved_promotion(
        borrow,
        actions=actions,
        configuration=_base_configuration(),
        sample_count=30,
        warmup_iterations=1,
        schedule_seed=42,
        lifecycle_timings={"cold_start_ms": 10.0, "startup_ms": 2.0, "dispatch_ms": 1.0},
    )

    expected_order = [
        str(row["arm"])
        for row in build_interleaved_schedule(
            samples_per_arm=30,
            warmup_iterations=1,
            seed=42,
        )
    ]
    assert computer.order == expected_order
    assert borrow_calls == 1
    assert len(computer.actions.run_calls) == 62
    assert all(call[0] == actions for call in computer.actions.run_calls)
    assert all(call[1]["screenshot_after"] is False for call in computer.actions.run_calls)

    assert len(computer.screenshots.full_calls) == 31
    assert all(call["storage"] == "inline" for call in computer.screenshots.full_calls)
    assert len(computer.screenshots.json_compat_calls) == 31
    assert all(
        "storage" not in call for call in computer.screenshots.json_compat_calls
    )

    for arm, artifact in artifacts.items():
        validate_promotion_artifact(artifact, expected_arm=arm)
        assert artifact["status"] == "complete"
        assert len(artifact["observations"]) == 30
        assert all(row["borrow_count"] == 1 for row in artifact["observations"])
        assert all(row["frame_valid"] is True for row in artifact["observations"])


@pytest.mark.asyncio
async def test_measurement_stops_after_first_possible_dispatch_failure() -> None:
    computer = _RecordingComputer()
    computer.actions.failure = TimeoutError("secret request text")

    @asynccontextmanager
    async def borrow() -> Any:
        yield computer

    artifacts = await measure_interleaved_promotion(
        borrow,
        actions=[{"type": "click", "x": 10, "y": 20}],
        configuration=_base_configuration(),
        sample_count=30,
        warmup_iterations=0,
        schedule_seed=42,
        lifecycle_timings={"cold_start_ms": 1.0, "startup_ms": 1.0, "dispatch_ms": 1.0},
    )

    assert len(computer.actions.run_calls) == 1
    assert sum(len(value["failures"]) for value in artifacts.values()) == 1
    assert "secret request text" not in str(artifacts)
    assert "timeout" in str(artifacts)


@pytest.mark.asyncio
async def test_measurement_reports_a_fixed_screenshot_failure_category() -> None:
    computer = _RecordingComputer()

    async def fail_screenshot(**_kwargs: Any) -> Screenshot:
        raise RuntimeError("secret response detail")

    computer.screenshots._full_json_inline_compat = fail_screenshot

    @asynccontextmanager
    async def borrow() -> Any:
        yield computer

    artifacts = await measure_interleaved_promotion(
        borrow,
        actions=[{"type": "move", "x": 1, "y": 1}],
        configuration=_base_configuration(),
        sample_count=30,
        warmup_iterations=1,
        schedule_seed=42,
        lifecycle_timings={"cold_start_ms": 1.0, "startup_ms": 1.0, "dispatch_ms": 1.0},
    )

    failures = [failure for artifact in artifacts.values() for failure in artifact["failures"]]
    assert failures == [
        {"phase": "warmup", "sample_index": None, "error_category": "screenshot"}
    ]
    assert "secret response detail" not in str(artifacts)


@pytest.mark.asyncio
async def test_measurement_retains_only_safe_http_status_from_daemon_failure() -> None:
    computer = _RecordingComputer()

    async def fail_screenshot(**_kwargs: Any) -> Screenshot:
        raise DaemonHTTPError(
            "secret daemon response",
            status_code=500,
            code="secret_internal_code",
            details={"secret": "never retain"},
        )

    computer.screenshots._full_json_inline_compat = fail_screenshot

    @asynccontextmanager
    async def borrow() -> Any:
        yield computer

    artifacts = await measure_interleaved_promotion(
        borrow,
        actions=[{"type": "move", "x": 1, "y": 1}],
        configuration=_base_configuration(),
        sample_count=30,
        warmup_iterations=1,
        schedule_seed=42,
        lifecycle_timings={"cold_start_ms": 1.0, "startup_ms": 1.0, "dispatch_ms": 1.0},
    )

    failures = [failure for artifact in artifacts.values() for failure in artifact["failures"]]
    assert failures == [
        {
            "phase": "warmup",
            "sample_index": None,
            "error_category": "screenshot",
            "http_status": 500,
        }
    ]
    assert "secret" not in str(failures)


@pytest.mark.asyncio
async def test_adapters_reject_the_wrong_screenshot_representation() -> None:
    computer = _RecordingComputer()

    async def wrong_candidate(**_kwargs: Any) -> Screenshot:
        return _screenshot(binary=False)

    computer.screenshots.full = wrong_candidate
    with pytest.raises(ValueError, match="byte-backed"):
        await CandidateDefaultAdapter().screenshot(computer)

    async def wrong_prior(**_kwargs: Any) -> Screenshot:
        return _screenshot(binary=True)

    computer.screenshots._full_json_inline_compat = wrong_prior
    with pytest.raises(ValueError, match="JSON/base64-backed"):
        await PriorPublicCompatibilityAdapter().screenshot(computer)


def test_adapters_keep_http1_and_do_not_expose_fused_or_raw_shortcuts() -> None:
    assert CandidateDefaultAdapter.http2 is False
    assert PriorPublicCompatibilityAdapter.http2 is False
    assert not hasattr(CandidateDefaultAdapter, "full_bytes")
    assert not hasattr(CandidateDefaultAdapter, "run_and_screenshot_bytes")
