from __future__ import annotations

import importlib.util
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from modal_computer_use.benchmarks.step_promotion_measurement import (
    measure_interleaved_step_promotion,
)
from modal_computer_use.latency import SessionStartupTiming
from modal_computer_use.models import (
    ActionBatchResult,
    ActionBatchTiming,
    ActionItemResult,
    CoordinateSpace,
    Screenshot,
)
from modal_computer_use.steps import ComputerStepTiming

ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "scripts" / "run_step_promotion.py"


def _load_runner() -> Any:
    spec = importlib.util.spec_from_file_location("live_step_promotion_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _screenshot(token: str) -> Screenshot:
    payload = f"frame:{token}".encode()
    return Screenshot(
        format="png",
        width=1,
        height=1,
        size_bytes=len(payload),
        bytes=payload,
        coordinate_space=CoordinateSpace.from_dimensions(desktop_width=1, desktop_height=1),
    )


class _Computer:
    def __init__(self) -> None:
        self.expected = "initial"
        self.actions = SimpleNamespace(run=self.run)
        self.screenshots = SimpleNamespace(full=self.full)

    def _actions(self, actions: list[dict[str, Any]]) -> ActionBatchResult:
        return ActionBatchResult(
            ok=True,
            results=[
                ActionItemResult(
                    index=index,
                    type=str(action["type"]),
                    ok=True,
                    output={"input_backend": "xtest"},
                )
                for index, action in enumerate(actions)
            ],
            timing=ActionBatchTiming(daemon_ms=0.5),
        )

    async def run(self, actions: list[dict[str, Any]], **_kwargs: object) -> ActionBatchResult:
        return self._actions(actions)

    async def full(self, **_kwargs: object) -> Screenshot:
        return _screenshot(self.expected)

    async def step(self, actions: list[dict[str, Any]], **_kwargs: object) -> Any:
        return SimpleNamespace(
            actions=self._actions(actions),
            screenshot=_screenshot(self.expected),
            timing=ComputerStepTiming(
                daemon_ms=0.7,
                action_ms=0.2,
                screenshot_ms=0.4,
                total_ms=0.7,
            ),
        )


class _Owner:
    handle = SimpleNamespace(config_hash="a" * 64)

    def session_handle(self) -> object:
        return self.handle

    async def runtime_placement(self) -> dict[str, str]:
        return {"cloud": "aws", "region": "us-west-2"}


class _Runtime:
    def __init__(self) -> None:
        self.owner_entered = 0
        self.owner_exited = 0
        self.measure_calls = 0

    @asynccontextmanager
    async def own(self, _settings: object, timing: SessionStartupTiming) -> Any:
        self.owner_entered += 1
        timing.mark("sandbox_create_started")
        timing.mark("sandbox_registered")
        timing.mark("attestation_ready")
        try:
            yield _Owner()
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
        computer = _Computer()

        @asynccontextmanager
        async def borrow() -> Any:
            yield computer

        async def prepare(target: _Computer, pair_index: int, arm: str) -> str:
            target.expected = f"{pair_index}:{arm}"
            return target.expected

        return await measure_interleaved_step_promotion(
            borrow,
            actions=[{"type": "click", "x": 32, "y": 32}],
            prepare=prepare,
            verify_frame=lambda frame, token: (
                frame.as_bytes() == f"frame:{token}".encode()
            ),
            configuration=configuration,
            lifecycle_timings=lifecycle_timings,
            sample_count=settings.sample_count,
            warmup_iterations=settings.warmup_iterations,
            schedule_seed=settings.schedule_seed,
            bootstrap_seed=settings.bootstrap_seed,
            bootstrap_resamples=settings.bootstrap_resamples,
        )


def test_step_runner_rejects_implicit_placement_and_small_samples() -> None:
    runner = _load_runner()
    settings = runner.StepPromotionSettings(
        app_name="step-test",
        owner="step-owner",
        environment="main",
        cloud="aws",
        region="us-west",
        source_sha="b" * 40,
        sample_count=99,
    )

    with pytest.raises(ValueError, match=r"exact Modal region|at least 100"):
        settings.validate()


@pytest.mark.asyncio
async def test_step_runner_owns_dispatches_gates_writes_and_cleans_up(tmp_path: Path) -> None:
    runner = _load_runner()
    runtime = _Runtime()
    settings = runner.StepPromotionSettings(
        app_name="step-test",
        owner="step-owner",
        environment="main",
        cloud="aws",
        region="us-west-2",
        source_sha="b" * 40,
    )

    verified: list[str] = []
    result = await runner.execute_live_step_promotion(
        runtime,
        settings=settings,
        output_dir=tmp_path,
        source_verifier=verified.append,
    )

    assert result["decision"] == "reject"
    assert result["paired_samples"] == 100
    assert runtime.owner_entered == 1
    assert runtime.owner_exited == 1
    assert runtime.measure_calls == 1
    assert verified == ["b" * 40]
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "candidate-default.json",
        "prior-public.json",
        "promotion-decision.json",
    ]
    combined = "".join(path.read_text(encoding="utf-8") for path in tmp_path.iterdir())
    assert "Bearer " not in combined
    assert "http://" not in combined
    assert "https://" not in combined


def test_step_runner_source_uses_one_borrow_and_deterministic_frame_freshness() -> None:
    source = RUNNER_PATH.read_text(encoding="utf-8")

    assert source.count("handle.borrow_async(") == 1
    assert "measure_interleaved_step_promotion(" in source
    assert "prepare=prepare_target" in source
    assert "verify_frame=verify_fresh_frame" in source
    assert "computer.browser.open_url(" not in source
    assert "screenshot.cursor_position" in source
    assert 'screenshot.captured_at > token.get("baseline_captured_at")' in source
    assert '"type": "move", "x": 16, "y": 16' in source
    assert "retries=0" in source
    assert "min_containers=0" in source
    assert "max_containers=1" in source
    assert "ACTION_BATCH" in source
    assert "computer.step(" not in source


def test_step_runner_requires_exact_clean_source_revision(monkeypatch) -> None:
    runner = _load_runner()
    calls: list[tuple[str, ...]] = []

    def clean_run(args: list[str], **_kwargs: object) -> Any:
        calls.append(tuple(args[1:]))
        output = ("b" * 40 + "\n") if "rev-parse" in args else ""
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(runner.subprocess, "run", clean_run)
    runner.verify_clean_source_revision("b" * 40)
    assert calls == [
        ("rev-parse", "HEAD"),
        ("status", "--porcelain=v1", "--untracked-files=normal"),
    ]

    def dirty_run(args: list[str], **_kwargs: object) -> Any:
        output = ("b" * 40 + "\n") if "rev-parse" in args else " M private.env\n"
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(runner.subprocess, "run", dirty_run)
    with pytest.raises(ValueError, match="worktree must be clean"):
        runner.verify_clean_source_revision("b" * 40)
