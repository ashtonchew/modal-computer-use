from __future__ import annotations

import base64
import importlib.util
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

if importlib.util.find_spec("modal") is None:
    pytest.skip("Modal benchmark runner requires the optional modal extra", allow_module_level=True)

from modal_computer_use.benchmarks.promotion_measurement import (
    measure_interleaved_promotion,
)
from modal_computer_use.latency import SessionStartupTiming
from modal_computer_use.models import (
    ActionBatchResult,
    ActionItemResult,
    CoordinateSpace,
    Screenshot,
)

ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "scripts" / "run_optimized_default_promotion.py"


def _load_runner() -> Any:
    spec = importlib.util.spec_from_file_location("live_promotion_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_live_runner_normalizes_modal_observed_cloud_labels() -> None:
    runner = _load_runner()

    assert runner._exact_placement(
        {"cloud": "CLOUD_PROVIDER_AWS", "region": "us-west-2"},
        name="target",
        expected_cloud="aws",
        expected_region="us-west-2",
    ) == {"cloud": "aws", "region": "us-west-2"}


class _Screenshots:
    async def full(self, **_kwargs: object) -> Screenshot:
        return _screenshot(binary=True)

    async def _full_json_inline_compat(self, **_kwargs: object) -> Screenshot:
        return _screenshot(binary=False)


class _Actions:
    async def run(
        self,
        actions: list[dict[str, Any]],
        **_kwargs: object,
    ) -> ActionBatchResult:
        return ActionBatchResult(
            ok=True,
            results=[
                ActionItemResult(
                    index=index,
                    type=str(action["type"]),
                    ok=True,
                    output={"input_backend": "xtest", "daemon_ms": 0.5},
                )
                for index, action in enumerate(actions)
            ],
        )


class _Computer:
    screenshots = _Screenshots()
    actions = _Actions()


def _screenshot(*, binary: bool) -> Screenshot:
    payload = b"\x89PNG\r\n\x1a\nframe"
    representation = (
        {"bytes": payload}
        if binary
        else {"data_base64": base64.b64encode(payload).decode("ascii")}
    )
    return Screenshot(
        format="png",
        width=1,
        height=1,
        size_bytes=len(payload),
        coordinate_space=CoordinateSpace.from_dimensions(
            desktop_width=1,
            desktop_height=1,
        ),
        **representation,
    )


class _Owner:
    def __init__(self) -> None:
        self.handle = SimpleNamespace(config_hash="a" * 64)

    def session_handle(self) -> object:
        return self.handle

    async def runtime_placement(self) -> dict[str, str]:
        return {"cloud": "aws", "region": "us-west-2"}


class _Runtime:
    def __init__(self) -> None:
        self.owner_entered = 0
        self.owner_exited = 0
        self.measure_calls = 0
        self.owner = _Owner()

    @asynccontextmanager
    async def own(self, _settings: object, timing: SessionStartupTiming) -> Any:
        self.owner_entered += 1
        timing.mark("sandbox_create_started")
        timing.mark("sandbox_registered")
        timing.mark("attestation_ready")
        try:
            yield self.owner
        finally:
            self.owner_exited += 1

    async def probe(self) -> dict[str, str]:
        return {"cloud": "aws", "region": "us-west-2"}

    async def measure(
        self,
        _handle: object,
        *,
        run_id: str,
        configuration: dict[str, Any],
        lifecycle_timings: dict[str, float],
        settings: object,
    ) -> dict[str, dict[str, Any]]:
        assert run_id
        self.measure_calls += 1

        @asynccontextmanager
        async def borrow() -> Any:
            yield _Computer()

        return await measure_interleaved_promotion(
            borrow,
            actions=[{"type": "move", "x": 32, "y": 32}],
            configuration=configuration,
            lifecycle_timings=lifecycle_timings,
            sample_count=settings.sample_count,
            warmup_iterations=settings.warmup_iterations,
            schedule_seed=settings.schedule_seed,
            bootstrap_seed=settings.bootstrap_seed,
            bootstrap_resamples=settings.bootstrap_resamples,
        )


@pytest.mark.asyncio
async def test_live_runner_owns_dispatches_gates_writes_and_cleans_up(tmp_path: Path) -> None:
    runner = _load_runner()
    runtime = _Runtime()
    settings = runner.PromotionSettings(
        app_name="promotion-test",
        owner="promotion-owner",
        environment="main",
        cloud="aws",
        region="us-west-2",
        source_sha="b" * 40,
    )

    result = await runner.execute_live_promotion(
        runtime,
        settings=settings,
        output_dir=tmp_path,
    )

    assert result["decision"] == "promote"
    assert result["paired_samples"] == 30
    assert runtime.owner_entered == 1
    assert runtime.owner_exited == 1
    assert runtime.measure_calls == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "candidate-default.json",
        "prior-public.json",
        "promotion-decision.json",
    ]
    combined = "".join(path.read_text(encoding="utf-8") for path in tmp_path.iterdir())
    assert "Bearer " not in combined
    assert "http://" not in combined
    assert "https://" not in combined
