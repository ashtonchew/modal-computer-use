from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_RUNNER_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "benchmarks"
    / "x11_shm_direct_vs_spawned_runner.py"
)
_SPEC = importlib.util.spec_from_file_location("x11_shm_direct_vs_spawned_runner", _RUNNER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)


def test_target_context_uses_mss_to_avoid_a_third_x11_worker(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def status() -> dict[str, object]:
        return {
            "configured_browser": "chromium",
            "prewarm": True,
            "prewarm_result": {"ok": True},
            "open_url_on_start": runner._FIXTURE_DATA_URL,
            "windows": 1,
        }

    computer = SimpleNamespace(browser=SimpleNamespace(status=status))

    class FakeContext:
        async def __aenter__(self):
            return computer

        async def __aexit__(self, *_args: object) -> None:
            return None

    def create(**kwargs: object) -> FakeContext:
        captured.update(kwargs)
        return FakeContext()

    monkeypatch.setattr(runner.AsyncComputerSandbox, "create", create)

    async def exercise() -> None:
        context = runner._TargetContext()
        await context.__aenter__()
        await context.__aexit__(None, None, None)

    asyncio.run(exercise())

    config = captured["config"]
    assert config.actions.screenshot_capture_source == "mss"


def test_main_refuses_existing_output_before_remote_dispatch(monkeypatch, tmp_path: Path) -> None:
    output = tmp_path / "existing.json"
    output.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(
        runner,
        "local_provenance",
        lambda: pytest.fail("local provenance must not run for an existing output"),
    )

    with pytest.raises(SystemExit, match="already exists"):
        runner.main(str(output))

    assert output.read_text(encoding="utf-8") == "keep"


def test_main_refuses_dangling_symlink_before_remote_dispatch(monkeypatch, tmp_path: Path) -> None:
    output = tmp_path / "artifact.json"
    output.symlink_to(tmp_path / "missing-target.json")
    monkeypatch.setattr(
        runner,
        "local_provenance",
        lambda: pytest.fail("local provenance must not run for a symlink output"),
    )

    with pytest.raises(SystemExit, match="already exists"):
        runner.main(str(output))

    assert output.is_symlink()


def test_child_nonzero_exit_cannot_retain_passed_true(monkeypatch) -> None:
    async def exec_child(*_args: object, **_kwargs: object):
        async def wait() -> int:
            return 17

        async def read() -> str:
            return '{"passed": true, "configured_resources": {"cpu": 9.0, "memory_bytes": 1}}'

        return SimpleNamespace(
            wait=SimpleNamespace(aio=wait),
            stdout=SimpleNamespace(read=SimpleNamespace(aio=read)),
        )

    computer = SimpleNamespace(
        _sandbox=SimpleNamespace(exec=SimpleNamespace(aio=exec_child)),
    )

    result = asyncio.run(runner._run_child(computer))

    assert result["passed"] is False
    assert result["configured_resources"] == {
        "cpu": runner.CPU,
        "memory_bytes": runner.MEMORY_MIB * 1024**2,
    }
