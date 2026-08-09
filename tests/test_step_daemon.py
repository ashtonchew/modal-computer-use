from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from modal_computer_use.daemon.input_rate_limit import InputTokenBucket
from modal_computer_use.daemon.leases import (
    LEASE_EPOCH_HEADER,
    LEASE_FENCE_HEADER,
    LEASE_ID_HEADER,
    LEASE_TOKEN_HEADER,
)
from modal_computer_use.daemon.receipts import OPERATION_SEQUENCE_HEADER
from modal_computer_use.models import ActionResult, Point
from modal_computer_use.steps import STEP_MEDIA_TYPE, decode_step_envelope


def _acquire_lease(test_client, run_id: str = "step-test") -> dict[str, str]:
    response = test_client.post("/v1/leases/acquire", json={"run_id": run_id})
    assert response.status_code == 200, response.text
    payload = response.json()
    return {
        LEASE_ID_HEADER: payload["lease_id"],
        LEASE_EPOCH_HEADER: payload["daemon_epoch"],
        LEASE_FENCE_HEADER: str(payload["fence"]),
        LEASE_TOKEN_HEADER: response.headers["x-computer-use-lease-token"],
        OPERATION_SEQUENCE_HEADER: "0",
    }


async def _acquire_async_lease(
    client: httpx.AsyncClient,
    run_id: str,
) -> dict[str, str]:
    response = await client.post("/v1/leases/acquire", json={"run_id": run_id})
    assert response.status_code == 200, response.text
    payload = response.json()
    return {
        LEASE_ID_HEADER: payload["lease_id"],
        LEASE_EPOCH_HEADER: payload["daemon_epoch"],
        LEASE_FENCE_HEADER: str(payload["fence"]),
        LEASE_TOKEN_HEADER: response.headers["x-computer-use-lease-token"],
        OPERATION_SEQUENCE_HEADER: "0",
    }


