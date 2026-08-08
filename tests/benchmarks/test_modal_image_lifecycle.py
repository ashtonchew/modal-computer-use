from __future__ import annotations

from typing import Any

from modal_computer_use.benchmarks.image_lifecycle import ImageLifecycleBenchmarkSpec
from modal_computer_use.benchmarks.modal_image_lifecycle import (
    run_modal_image_lifecycle,
    run_modal_image_lifecycle_in_runner,
)
from modal_computer_use.image import ImageCanaryRecord, ImageReleaseRecord


def _release_record() -> ImageReleaseRecord:
    revision = "a" * 40
    return ImageReleaseRecord(
        schema_version=1,
        logical_release="2.0.0",
        source_revision=revision,
        image_variant="standard",
        image_name="modal-computer-use-standard",
        image_tag=revision,
        image_reference=f"modal-computer-use-standard:{revision}",
        workspace_name="test-workspace",
        environment_name="test-environment",
        modal_image_object_id="im-managed",
        pyproject_sha256="b" * 64,
        uv_lock_sha256="c" * 64,
        image_builder_version="2025.06",
        uv_version="0.12.3",
        modal_sdk_version="1.5.3",
        build_app_name="modal-computer-use-image-builds",
        canary=ImageCanaryRecord(
            status="passed",
            checks=(
                "healthz",
                "readyz",
                "version",
                "capabilities",
                "image_object_id",
                "browser",
                "screenshot",
                "cleanup",
            ),
            checked_at="2026-08-08T20:00:00Z",
        ),
        published_at="2026-08-08T20:01:00Z",
    )


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _FakeComputer:
    def __init__(self, *, object_id: str, clock: _Clock, timing: Any) -> None:
        self.object_id = object_id
        self.clock = clock
        self.timing = timing
        self.closed = False

    def __enter__(self) -> _FakeComputer:
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True
        self.clock.advance(0.005)

    def first_valid_frame(self, config: Any, *, timing: Any) -> bytes:
        assert config.desktop.resolution == (1024, 768)
        self.clock.advance(0.010)
        timing.mark("first_valid_frame")
        return b"png"

    def ensure_browser_ready(self, config: Any, *, timing: Any) -> None:
        assert config.browser is None

    def modal_image_object_id(self) -> str:
        return self.object_id

    def runtime_placement(self) -> dict[str, str]:
        return {"cloud": "aws", "region": "us-west-2"}


def test_modal_image_lifecycle_runner_uses_inline_and_exact_release_adapters() -> None:
    clock = _Clock()
    exact_image = object()
    create_calls: list[dict[str, Any]] = []
    computers: list[_FakeComputer] = []

    def resolve_exact_image(object_id: str) -> object:
        assert object_id == "im-managed"
        return exact_image

    def create_computer(**kwargs: Any) -> _FakeComputer:
        create_calls.append(kwargs)
        clock.advance(0.010)
        kwargs["timing"].mark("sandbox_registered")
        clock.advance(0.010)
        kwargs["timing"].mark("authenticated_daemon_ready")
        computer = _FakeComputer(
            object_id="im-inline" if kwargs.get("image") is None else "im-managed",
            clock=clock,
            timing=kwargs["timing"],
        )
        computers.append(computer)
        return computer

    spec = ImageLifecycleBenchmarkSpec(
        source_revision="a" * 40,
        release_record=_release_record(),
        run_kind="pilot",
        samples_per_arm=1,
        warmup_pairs=1,
        schedule_seed=7,
        requested_region="us-west-2",
        cpu=1.0,
        memory_mib=2048,
        sandbox_timeout_seconds=180,
        max_estimated_cost_usd=20.0,
        benchmark_run_id="image-lifecycle-test",
    )

    artifact = run_modal_image_lifecycle_in_runner(
        spec,
        run_tag="image-lifecycle-test",
        runner_placement={"cloud": "aws", "region": "us-west-2"},
        create_computer=create_computer,
        resolve_exact_image=resolve_exact_image,
        clock=clock,
        generated_at=lambda: "2026-08-08T21:00:00Z",
    )

    assert artifact["status"] == "complete"
    assert len(create_calls) == 4
    assert sum(call.get("image") is None for call in create_calls) == 2
    assert sum(call.get("image") is exact_image for call in create_calls) == 2
    assert all(call["app_name"] == "modal-computer-use-image-lifecycle" for call in create_calls)
    assert all(call["config"].resources.cpu == 1.0 for call in create_calls)
    assert all(call["config"].resources.memory_mib == 2048 for call in create_calls)
    assert all(call["cpu"] == (1.0, 1.0) for call in create_calls)
    assert all(call["memory"] == (2048, 2048) for call in create_calls)
    assert all(call["config"].runtime.modal_region == "us-west-2" for call in create_calls)
    assert all(
        call["config"].runtime.modal_environment == "test-environment"
        for call in create_calls
    )
    assert all(call["tags"]["benchmark"] == "modal-image-lifecycle" for call in create_calls)
    assert all(
        set(call["tags"]) == {"benchmark", "benchmark_arm"}
        for call in create_calls
    )
    assert all(computer.closed for computer in computers)


def test_modal_image_lifecycle_dispatches_one_exact_regional_function() -> None:
    calls: list[dict[str, Any]] = []
    exact_image = object()
    spec = ImageLifecycleBenchmarkSpec(
        source_revision="a" * 40,
        release_record=_release_record(),
        run_kind="pilot",
        samples_per_arm=1,
        warmup_pairs=1,
        schedule_seed=7,
        requested_region="us-west-2",
        cpu=1.0,
        memory_mib=2048,
        sandbox_timeout_seconds=180,
        max_estimated_cost_usd=20.0,
        benchmark_run_id="image-lifecycle-test",
    )

    def launch(_entrypoint: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"status": "complete"}

    result = run_modal_image_lifecycle(
        spec,
        function_launcher=launch,
        resolve_image=lambda _record: exact_image,
    )

    assert result == {"status": "complete"}
    assert calls == [
        {
            "config": spec,
            "run_tag": "image-lifecycle-test",
            "app_name": "modal-computer-use-image-lifecycle-runner",
            "region": "us-west-2",
            "environment_name": "test-environment",
            "image": exact_image,
            "cpu": 1.0,
            "memory_mib": 1024,
            "timeout_seconds": 1020,
            "cpu_limit": 1.0,
            "memory_limit_mib": 1024,
            "retries": 0,
        }
    ]
