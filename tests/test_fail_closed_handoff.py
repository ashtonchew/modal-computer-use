from __future__ import annotations

import pytest

from modal_computer_use import ComputerSessionHandle, SessionCompatibilityError
from modal_computer_use.sandbox import _session_policy_id_prefix


def _handle(*, requested_region: str = "us-west") -> ComputerSessionHandle:
    policy_prefix = _session_policy_id_prefix(
        app_name="desktop-app",
        modal_environment="prod",
        requested_modal_region=requested_region,
        ingress="connect",
        daemon_http_version="1.1",
        vnc_mode="off",
        config_hash="a" * 16,
    )
    return ComputerSessionHandle(
        sandbox_id="sb-owned",
        session_id=policy_prefix + "b" * 16,
        app_name="desktop-app",
        modal_environment="prod",
        requested_modal_region=requested_region,
        ingress="connect",
        daemon_http_version="1.1",
        vnc_mode="off",
        config_hash="a" * 16,
    )


@pytest.fixture(autouse=True)
def _placed_modal_function(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_IS_REMOTE", "1")
    monkeypatch.setenv("MODAL_ENVIRONMENT", "prod")
    monkeypatch.setenv("MODAL_REGION", "us-west-2")


def test_missing_declared_function_placement_has_a_typed_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attached = False

    def attach(*_args: object, **_kwargs: object) -> object:
        nonlocal attached
        attached = True
        raise AssertionError("missing placement must fail before target attachment")

    monkeypatch.setattr("modal_computer_use.sandbox._borrow_modal_function_session", attach)

    with (
        pytest.raises(SessionCompatibilityError) as raised,
        _handle().borrow(run_id="run-123", function_region=" "),
    ):
        raise AssertionError("unreachable")

    assert type(raised.value).__name__ == "SessionPlacementMissingError"
    assert attached is False
    assert "us-west" not in str(raised.value)
    assert "MODAL_REGION" not in str(raised.value)


def test_malformed_declared_function_placement_has_a_typed_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "modal_computer_use.sandbox._borrow_modal_function_session",
        lambda *_args, **_kwargs: pytest.fail(
            "malformed placement must fail before target attachment"
        ),
    )

    with (
        pytest.raises(SessionCompatibilityError) as raised,
        _handle(requested_region="us-west-2").borrow(
            run_id="run-123",
            function_region="US WEST 2",
        ),
    ):
        raise AssertionError("unreachable")

    assert type(raised.value).__name__ == "SessionPlacementMalformedError"


def test_broad_function_selector_is_unverifiable_before_target_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "modal_computer_use.sandbox._borrow_modal_function_session",
        lambda *_args, **_kwargs: pytest.fail(
            "a broad selector must fail before target attachment"
        ),
    )

    with (
        pytest.raises(SessionCompatibilityError) as raised,
        _handle().borrow(run_id="run-123", function_region="us-west"),
    ):
        raise AssertionError("unreachable")

    assert type(raised.value).__name__ == "SessionPlacementUnverifiableError"
    assert str(raised.value) == "Function or target placement could not be verified"


def test_missing_observed_function_region_fails_before_target_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODAL_REGION")
    monkeypatch.setattr(
        "modal_computer_use.sandbox._borrow_modal_function_session",
        lambda *_args, **_kwargs: pytest.fail(
            "missing runtime placement must fail before target attachment"
        ),
    )

    with (
        pytest.raises(SessionCompatibilityError) as raised,
        _handle(requested_region="us-west-2").borrow(
            run_id="run-123",
            function_region="us-west-2",
        ),
    ):
        raise AssertionError("unreachable")

    assert type(raised.value).__name__ == "SessionPlacementMissingError"


def test_malformed_observed_function_region_fails_before_target_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODAL_REGION", "https://credential.invalid/private")
    monkeypatch.setattr(
        "modal_computer_use.sandbox._borrow_modal_function_session",
        lambda *_args, **_kwargs: pytest.fail(
            "malformed runtime placement must fail before target attachment"
        ),
    )

    with (
        pytest.raises(SessionCompatibilityError) as raised,
        _handle(requested_region="us-west-2").borrow(
            run_id="run-123",
            function_region="us-west-2",
        ),
    ):
        raise AssertionError("unreachable")

    assert type(raised.value).__name__ == "SessionPlacementMalformedError"
    assert "credential" not in str(raised.value)


def test_broad_observed_function_region_is_unverifiable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODAL_REGION", "us-west")
    monkeypatch.setattr(
        "modal_computer_use.sandbox._borrow_modal_function_session",
        lambda *_args, **_kwargs: pytest.fail(
            "broad observed placement must fail before target attachment"
        ),
    )

    with (
        pytest.raises(SessionCompatibilityError) as raised,
        _handle(requested_region="us-west-2").borrow(
            run_id="run-123",
            function_region="us-west-2",
        ),
    ):
        raise AssertionError("unreachable")

    assert type(raised.value).__name__ == "SessionPlacementUnverifiableError"


def test_observed_function_region_mismatch_is_distinct_from_unverifiable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODAL_REGION", "us-east-1")
    monkeypatch.setattr(
        "modal_computer_use.sandbox._borrow_modal_function_session",
        lambda *_args, **_kwargs: pytest.fail(
            "mismatched observed placement must fail before target attachment"
        ),
    )

    with (
        pytest.raises(SessionCompatibilityError) as raised,
        _handle(requested_region="us-west-2").borrow(
            run_id="run-123",
            function_region="us-west-2",
        ),
    ):
        raise AssertionError("unreachable")

    assert type(raised.value).__name__ == "SessionPlacementMismatchError"