def test_step_requires_an_active_lease_before_mutation(test_client, app) -> None:
    response = test_client.post(
        "/v1/steps",
        json={"actions": [{"type": "move", "x": 10, "y": 20}]},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "lease_required"
    assert app.state.backend.cursor.x == 0
    assert app.state.backend.cursor.y == 0


def test_step_reserves_the_whole_weighted_batch_before_receipt_or_mutation(
    test_client,
    app,
) -> None:
    app.state.settings = replace(
        app.state.settings,
        input_rate_limit_per_sec=1,
        input_rate_limit_burst=1,
    )
    app.state.input_token_bucket = InputTokenBucket(refill_rate=1, capacity=1)
    headers = _acquire_lease(test_client, run_id="rate-limited-step")

    response = test_client.post(
        "/v1/steps",
        headers=headers,
        json={
            "actions": [
                {"type": "move", "x": 10, "y": 20},
                {"type": "move", "x": 30, "y": 40},
            ]
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "input_cost_exceeds_burst"
    assert app.state.backend.cursor == Point(x=0, y=0)
    assert app.state.action_count == 0
    receipt = test_client.post(
        "/v1/receipts/status",
        headers=headers,
        json={"run_id": "rate-limited-step", "sequence": 0},
    )
    assert receipt.status_code == 200
    assert receipt.json()["state"] == "MISSING"


def test_step_rejects_an_unsupported_accept_before_mutation(test_client, app) -> None:
    response = test_client.post(
        "/v1/steps",
        headers={"Accept": "application/json"},
        json={"actions": [{"type": "move", "x": 10, "y": 20}]},
    )

    assert response.status_code == 406
    assert response.json()["code"] == "step_not_acceptable"
    assert app.state.backend.cursor.x == 0
    assert app.state.backend.cursor.y == 0


def test_step_returns_a_binary_envelope_and_reuses_the_lease(test_client, app) -> None:
    headers = _acquire_lease(test_client)
    response = test_client.post(
        "/v1/steps",
        headers=headers,
        json={"actions": [{"type": "move", "x": 10, "y": 20}]},
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == STEP_MEDIA_TYPE
    assert response.headers["cache-control"] == "no-store, no-transform"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"].startswith("application/")
    assert response.content.startswith(b"MCUSTEP\x00")
    result = decode_step_envelope(response.content)
    assert result.screenshot.cursor_visible is False
    assert result.screenshot.cursor_position == Point(x=10, y=20)
    assert app.state.backend.cursor.x == 10
    assert app.state.backend.cursor.y == 20


def test_step_accepts_client_payload_with_null_screenshot_options(test_client) -> None:
    headers = _acquire_lease(test_client, run_id="null-options")
    response = test_client.post(
        "/v1/steps",
        headers=headers,
        json={
            "actions": [{"type": "move", "x": 10, "y": 20}],
            "screenshot_options": None,
        },
    )

    assert response.status_code == 200, response.text


def test_step_rehydrates_nested_screenshot_segments(test_client) -> None:
    headers = _acquire_lease(test_client)
    response = test_client.post(
        "/v1/steps",
        headers=headers,
        json={
            "actions": [
                {"type": "screenshot", "options": {"format": "png"}},
                {"type": "move", "x": 10, "y": 20},
            ]
        },
    )

    assert response.status_code == 200, response.text
    result = decode_step_envelope(response.content)
    nested = result.actions.results[0].output
    assert nested["format"] == "png"
    assert nested["bytes"].startswith(b"\x89PNG\r\n\x1a\n")
    assert result.screenshot.as_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert result.timing.action_ms is not None
    assert result.timing.screenshot_ms is not None
    assert result.timing.total_ms is not None
    assert result.timing.action_ms >= 0
    assert result.timing.screenshot_ms >= 0
    assert result.timing.total_ms >= result.timing.action_ms


def test_step_returns_immediate_frame_for_known_terminal_action_failure(
    test_client,
    app,
) -> None:
    async def fail_scroll(direction: str, amount: int, *, x: int | None, y: int | None):
        del direction, amount, x, y
        return ActionResult(
            ok=False,
            message="scroll failed",
            output={"code": "scroll_failed"},
        )

    app.state.backend.mouse_scroll = fail_scroll
    headers = _acquire_lease(test_client)
    response = test_client.post(
        "/v1/steps",
        headers=headers,
        json={"actions": [{"type": "scroll", "direction": "down", "amount": 1}]},
    )

    assert response.status_code == 200, response.text
    result = decode_step_envelope(response.content)
    assert result.actions.ok is False
    assert result.actions.results[0].error_code == "scroll_failed"
    assert result.screenshot.as_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    receipt = test_client.post(
        "/v1/receipts/status",
        headers=headers,
        json={"run_id": "step-test", "sequence": 0},
    )
    assert receipt.status_code == 200
    assert receipt.json()["state"] == "COMPLETED"


def test_step_continues_after_known_failure_only_when_explicit(test_client, app) -> None:
    async def fail_scroll(direction: str, amount: int, *, x: int | None, y: int | None):
        del direction, amount, x, y
        return ActionResult(
            ok=False,
            message="scroll failed",
            output={"code": "scroll_failed"},
        )

    app.state.backend.mouse_scroll = fail_scroll
    headers = _acquire_lease(test_client, run_id="continue-step")
    response = test_client.post(
        "/v1/steps",
        headers=headers,
        json={
            "actions": [
                {"type": "scroll", "direction": "down", "amount": 1},
                {"type": "move", "x": 30, "y": 40},
            ],
            "continue_on_error": True,
        },
    )

    assert response.status_code == 200, response.text
    result = decode_step_envelope(response.content)
    assert [item.ok for item in result.actions.results] == [False, True]
    assert result.screenshot.cursor_position == Point(x=30, y=40)
    receipt = test_client.post(
        "/v1/receipts/status",
        headers=headers,
        json={"run_id": "continue-step", "sequence": 0},
    )
    assert receipt.json()["state"] == "COMPLETED"


def test_step_input_timeout_is_indeterminate_and_returns_no_frame(test_client, app) -> None:
    async def hang_move(_x: int, _y: int) -> Point:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    app.state.backend.mouse_move = hang_move
    headers = _acquire_lease(test_client, run_id="timeout-step")
    response = test_client.post(
        "/v1/steps",
        headers=headers,
        json={
            "actions": [{"type": "move", "x": 30, "y": 40}],
            "max_action_timeout_ms": 10,
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "recovery_required"
    assert not response.content.startswith(b"MCUSTEP\x00")
    assert app.state.screenshot_count == 0
    receipt = test_client.post(
        "/v1/receipts/status",
        headers=headers,
        json={"run_id": "timeout-step", "sequence": 0},
    )
    assert receipt.json()["state"] == "INDETERMINATE"
    blocked = test_client.post(
        "/v1/steps",
        headers=headers,
        json={"actions": [{"type": "move", "x": 1, "y": 1}]},
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "recovery_required"


def test_step_observation_failure_completes_receipt_without_replay(
    test_client,
    app,
) -> None:
    async def fail_capture(*_args, **_kwargs):
        raise RuntimeError("capture unavailable")

    app.state.backend.screenshot_bytes = fail_capture
    headers = _acquire_lease(test_client, run_id="observation-error")
    response = test_client.post(
        "/v1/steps",
        headers=headers,
        json={"actions": [{"type": "move", "x": 10, "y": 20}]},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "operation_result_unavailable"
    receipt = test_client.post(
        "/v1/receipts/status",
        headers=headers,
        json={"run_id": "observation-error", "sequence": 0},
    )
    assert receipt.status_code == 200
    assert receipt.json()["state"] == "COMPLETED"


def test_step_observation_cleanup_failure_quarantines_the_run(test_client, app) -> None:
    async def fail_capture(*_args, **_kwargs):
        raise RuntimeError("capture unavailable")

    async def fail_cleanup():
        return ActionResult(
            ok=False,
            message="release failed",
            output={"code": "release_all_incomplete"},
        )

    app.state.backend.screenshot_bytes = fail_capture
    app.state.backend.release_all = fail_cleanup
    headers = _acquire_lease(test_client, run_id="cleanup-error")
    response = test_client.post(
        "/v1/steps",
        headers=headers,
        json={"actions": [{"type": "move", "x": 10, "y": 20}]},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "recovery_required"
    receipt = test_client.post(
        "/v1/receipts/status",
        headers=headers,
        json={"run_id": "cleanup-error", "sequence": 0},
    )
    assert receipt.status_code == 200
    assert receipt.json()["state"] == "INDETERMINATE"


@pytest.mark.asyncio
async def test_step_cancellation_during_input_dispatch_is_indeterminate(app) -> None:
    dispatched = asyncio.Event()

    async def cancel_during_move(_x: int, _y: int) -> Point:
        dispatched.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    app.state.backend.mouse_move = cancel_during_move
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": "Bearer dev"},
    ) as client:
        headers = await _acquire_async_lease(client, "cancel-dispatch")
        request = asyncio.create_task(
            client.post(
                "/v1/steps",
                headers=headers,
                json={"actions": [{"type": "move", "x": 10, "y": 20}]},
            )
        )
        await asyncio.wait_for(dispatched.wait(), timeout=1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        receipt = await client.post(
            "/v1/receipts/status",
            headers=headers,
            json={"run_id": "cancel-dispatch", "sequence": 0},
        )

    assert receipt.status_code == 200
    assert receipt.json()["state"] == "INDETERMINATE"


@pytest.mark.asyncio
async def test_step_cancellation_during_observation_completes_without_replay(app) -> None:
    observing = asyncio.Event()

    async def move(_x: int, _y: int) -> Point:
        return Point(x=10, y=20)

    async def cancel_during_capture(*_args, **_kwargs):
        observing.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    app.state.backend.mouse_move = move
    app.state.backend.screenshot_bytes = cancel_during_capture
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": "Bearer dev"},
    ) as client:
        headers = await _acquire_async_lease(client, "cancel-observation")
        request = asyncio.create_task(
            client.post(
                "/v1/steps",
                headers=headers,
                json={"actions": [{"type": "move", "x": 10, "y": 20}]},
            )
        )
        await asyncio.wait_for(observing.wait(), timeout=1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        receipt = await client.post(
            "/v1/receipts/status",
            headers=headers,
            json={"run_id": "cancel-observation", "sequence": 0},
        )

    assert receipt.status_code == 200
    assert receipt.json()["state"] == "COMPLETED"



def test_step_preflights_the_whole_tree_before_mutation(test_client, app) -> None:
    headers = _acquire_lease(test_client)
    response = test_client.post(
        "/v1/steps",
        headers=headers,
        json={
            "actions": [
                {"type": "move", "x": 10, "y": 20},
                {"type": "drag"},
            ]
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "action_validation_failed"
    assert app.state.backend.cursor.x == 0
    assert app.state.backend.cursor.y == 0


def test_step_preflights_aggregate_inline_screenshot_pixels(test_client, app) -> None:
    app.state.settings = replace(app.state.settings, screenshot_max_pixels=1_048_576)
    headers = _acquire_lease(test_client, run_id="pixel-budget")
    response = test_client.post(
        "/v1/steps",
        headers=headers,
        json={
            "actions": [{"type": "screenshot", "options": {"format": "png"}}],
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "action_validation_failed"
    assert app.state.screenshot_count == 0


def test_step_rejects_artifact_backed_nested_screenshots_before_mutation(test_client, app) -> None:
    headers = _acquire_lease(test_client, run_id="artifact-step")
    response = test_client.post(
        "/v1/steps",
        headers=headers,
        json={
            "actions": [
                {
                    "type": "screenshot",
                    "options": {"format": "png", "storage": "artifact"},
                },
                {"type": "move", "x": 10, "y": 20},
            ]
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "action_validation_failed"
    assert app.state.backend.cursor.x == 0
    assert app.state.backend.cursor.y == 0


def test_step_capability_is_advertised_and_required(test_client) -> None:
    response = test_client.get("/v1/capabilities")

    assert response.status_code == 200
    assert "computer-step-envelope-v1" in response.json()["primitives"]


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_step_only_exposes_post(test_client, method: str) -> None:
    response = getattr(test_client, method)("/v1/steps")

    assert response.status_code in {404, 405}
