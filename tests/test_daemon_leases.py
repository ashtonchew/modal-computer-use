from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.leases import (
    LEASE_EPOCH_HEADER,
    LEASE_FENCE_HEADER,
    LEASE_ID_HEADER,
    LEASE_TOKEN_HEADER,
    LeaseCoordinator,
    LeaseCredentials,
    MutationLease,
)
from modal_computer_use.daemon.receipts import OPERATION_SEQUENCE_HEADER
from modal_computer_use.daemon.settings import DaemonSettings


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _credentials(grant: Any) -> LeaseCredentials:
    return LeaseCredentials(
        lease_id=grant.lease_id,
        epoch=grant.epoch,
        fence=grant.fence,
        token=grant.token,
    )


def _lease_headers(response: httpx.Response) -> dict[str, str]:
    body = response.json()
    return {
        LEASE_ID_HEADER: body["lease_id"],
        LEASE_EPOCH_HEADER: body["daemon_epoch"],
        LEASE_FENCE_HEADER: str(body["fence"]),
        LEASE_TOKEN_HEADER: response.headers[LEASE_TOKEN_HEADER],
    }


def _acquire(client: Any, run_id: str = "application-run") -> httpx.Response:
    return client.post("/v1/leases/acquire", json={"run_id": run_id})


def test_coordinator_acquire_busy_heartbeat_release_expiry_and_fence() -> None:
    clock = _Clock()
    coordinator = LeaseCoordinator(
        clock=clock,
        ttl_seconds=30,
        heartbeat_interval_seconds=10,
    )

    first = coordinator.acquire("application-run")
    assert first.run_id == "application-run"
    assert first.fence == 1
    assert coordinator.status()["state"] == "active"

    with pytest.raises(DaemonError) as busy:
        coordinator.acquire("other-run")
    assert busy.value.code == "session_busy"
    assert busy.value.details == {"retry_after_seconds": 30}

    clock.advance(25)
    heartbeat = coordinator.heartbeat(_credentials(first))
    assert heartbeat["expires_in_seconds"] == 30
    clock.advance(20)
    assert coordinator.status()["state"] == "active"

    released = coordinator.release(_credentials(first))
    assert released["state"] == "released"
    assert released["run_state"] == "released"
    with pytest.raises(DaemonError) as released_error:
        coordinator.heartbeat(_credentials(first))
    assert released_error.value.code == "lease_released"

    second = coordinator.acquire("second-run")
    assert second.fence == 2
    assert second.lease_id != first.lease_id
    assert second.run_id == "second-run"
    clock.advance(31)
    expired = coordinator.status()
    assert expired["state"] == "expired"
    assert expired["run_state"] == "interrupted"
    with pytest.raises(DaemonError) as expired_error:
        coordinator.validate_mutation(_credentials(second))
    assert expired_error.value.code == "lease_expired"

    third = coordinator.acquire("third-run")
    assert third.fence == 3
    assert third.run_id == "third-run"


def test_coordinator_rejects_blank_application_run_id() -> None:
    coordinator = LeaseCoordinator()

    with pytest.raises(DaemonError) as error:
        coordinator.acquire("  \t")

    assert error.value.code == "invalid_run_id"
    assert coordinator.status()["state"] == "free"
    assert coordinator.status()["run_id"] is None


def test_prepare_acquire_cannot_install_an_unreturned_lease_when_clock_crosses_ttl() -> None:
    moments = iter((0.0, 0.0, 0.5, 2.0))
    coordinator = LeaseCoordinator(clock=lambda: next(moments), ttl_seconds=1)
    first = coordinator.acquire("first-run")

    with pytest.raises(DaemonError) as busy:
        coordinator.prepare_acquire()

    assert busy.value.code == "session_busy"
    assert coordinator._fence == 1
    assert coordinator._lease is not None
    assert coordinator._lease.lease_id == first.lease_id
    assert coordinator._lease.run_id == "first-run"


