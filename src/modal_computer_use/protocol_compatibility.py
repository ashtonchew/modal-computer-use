from __future__ import annotations

from collections.abc import Mapping

from .errors import SessionDaemonProtocolError

DEFAULT_TRAJECTORY_PRIMITIVES = frozenset(
    {
        "screenshot-binary-metadata-v1",
        "trajectory-leases-v1",
        "trajectory-operation-receipts-v1",
        "computer-step-envelope-v1",
    }
)


def validate_default_trajectory_protocol(
    *,
    version_payload: object,
    capabilities_payload: object,
) -> None:
    """Reject a daemon that cannot safely run the default trajectory protocol."""
    if not isinstance(version_payload, Mapping):
        raise SessionDaemonProtocolError
    if version_payload.get("api_version") != "v1":
        raise SessionDaemonProtocolError

    if not isinstance(capabilities_payload, Mapping):
        raise SessionDaemonProtocolError
    primitives = capabilities_payload.get("primitives")
    if not isinstance(primitives, list) or any(
        not isinstance(primitive, str) for primitive in primitives
    ):
        raise SessionDaemonProtocolError
    if not DEFAULT_TRAJECTORY_PRIMITIVES.issubset(primitives):
        raise SessionDaemonProtocolError
