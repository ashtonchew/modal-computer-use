from __future__ import annotations

import pytest

from modal_computer_use.client import DaemonClient
from modal_computer_use.daemon.leases import (
    LEASE_EPOCH_HEADER,
    LEASE_FENCE_HEADER,
    LEASE_ID_HEADER,
    LEASE_TOKEN_HEADER,
)
from modal_computer_use.daemon.receipts import OPERATION_SEQUENCE_HEADER
from modal_computer_use.errors import SessionDaemonProtocolError
from modal_computer_use.models import Screenshot
from modal_computer_use.protocol_compatibility import validate_default_trajectory_protocol
from modal_computer_use.transports import HTTPTransport


@pytest.mark.parametrize(
    ("daemon_version", "sdk_min_version", "sdk_max_version", "additional_primitive"),
    [
        ("1.1.0", "1.1.0", "1.x", None),
        ("0.9.0", "9.0.0", "9.x", "older-daemon-extension"),
        ("99.0.0", "0.1.0", "999.x", "future-daemon-extension"),
    ],
)
def test_default_trajectory_accepts_protocol_behavior_independent_of_package_versions(
    daemon_version: str,
    sdk_min_version: str,
    sdk_max_version: str,
    additional_primitive: str | None,
) -> None:
    primitives = [
        "screenshot-binary-metadata-v1",
        "trajectory-leases-v1",
        "trajectory-operation-receipts-v1",
        "computer-step-envelope-v1",
    ]
    if additional_primitive is not None:
        primitives.append(additional_primitive)

    validate_default_trajectory_protocol(
        version_payload={
            "api_version": "v1",
            "daemon_version": daemon_version,
            "sdk_min_version": sdk_min_version,
            "sdk_max_version": sdk_max_version,
            "future_version_field": {"accepted": True},
        },
        capabilities_payload={
            "primitives": primitives,
            "future_capability_field": {"accepted": True},
        },
    )


@pytest.mark.parametrize(
    ("version_payload", "capabilities_payload"),
    [
        (None, {"primitives": []}),
        ({}, {"primitives": []}),
        ({"api_version": "v2"}, {"primitives": []}),
        ({"api_version": 1}, {"primitives": []}),
        ({"api_version": "v1"}, None),
        ({"api_version": "v1"}, {}),
        ({"api_version": "v1"}, {"primitives": "all"}),
        ({"api_version": "v1"}, {"primitives": ["trajectory-leases-v1", 1]}),
        (
            {"api_version": "v1"},
            {
                "primitives": [
                    "screenshot-binary-metadata-v1",
                    "trajectory-leases-v1",
                ]
            },
        ),
    ],
)
def test_default_trajectory_rejects_unsupported_or_malformed_protocol_documents(
    version_payload: object,
    capabilities_payload: object,
) -> None:
    with pytest.raises(SessionDaemonProtocolError):
        validate_default_trajectory_protocol(
            version_payload=version_payload,
            capabilities_payload=capabilities_payload,
        )


def test_direct_daemon_json_screenshot_and_idempotent_action_routes_remain_available(
    test_client,
) -> None:
    transport = HTTPTransport("http://testserver", token="dev", client=test_client)
    client = DaemonClient("http://testserver", transport=transport)

    assert client.get_json("/healthz") == {"ok": True}
    assert client.get_json("/readyz")["ready"] is True
    assert client.get_json("/v1/version")["api_version"] == "v1"
    assert "screenshot-binary-metadata-v1" in client.get_json("/v1/capabilities")[
        "primitives"
    ]

    screenshot = Screenshot.model_validate(
        client.post_json(
            "/v1/screenshots/full",
            json={"format": "png", "storage": "inline"},
        )
    )
    assert screenshot.data_base64 is not None
    assert screenshot.as_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    action_payload = {"actions": [{"type": "move", "x": 10, "y": 20}]}
    first = client.post_json(
        "/v1/actions/run",
        json=action_payload,
        headers={"Idempotency-Key": "direct-compatibility-check"},
        _mutation=True,
    )
    second = client.post_json(
        "/v1/actions/run",
        json=action_payload,
        headers={"Idempotency-Key": "direct-compatibility-check"},
        _mutation=True,
    )
    assert first["call_id"] == second["call_id"]


def test_default_trajectory_protocol_behavior_matrix(test_client) -> None:
    assert test_client.get("/healthz").json() == {"ok": True}
    assert test_client.get("/readyz").json()["ready"] is True

    version_response = test_client.get("/v1/version")
    capabilities_response = test_client.get("/v1/capabilities")
    validate_default_trajectory_protocol(
        version_payload=version_response.json(),
        capabilities_payload=capabilities_response.json(),
    )
    assert version_response.headers["x-computer-use-lease-protocol"] == "1"
    assert version_response.headers["x-computer-use-receipt-protocol"] == "1"

    lease_response = test_client.post(
        "/v1/leases/acquire",
        json={"run_id": "protocol-matrix-run"},
    )
    assert lease_response.status_code == 200
    lease = lease_response.json()
    lease_headers = {
        LEASE_ID_HEADER: lease["lease_id"],
        LEASE_EPOCH_HEADER: lease["daemon_epoch"],
        LEASE_FENCE_HEADER: str(lease["fence"]),
        LEASE_TOKEN_HEADER: lease_response.headers[LEASE_TOKEN_HEADER],
    }

    screenshot_response = test_client.post(
        "/v1/screenshots/full/raw",
        json={"format": "png", "show_cursor": False, "storage": "inline"},
        headers=lease_headers,
    )
    assert screenshot_response.status_code == 200
    assert screenshot_response.headers["content-type"] == "image/png"
    assert screenshot_response.headers["x-computer-use-size-bytes"] == str(
        len(screenshot_response.content)
    )
    assert screenshot_response.headers["x-computer-use-sha256"]
    assert screenshot_response.headers["x-computer-use-capture-backend"]

    action_response = test_client.post(
        "/v1/actions/run",
        json={"actions": [{"type": "move", "x": 10, "y": 20}]},
        headers={**lease_headers, OPERATION_SEQUENCE_HEADER: "0"},
    )
    assert action_response.status_code == 200
    assert action_response.json()["ok"] is True

    receipt_response = test_client.post(
        "/v1/receipts/status",
        json={"run_id": "protocol-matrix-run", "sequence": 0},
        headers=lease_headers,
    )
    assert receipt_response.status_code == 200
    assert receipt_response.json()["state"] == "COMPLETED"

    release_response = test_client.post("/v1/leases/release", headers=lease_headers)
    assert release_response.status_code == 200
    assert release_response.json()["state"] == "released"
