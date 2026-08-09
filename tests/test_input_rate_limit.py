from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from modal_computer_use.config import ActionConfig
from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.input_rate_limit import (
    InputTokenBucket,
    input_token_cost,
)
from modal_computer_use.daemon.leases import (
    LEASE_EPOCH_HEADER,
    LEASE_FENCE_HEADER,
    LEASE_ID_HEADER,
    LEASE_TOKEN_HEADER,
)
from modal_computer_use.daemon.receipts import OPERATION_SEQUENCE_HEADER
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.errors import DaemonHTTPError
from modal_computer_use.models import (
    ClickAction,
    DoubleClickAction,
    DragAction,
    HoldKeyAction,
    MoveAction,
    Point,
    ScrollAction,
    TripleClickAction,
    TypeAction,
)
from modal_computer_use.transports.http import HTTPTransport


def _settings(tmp_path, **overrides: object) -> DaemonSettings:
    return DaemonSettings(
        backend="mock",
        artifacts_dir=tmp_path / "artifacts",
        recordings_dir=tmp_path / "recordings",
        runtime_dir=tmp_path / "runtime",
        local_token="dev",
        **overrides,
    )


def test_public_defaults_leave_headroom_for_the_optimized_step_path() -> None:
    actions = ActionConfig()

    assert actions.input_rate_limit_per_sec == 100
    assert actions.input_rate_limit_burst == 400


def test_normalized_input_cost_accounts_for_repeated_and_compound_work() -> None:
    assert input_token_cost(MoveAction(x=1, y=2)) == 1
    assert input_token_cost(ClickAction()) == 1
    assert input_token_cost(DoubleClickAction()) == 2
    assert input_token_cost(TripleClickAction()) == 3
    assert input_token_cost(TypeAction(text="x" * 64)) == 3
    assert input_token_cost(ScrollAction(amount=64)) == 3
    assert input_token_cost(
        DragAction(path=[{"x": index, "y": index} for index in range(64)])
    ) == 3
    assert input_token_cost(
        HoldKeyAction(key="SHIFT", actions=[{"type": "move", "x": 1, "y": 2}])
    ) == 2


def test_token_bucket_supports_burst_and_continuous_refill() -> None:
    now = 100.0
    bucket = InputTokenBucket(refill_rate=500, capacity=1_000, clock=lambda: now)

    assert bucket.reserve(1_000) is None
    limited = bucket.reservation_error(1)
    assert limited is not None
    assert limited.retry_after_ms == 2

    now += 0.5
    assert bucket.reserve(250) is None
    assert bucket.available == 0


def test_batch_reserves_full_weight_before_any_mutation(tmp_path) -> None:
    app = create_app(
        _settings(
            tmp_path,
            input_rate_limit_per_sec=1,
            input_rate_limit_burst=1,
        )
    )
    calls = 0
    original = app.state.backend.mouse_move

    async def move(x: int, y: int):
        nonlocal calls
        calls += 1
        return await original(x, y)

    app.state.backend.mouse_move = move
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [
                    {"type": "move", "x": 10, "y": 20},
                    {"type": "move", "x": 30, "y": 40},
                ]
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "input_cost_exceeds_burst"
    assert calls == 0
    assert app.state.action_count == 0


def test_transient_batch_limit_returns_retry_after_before_mutation(tmp_path) -> None:
    app = create_app(
        _settings(
            tmp_path,
            input_rate_limit_per_sec=10,
            input_rate_limit_burst=2,
        )
    )
    calls = 0
    original = app.state.backend.mouse_move

    async def move(x: int, y: int):
        nonlocal calls
        calls += 1
        return await original(x, y)

    app.state.backend.mouse_move = move
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        accepted = client.post(
            "/v1/actions/run",
            json={"actions": [{"type": "move", "x": 1, "y": 1}]},
        )
        limited = client.post(
            "/v1/actions/run",
            json={
                "actions": [
                    {"type": "move", "x": 2, "y": 2},
                    {"type": "move", "x": 3, "y": 3},
                ]
            },
        )

    assert accepted.status_code == 200
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "1"
    assert limited.json()["code"] == "rate_limited"
    assert 1 <= limited.json()["details"]["retry_after_ms"] <= 100
    assert calls == 1
    assert app.state.action_count == 1


