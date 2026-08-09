from __future__ import annotations

import asyncio

import httpx
import pytest


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/v1/keyboard/press", {"key": "a", "modifiers": ["shift"]}),
        ("POST", "/v1/mouse/move", {"x": 1, "y": 2}),
        (
            "POST",
            "/v1/mouse/drag",
            {"start_x": 1, "start_y": 2, "end_x": 3, "end_y": 4},
        ),
        ("GET", "/v1/mouse/position", None),
        ("POST", "/v1/actions/run", {"actions": [{"type": "move", "x": 1, "y": 2}]}),
    ],
    ids=("keyboard", "pointer", "drag", "pointer-position", "batch"),
)
def test_public_input_operations_wait_for_the_input_lock(
    app,
    method: str,
    path: str,
    payload: dict[str, object] | None,
) -> None:
    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                headers={"Authorization": "Bearer dev"},
            ) as client:
                await app.state.input_lock.acquire()
                request_task = asyncio.create_task(client.request(method, path, json=payload))
                try:
                    await asyncio.sleep(0.02)
                    assert not request_task.done()
                finally:
                    app.state.input_lock.release()

                response = await request_task
                assert response.status_code == 200

    asyncio.run(exercise())