def test_release_validated_ignores_ttl_only_for_exact_admitted_identity() -> None:
    clock = _Clock()
    coordinator = LeaseCoordinator(clock=clock, ttl_seconds=1)
    grant = coordinator.acquire("validated-run")
    admitted = coordinator.validate_mutation(_credentials(grant))
    assert admitted is not None

    for mismatched in (
        MutationLease(run_id="other-run", epoch=admitted.epoch, fence=admitted.fence),
        MutationLease(run_id=admitted.run_id, epoch="other-epoch", fence=admitted.fence),
        MutationLease(run_id=admitted.run_id, epoch=admitted.epoch, fence=admitted.fence + 1),
    ):
        with pytest.raises(DaemonError) as stale:
            coordinator.release_validated(mismatched)
        assert stale.value.code == "lease_stale"

    clock.advance(2)
    released = coordinator.release_validated(admitted)
    assert released["state"] == "released"
    with pytest.raises(DaemonError) as repeated:
        coordinator.release_validated(admitted)
    assert repeated.value.code == "lease_stale"


def test_coordinator_epoch_changes_on_daemon_restart_and_tokens_are_repr_safe() -> None:
    first = LeaseCoordinator()
    second = LeaseCoordinator()
    grant = first.acquire("same-run")
    credentials = _credentials(grant)

    assert first.epoch != second.epoch
    assert grant.token not in repr(grant)
    assert grant.token not in repr(credentials)
    assert grant.token not in repr(first)


def test_lease_protocol_returns_token_only_in_protected_header(test_client) -> None:
    response = _acquire(test_client)

    assert response.status_code == 200
    assert response.headers[LEASE_TOKEN_HEADER]
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["state"] == "active"
    assert "token" not in response.json()
    assert response.headers[LEASE_TOKEN_HEADER] not in response.text

    busy = _acquire(test_client, "other-run")
    assert busy.status_code == 409
    assert busy.json()["code"] == "session_busy"
    assert 1 <= busy.json()["details"]["retry_after_seconds"] <= 30
    assert response.headers[LEASE_TOKEN_HEADER] not in busy.text
    assert "application-run" not in busy.text
    assert "other-run" not in busy.text


@pytest.mark.parametrize("payload", [None, {}, {"run_id": ""}, {"run_id": "   "}])
def test_acquire_rejects_missing_or_blank_run_id(test_client, payload: Any) -> None:
    response = test_client.post("/v1/leases/acquire", json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert test_client.get("/v1/leases/status").json()["state"] == "free"


def test_lease_heartbeat_release_and_stale_delayed_fence(test_client) -> None:
    first = _acquire(test_client, "first-run")
    first_headers = _lease_headers(first)

    heartbeat = test_client.post("/v1/leases/heartbeat", headers=first_headers)
    assert heartbeat.status_code == 200
    assert heartbeat.json()["state"] == "active"

    released = test_client.post("/v1/leases/release", headers=first_headers)
    assert released.status_code == 200
    assert released.json()["state"] == "released"
    assert released.json()["run_id"] == "first-run"
    assert test_client.get("/v1/leases/status").json()["run_id"] == "first-run"

    second = _acquire(test_client, "second-run")
    second_headers = {
        **_lease_headers(second),
        OPERATION_SEQUENCE_HEADER: "0",
    }
    assert second.json()["fence"] == first.json()["fence"] + 1
    assert first.json()["run_id"] == "first-run"
    assert second.json()["run_id"] == "second-run"

    delayed = test_client.post(
        "/v1/mouse/move",
        json={"x": 4, "y": 5},
        headers=first_headers,
    )
    assert delayed.status_code == 409
    assert delayed.json()["code"] == "lease_stale"

    current = test_client.post(
        "/v1/mouse/move",
        json={"x": 4, "y": 5},
        headers=second_headers,
    )
    assert current.status_code == 200


def test_acquire_run_id_is_not_taken_from_daemon_settings(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            runtime_dir=tmp_path / "runtime",
            local_token="dev",
            run_id="computer-use-environment-run",
        )
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        acquired = _acquire(client, "borrowing-application-run")
        status = client.get("/v1/leases/status")

    assert acquired.json()["run_id"] == "borrowing-application-run"
    assert status.json()["run_id"] == "borrowing-application-run"


def test_cached_action_replay_cannot_bypass_active_lease(test_client) -> None:
    payload = {"actions": [{"type": "move", "x": 4, "y": 5}]}
    first = test_client.post(
        "/v1/actions/run",
        json=payload,
        headers={"Idempotency-Key": "lease-replay"},
    )
    _acquire(test_client)

    replay = test_client.post(
        "/v1/actions/run",
        json=payload,
        headers={"Idempotency-Key": "lease-replay"},
    )

    assert first.status_code == 200
    assert replay.status_code == 409
    assert replay.json()["code"] == "lease_required"


def test_missing_and_malformed_lease_headers_have_sanitized_codes(test_client) -> None:
    acquired = _acquire(test_client)
    token = acquired.headers[LEASE_TOKEN_HEADER]

    missing = test_client.post("/v1/input/release-all")
    malformed = test_client.post(
        "/v1/input/release-all",
        headers={LEASE_TOKEN_HEADER: token},
    )

    assert missing.status_code == 409
    assert missing.json() == {
        "code": "lease_required",
        "message": "an active trajectory lease is required",
        "details": {},
    }
    assert malformed.status_code == 409
    assert malformed.json()["code"] == "lease_stale"
    assert token not in malformed.text


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("post", "/v1/mouse/click", {"json": {"x": 1, "y": 2}}),
        ("post", "/v1/keyboard/press", {"json": {"key": "ENTER"}}),
        ("put", "/v1/clipboard/text", {"json": {"text": "blocked"}}),
        ("post", "/v1/commands/run", {"json": {"command": ["true"]}}),
        (
            "post",
            "/v1/browser/open-url",
            {"json": {"url": "https://example.com", "wait_for_window": False}},
        ),
        (
            "post",
            "/v1/browser/render-metrics",
            {"json": {"url": "https://example.com"}},
        ),
        ("post", "/v1/apps/launch", {"json": {"command": "xterm"}}),
        ("post", "/v1/windows/window-1/activate", {}),
        ("put", "/v1/artifacts/result.txt", {"content": b"blocked"}),
        ("post", "/v1/recordings", {"json": {}}),
        (
            "post",
            "/v1/actions/run",
            {"json": {"actions": [{"type": "move", "x": 1, "y": 2}]}},
        ),
        ("post", "/v1/computer/stop", {}),
    ],
)
def test_active_lease_blocks_representative_unfenced_http_mutations(
    test_client,
    method: str,
    path: str,
    kwargs: dict[str, Any],
) -> None:
    _acquire(test_client)

    response = getattr(test_client, method)(path, **kwargs)

    assert response.status_code == 409
    assert response.json()["code"] == "lease_required"


