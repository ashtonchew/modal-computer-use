from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.models import ActionResult


def _settings(tmp_path, *, browser_prewarm: bool = True) -> DaemonSettings:
    return DaemonSettings(
        backend="mock",
        artifacts_dir=tmp_path / "artifacts",
        recordings_dir=tmp_path / "recordings",
        runtime_dir=tmp_path / "runtime",
        local_token="dev",
        browser_prewarm=browser_prewarm,
        readiness_cache_ttl_ms=60_000,
    )


@pytest.mark.asyncio
async def test_lifespan_overlaps_backend_readiness_and_browser_start_after_supervisor(
    tmp_path,
) -> None:
    app = create_app(_settings(tmp_path))
    events: list[str] = []
    browser_started = asyncio.Event()
    readiness_started = asyncio.Event()
    readiness_finished = asyncio.Event()

    async def supervisor_start() -> None:
        events.append("supervisor-start")

    async def backend_ready() -> tuple[bool, list[str]]:
        events.append("backend-ready-start")
        readiness_started.set()
        await browser_started.wait()
        events.append("backend-ready-finish")
        readiness_finished.set()
        return True, []

    async def browser_prewarm():
        events.append("browser-prewarm-start")
        browser_started.set()
        await readiness_started.wait()
        await readiness_finished.wait()
        events.append("browser-prewarm-finish")
        return ActionResult(ok=True, message="browser prewarm complete")

    app.state.backend.ready = backend_ready
    app.state.backend.prewarm_browser = browser_prewarm
    app.state.supervisor.start = supervisor_start

    async def run_lifespan() -> None:
        async with app.router.lifespan_context(app):
            assert events[:1] == ["supervisor-start"]
            assert readiness_finished.is_set()

    await asyncio.wait_for(run_lifespan(), timeout=1)

    assert events == [
        "supervisor-start",
        "backend-ready-start",
        "browser-prewarm-start",
        "backend-ready-finish",
        "browser-prewarm-finish",
    ]


def test_lifespan_seeds_generation_aware_readiness_before_readyz(tmp_path) -> None:
    app = create_app(_settings(tmp_path))
    calls = 0

    async def backend_ready() -> tuple[bool, list[str]]:
        nonlocal calls
        calls += 1
        return True, []

    app.state.backend.ready = backend_ready
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        assert client.get("/readyz").status_code == 200
        assert client.get("/readyz").status_code == 200

    assert calls == 1


def test_lifespan_does_not_freeze_failed_startup_readiness(tmp_path) -> None:
    app = create_app(_settings(tmp_path))
    calls = 0
    outcomes = iter(
        [
            (False, ["startup readiness failed"]),
            (True, []),
        ]
    )

    async def backend_ready() -> tuple[bool, list[str]]:
        nonlocal calls
        calls += 1
        return next(outcomes)

    app.state.backend.ready = backend_ready
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.get("/readyz")
        assert response.status_code == 200

    assert calls == 2