def test_direct_route_uses_weighted_cost_before_backend_dispatch(tmp_path) -> None:
    app = create_app(
        _settings(
            tmp_path,
            input_rate_limit_per_sec=1,
            input_rate_limit_burst=1,
        )
    )
    calls = 0
    original = app.state.backend.mouse_click

    async def click(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await original(*args, **kwargs)

    app.state.backend.mouse_click = click
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/mouse/click",
            json={"x": 1, "y": 1, "double": True},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "input_cost_exceeds_burst"
    assert calls == 0
    assert app.state.action_count == 0


def test_explicit_zero_refill_disables_weighted_admission(tmp_path) -> None:
    app = create_app(
        _settings(
            tmp_path,
            input_rate_limit_per_sec=0,
            input_rate_limit_burst=1,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [
                    {"type": "type", "text": "x" * 64, "delay_ms": 0, "method": "clipboard"}
                ]
            },
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert app.state.action_count == 1


def test_sync_sdk_exposes_retry_after_without_exposing_response_headers(tmp_path) -> None:
    app = create_app(
        _settings(
            tmp_path,
            input_rate_limit_per_sec=10,
            input_rate_limit_burst=1,
        )
    )
    with TestClient(app) as client:
        transport = HTTPTransport("http://testserver", token="dev", client=client)
        transport.request("POST", "/v1/mouse/move", json={"x": 1, "y": 1})
        with pytest.raises(DaemonHTTPError) as exc_info:
            transport.request("POST", "/v1/mouse/move", json={"x": 2, "y": 2})

    assert exc_info.value.code == "rate_limited"
    assert exc_info.value.retry_after_ms is not None
    assert exc_info.value.retry_after_seconds == 1


def test_leased_direct_rate_rejection_abandons_receipt_without_recovery(tmp_path) -> None:
    app = create_app(
        _settings(
            tmp_path,
            input_rate_limit_per_sec=1,
            input_rate_limit_burst=1,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        acquired = client.post("/v1/leases/acquire", json={"run_id": "rate-receipt"})
        lease = acquired.json()
        headers = {
            LEASE_ID_HEADER: lease["lease_id"],
            LEASE_EPOCH_HEADER: lease["daemon_epoch"],
            LEASE_FENCE_HEADER: str(lease["fence"]),
            LEASE_TOKEN_HEADER: acquired.headers["x-computer-use-lease-token"],
            OPERATION_SEQUENCE_HEADER: "0",
        }
        first = client.post("/v1/mouse/move", headers=headers, json={"x": 1, "y": 1})
        headers[OPERATION_SEQUENCE_HEADER] = "1"
        limited = client.post("/v1/mouse/move", headers=headers, json={"x": 2, "y": 2})
        receipt = client.post(
            "/v1/receipts/status",
            headers=headers,
            json={"run_id": "rate-receipt", "sequence": 1},
        )
        recovery = client.get("/v1/recovery/status", headers=headers)

    assert first.status_code == 200
    assert limited.status_code == 429
    assert limited.json()["details"]["retry_safe"] is True
    assert limited.json()["details"]["emission_state"] == "not_started"
    assert receipt.status_code == 200
    assert receipt.json()["state"] == "MISSING"
    assert recovery.status_code == 200
    assert recovery.json()["recovery_required"] is False


def test_receipt_conflict_refunds_batch_tokens_before_next_valid_sequence(tmp_path) -> None:
    app = create_app(
        _settings(
            tmp_path,
            input_rate_limit_per_sec=1,
            input_rate_limit_burst=3,
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        acquired = client.post("/v1/leases/acquire", json={"run_id": "refund-sequence"})
        lease = acquired.json()
        headers = {
            LEASE_ID_HEADER: lease["lease_id"],
            LEASE_EPOCH_HEADER: lease["daemon_epoch"],
            LEASE_FENCE_HEADER: str(lease["fence"]),
            LEASE_TOKEN_HEADER: acquired.headers["x-computer-use-lease-token"],
            OPERATION_SEQUENCE_HEADER: "0",
        }
        first = client.post(
            "/v1/actions/run",
            headers=headers,
            json={"actions": [{"type": "move", "x": 1, "y": 1}]},
        )
        conflict = client.post(
            "/v1/actions/run",
            headers=headers,
            json={"actions": [{"type": "move", "x": 2, "y": 2}]},
        )
        headers[OPERATION_SEQUENCE_HEADER] = "1"
        valid = client.post(
            "/v1/actions/run",
            headers=headers,
            json={
                "actions": [
                    {"type": "move", "x": 3, "y": 3},
                    {"type": "move", "x": 4, "y": 4},
                ]
            },
        )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "run_sequence_conflict"
    assert valid.status_code == 200
    assert valid.json()["ok"] is True
    assert app.state.backend.cursor == Point(x=4, y=4)