def test_unleased_compatibility_and_read_only_routes_remain_available(test_client) -> None:
    legacy = test_client.post("/v1/mouse/move", json={"x": 7, "y": 8})
    assert legacy.status_code == 200

    _acquire(test_client)
    for response in (
        test_client.get("/healthz"),
        test_client.get("/readyz"),
        test_client.get("/v1/version"),
        test_client.get("/v1/capabilities"),
        test_client.get("/v1/leases/status"),
        test_client.get("/v1/session/metadata"),
        test_client.post("/v1/screenshots/full", json={"format": "png"}),
        test_client.get("/v1/clipboard/text"),
    ):
        assert response.status_code == 200

    capabilities = test_client.get("/v1/capabilities")
    assert "trajectory-leases-v1" in capabilities.json()["primitives"]
    assert capabilities.headers["x-computer-use-lease-protocol"] == "1"


def test_hot_session_revalidates_each_mutating_message(test_client) -> None:
    _acquire(test_client)

    with test_client.websocket_connect("/v1/session/hot") as websocket:
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json({"id": "read", "op": "screenshot_raw", "payload": {}})
        assert websocket.receive_json()["type"] == "binary"
        assert websocket.receive_bytes()

        websocket.send_json(
            {
                "id": "mutate-1",
                "op": "run_actions",
                "payload": {"actions": [{"type": "move", "x": 1, "y": 2}]},
            }
        )
        first = websocket.receive_json()
        websocket.send_json(
            {
                "id": "mutate-2",
                "op": "run_actions",
                "payload": {"actions": [{"type": "click", "x": 1, "y": 2}]},
            }
        )
        second = websocket.receive_json()

    assert first["type"] == second["type"] == "error"
    assert first["error"]["code"] == second["error"]["code"] == "lease_required"


def test_observation_stream_blocks_action_capable_messages_but_allows_frames(test_client) -> None:
    _acquire(test_client)

    with test_client.websocket_connect("/v1/observations/stream") as websocket:
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json(
            {
                "id": "start",
                "op": "start",
                "payload": {"fps": 0.01, "format": "png", "show_cursor": False},
            }
        )
        assert websocket.receive_json()["type"] == "started"
        assert websocket.receive_json()["type"] == "frame"
        assert websocket.receive_bytes()
        websocket.send_json(
            {
                "id": "mutate",
                "op": "run_actions_capture",
                "payload": {"actions": [{"type": "move", "x": 1, "y": 2}]},
            }
        )
        blocked = websocket.receive_json()

    assert blocked["type"] == "error"
    assert blocked["error"]["code"] == "lease_required"


def test_acquire_waits_for_existing_input_lock_to_drain(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            runtime_dir=tmp_path / "runtime",
            local_token="dev",
        )
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer dev"},
        ) as client:
            await app.state.input_lock.acquire()
            acquire_task = asyncio.create_task(
                client.post("/v1/leases/acquire", json={"run_id": "application-run"})
            )
            await asyncio.sleep(0)
            assert not acquire_task.done()
            app.state.input_lock.release()
            response = await acquire_task
            assert response.status_code == 200

            await app.state.input_lock.acquire()
            release_task = asyncio.create_task(
                client.post("/v1/leases/release", headers=_lease_headers(response))
            )
            await asyncio.sleep(0)
            assert not release_task.done()
            app.state.input_lock.release()
            released = await release_task
            assert released.status_code == 200
            assert released.json()["state"] == "released"

    asyncio.run(exercise())


def test_idle_expiration_seals_run_interrupted_and_releases_ownership(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            runtime_dir=tmp_path / "runtime",
            local_token="dev",
        )
    )
    app.state.lease_coordinator = LeaseCoordinator(
        ttl_seconds=0.02,
        heartbeat_interval_seconds=0.01,
    )

    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        acquired = _acquire(client)
        time.sleep(0.05)
        status = client.get("/v1/leases/status")
        legacy = client.post("/v1/mouse/move", json={"x": 3, "y": 4})

    assert acquired.status_code == 200
    assert status.json()["state"] == "expired"
    assert status.json()["run_state"] == "interrupted"
    assert legacy.status_code == 200


def test_heartbeat_renews_while_admitted_mutation_exceeds_ttl(tmp_path) -> None:
    clock = _Clock()
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            runtime_dir=tmp_path / "runtime",
            local_token="dev",
        )
    )
    app.state.lease_coordinator = LeaseCoordinator(
        clock=clock,
        ttl_seconds=1,
        heartbeat_interval_seconds=0.25,
    )
    app.state.supervisor.running = True
    original_move = app.state.backend.mouse_move
    admitted = asyncio.Event()
    finish_mutation = asyncio.Event()

    async def slow_move(x: int, y: int):
        admitted.set()
        await finish_mutation.wait()
        return await original_move(x, y)

    app.state.backend.mouse_move = slow_move

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer dev"},
        ) as client:
            acquired = await client.post(
                "/v1/leases/acquire", json={"run_id": "long-operation"}
            )
            original_expiry = clock.now + app.state.lease_coordinator.ttl_seconds
            lease_headers = {
                **_lease_headers(acquired),
                OPERATION_SEQUENCE_HEADER: "0",
            }
            mutation = asyncio.create_task(
                client.post(
                    "/v1/mouse/move",
                    json={"x": 7, "y": 8},
                    headers=lease_headers,
                )
            )
            await asyncio.wait_for(
                admitted.wait(),
                timeout=1,
            )
            clock.advance(0.75)
            heartbeat = await asyncio.wait_for(
                client.post("/v1/leases/heartbeat", headers=lease_headers),
                timeout=1,
            )
            clock.advance(0.5)
            assert clock.now > original_expiry
            finish_mutation.set()
            result = await mutation
            status = await client.get("/v1/leases/status")
            receipt = await client.post(
                "/v1/receipts/status",
                json={"run_id": "long-operation", "sequence": 0},
                headers=lease_headers,
            )

        assert heartbeat.status_code == 200
        assert result.status_code == 200
        assert status.json()["state"] == "active"
        assert status.json()["run_state"] == "active"
        assert receipt.json()["state"] == "COMPLETED"

    asyncio.run(exercise())
