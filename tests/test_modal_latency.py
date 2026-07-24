from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from modal_computer_use import ComputerConfig, ComputerSandbox, SandboxRef
from modal_computer_use.errors import (
    BrowserReadinessError,
    ConfigConflictError,
    DaemonHTTPError,
    FrameValidationError,
    SandboxUnavailableError,
)
from modal_computer_use.latency import (
    SessionStartupTiming,
    WarmPoolClaim,
    WarmPoolEntry,
    WarmPoolPolicy,
    estimate_pool_idle_cost,
    estimate_warm_idle_cost,
    pool_config_identity,
    validate_first_frame,
)
from modal_computer_use.manager import ComputerSandboxManager
from modal_computer_use.observations import ActionObservationResult
from modal_computer_use.sandbox import (
    ModalSandboxExecResult,
    modal_daemon_endpoint,
    run_modal_daemon_command,
    run_modal_daemon_command_with_fallback,
)
from modal_computer_use.transports import ObservationFrame


def test_startup_timing_records_monotonic_supported_and_unsupported_stages() -> None:
    ticks = iter((10.0, 10.1, 10.4))
    timing = SessionStartupTiming(clock=lambda: next(ticks))

    timing.mark("sandbox_create_started")
    timing.unsupported("scheduled", "Modal V1 does not expose this timestamp")
    timing.mark("sandbox_registered")

    payload = timing.as_dict()
    assert payload["stages"]["sandbox_create_started"]["elapsed_ms"] == pytest.approx(100.0)
    assert payload["stages"]["scheduled"] == {
        "status": "unsupported",
        "elapsed_ms": None,
        "reason": "Modal V1 does not expose this timestamp",
    }
    assert payload["stages"]["sandbox_registered"]["elapsed_ms"] == pytest.approx(400.0)


def test_validate_first_frame_rejects_empty_invalid_and_wrong_geometry() -> None:
    with pytest.raises(ValueError, match="empty"):
        validate_first_frame(b"", expected_width=1, expected_height=1, image_format="png")
    with pytest.raises(ValueError, match="decode"):
        validate_first_frame(
            b"not-an-image", expected_width=1, expected_height=1, image_format="png"
        )

    pillow = pytest.importorskip("PIL.Image")
    from io import BytesIO

    buffer = BytesIO()
    pillow.new("RGB", (2, 1)).save(buffer, format="PNG")
    with pytest.raises(ValueError, match="geometry"):
        validate_first_frame(
            buffer.getvalue(), expected_width=1, expected_height=1, image_format="png"
        )


def test_pool_identity_ignores_request_identity_but_tracks_runtime_compatibility() -> None:
    first = ComputerConfig(run_id="run-a", request_id=None)
    second = ComputerConfig(run_id="run-b", request_id=None)
    assert pool_config_identity(first) == pool_config_identity(second)

    changed = second.model_copy(deep=True)
    changed.runtime.modal_region = "us-east"
    assert pool_config_identity(first) != pool_config_identity(changed)


def test_warm_pool_policy_rejects_idle_timeout_and_near_expiry() -> None:
    WarmPoolPolicy(pool_name="a" * 59, capacity=100)
    with pytest.raises(ValueError, match="64 bytes or longer"):
        WarmPoolPolicy(pool_name="a" * 60, capacity=1)
    with pytest.raises(ValueError, match="64 bytes or longer"):
        WarmPoolPolicy(pool_name="é" * 31, capacity=1)
    with pytest.raises(ValueError, match="invalid Modal Sandbox name"):
        WarmPoolPolicy(pool_name="prod/pool", capacity=1)

    with pytest.raises(ValueError, match="idle_timeout"):
        WarmPoolPolicy(pool_name="prod", capacity=2).validate_config(
            ComputerConfig(runtime={"idle_timeout_seconds": 60})
        )
    with pytest.raises(ValueError, match="vnc_password"):
        WarmPoolPolicy(pool_name="prod", capacity=2).validate_config(
            ComputerConfig(expose_vnc="control", vnc_password="test-password")
        )

    now = datetime(2026, 7, 18, tzinfo=UTC)
    entry = WarmPoolEntry(
        sandbox_id="sb-1",
        slot_name="prod-000",
        pool_name="prod",
        app_name="computer-app",
        config_identity="abc",
        queue_identity="queue-1",
        created_at=now - timedelta(seconds=30),
        ready_at=now - timedelta(seconds=20),
        expires_at=now + timedelta(seconds=29),
        requested_region="us-east",
        actual_region="us-east-1",
        cpu=4,
        memory_mib=8192,
    )
    policy = WarmPoolPolicy(pool_name="prod", capacity=2, min_remaining_seconds=30)
    assert policy.rejection_reason(entry, expected_identity="abc", now=now) == "near_expiry"


def test_estimate_warm_idle_cost_records_pool_and_resource_seconds() -> None:
    now = datetime(2026, 7, 18, tzinfo=UTC)
    entry = WarmPoolEntry(
        sandbox_id="sb-1",
        slot_name="prod-000",
        pool_name="prod",
        app_name="computer-app",
        config_identity="abc",
        queue_identity="queue-1",
        created_at=now - timedelta(seconds=15),
        ready_at=now - timedelta(seconds=10),
        expires_at=now + timedelta(seconds=100),
        requested_region="us-east",
        actual_region="us-east-1",
        cpu=4,
        memory_mib=8192,
    )
    cost = estimate_warm_idle_cost(entry, claimed_at=now, configured_pool_size=3)
    assert cost["configured_pool_size"] == 3
    assert cost["idle_resource_seconds"] == pytest.approx(10.0)
    assert cost["cpu_core_seconds"] == pytest.approx(40.0)
    assert cost["memory_gib_seconds"] == pytest.approx(80.0)
    assert cost["estimated_cost"]["status"] == "estimated"
    assert cost["estimated_cost"]["region_multiplier"] == 1.75
    assert cost["estimated_cost"]["total"] == pytest.approx(
        (40.0 * 0.00003942 + 80.0 * 0.00000667) * 1.75
    )

    aggregate = estimate_pool_idle_cost([entry, entry], observed_at=now, configured_pool_size=2)
    assert aggregate["observed_ready_slots"] == 2
    assert aggregate["idle_resource_seconds"] == pytest.approx(20.0)
    assert aggregate["cpu_core_seconds"] == pytest.approx(80.0)
    assert aggregate["estimated_cost"]["status"] == "estimated"

    partial_entry = WarmPoolEntry.from_dict({**entry.as_dict(), "cpu": None})
    partial = estimate_pool_idle_cost(
        [partial_entry, partial_entry],
        observed_at=now,
        configured_pool_size=2,
    )
    assert partial["estimated_cost"]["status"] == "partial"


def test_causal_frame_validation_requires_mutation_and_reconstructable_image() -> None:
    pillow = pytest.importorskip("PIL.Image")
    from io import BytesIO

    buffer = BytesIO()
    pillow.new("RGB", (1, 1)).save(buffer, format="PNG")
    result = ActionObservationResult(
        frame=ObservationFrame(
            payload=buffer.getvalue(),
            metadata={
                "id": 1,
                "action_id": 1,
                "causal_frame": True,
                "change_detected": True,
                "change_timeout_reached": False,
                "action_result": {"ok": True},
                "kind": "full",
                "format": "png",
                "width": 1,
                "height": 1,
            },
        ),
        elapsed_ms=12.5,
    )
    assert result.require_valid_frame(require_change=True) == buffer.getvalue()
    assert result.elapsed_ms == 12.5

    invalid = ActionObservationResult(
        frame=ObservationFrame(
            payload=buffer.getvalue(),
            metadata={
                **result.frame.metadata,
                "change_timeout_reached": True,
            },
        )
    )
    with pytest.raises(ValueError, match="timeout"):
        invalid.require_valid_frame(require_change=True)

    for field in ("action_id", "width", "height"):
        malformed = ActionObservationResult(
            frame=ObservationFrame(
                payload=buffer.getvalue(),
                metadata={**result.frame.metadata, field: True},
            )
        )
        with pytest.raises(ValueError):
            malformed.require_valid_frame(require_change=True)


def test_same_region_runner_falls_back_to_external_caller(monkeypatch) -> None:
    attempts: list[str] = []
    computer = SimpleNamespace()

    def external_runner(
        command: tuple[str, ...], *, env: dict[str, str], timeout: int
    ) -> ModalSandboxExecResult:
        attempts.append("external")
        assert command == ("python", "worker.py")
        assert timeout == 30
        assert env["COMPUTER_USE_DAEMON_RUNNER_PATH"] == "inherited"
        return ModalSandboxExecResult(sandbox_id="sb-target", returncode=0, stdout="ok", stderr="")

    def endpoint(_computer: object, path: str) -> SimpleNamespace:
        attempts.append(path)
        if path == "connect":
            raise SandboxUnavailableError("connect failed with diagnostic payload")
        return SimpleNamespace(
            path=path,
            base_url="https://external.example",
            token="test-token",
            target_sandbox_id="sb-target",
        )

    monkeypatch.setattr("modal_computer_use.sandbox.modal_daemon_endpoint", endpoint)

    result = run_modal_daemon_command_with_fallback(
        computer,
        ("python", "worker.py"),
        modal_region="us-east",
        exec_timeout_seconds=30,
        external_runner=external_runner,
    )

    assert attempts == ["connect", "inherited", "external"]
    assert result.selected_path == "external"
    assert result.fallback_used is True
    assert result.fallback_reason == "connect_endpoint_unavailable"
    assert result.fallback_error_type == "SandboxUnavailableError"
    assert "diagnostic payload" not in repr(result)
    assert result.result.stdout == "ok"


def test_same_region_runner_inherits_target_requested_region(monkeypatch) -> None:
    dispatched: list[dict[str, object]] = []
    computer = SimpleNamespace(_requested_modal_region="us-west")
    monkeypatch.setattr(
        "modal_computer_use.sandbox.modal_daemon_endpoint",
        lambda _computer, path: SimpleNamespace(
            path=path,
            base_url="https://connect.example",
            token="test-token",
            target_sandbox_id="sb-target",
        ),
    )

    def exec_once(command: tuple[str, ...], **kwargs: object) -> ModalSandboxExecResult:
        dispatched.append({"command": command, **kwargs})
        return ModalSandboxExecResult(
            sandbox_id="sb-runner",
            returncode=0,
            stdout="ok",
            stderr="",
        )

    result = run_modal_daemon_command_with_fallback(
        computer,
        ("python", "worker.py"),
        exec_once=exec_once,
    )

    assert result.requested_region == "us-west"
    assert result.selected_path == "same-region-connect"
    assert dispatched[0]["region"] == "us-west"


def test_same_region_runner_rejects_region_conflict_before_connect(monkeypatch) -> None:
    endpoint_calls = 0

    def endpoint(*args: object, **kwargs: object) -> SimpleNamespace:
        nonlocal endpoint_calls
        endpoint_calls += 1
        raise AssertionError("conflicting placement must fail before Connect preparation")

    monkeypatch.setattr("modal_computer_use.sandbox.modal_daemon_endpoint", endpoint)
    with pytest.raises(ConfigConflictError, match="does not match"):
        run_modal_daemon_command_with_fallback(
            SimpleNamespace(_requested_modal_region="us-west"),
            ("python", "worker.py"),
            modal_region="us-east",
        )

    assert endpoint_calls == 0


def test_colocated_runner_requires_region_when_target_placement_is_unknown() -> None:
    with pytest.raises(ValueError, match="requested region is unknown"):
        run_modal_daemon_command_with_fallback(
            SimpleNamespace(),
            ("python", "worker.py"),
        )


def test_diagnostic_runner_inherits_target_requested_region(monkeypatch) -> None:
    dispatched: list[dict[str, object]] = []
    computer = SimpleNamespace(_requested_modal_region="us-west")
    monkeypatch.setattr(
        "modal_computer_use.sandbox.modal_daemon_endpoint",
        lambda _computer, path: SimpleNamespace(
            path=path,
            base_url="https://daemon.example",
            token="test-token",
            target_sandbox_id="sb-target",
            execute_in_target=False,
        ),
    )

    def exec_once(command: tuple[str, ...], **kwargs: object) -> ModalSandboxExecResult:
        dispatched.append({"command": command, **kwargs})
        return ModalSandboxExecResult(
            sandbox_id="sb-runner",
            returncode=0,
            stdout="ok",
            stderr="",
        )

    run_modal_daemon_command(
        computer,
        ("python", "worker.py"),
        path="connect",
        exec_once=exec_once,
    )

    assert dispatched[0]["region"] == "us-west"


def test_same_region_runner_never_falls_back_after_dispatch(monkeypatch) -> None:
    external_calls = 0

    monkeypatch.setattr(
        "modal_computer_use.sandbox.modal_daemon_endpoint",
        lambda _computer, path: SimpleNamespace(
            path=path,
            base_url="https://connect.example",
            token="test-token",
            target_sandbox_id="sb-target",
        ),
    )

    def fail_after_dispatch(*args: object, **kwargs: object) -> ModalSandboxExecResult:
        raise TimeoutError("runner result timed out after dispatch")

    def external_runner(*args: object, **kwargs: object) -> ModalSandboxExecResult:
        nonlocal external_calls
        external_calls += 1
        raise AssertionError("external fallback must not run after dispatch")

    with pytest.raises(TimeoutError, match="after dispatch"):
        run_modal_daemon_command_with_fallback(
            SimpleNamespace(),
            ("python", "worker.py"),
            modal_region="us-east",
            external_runner=external_runner,
            exec_once=fail_after_dispatch,
        )
    assert external_calls == 0


def test_same_region_runner_does_not_fallback_for_unexpected_preparation_error(
    monkeypatch,
) -> None:
    external_calls = 0

    def endpoint(_computer: object, path: str) -> SimpleNamespace:
        if path == "connect":
            raise RuntimeError("programming failure")
        return SimpleNamespace(
            path=path,
            base_url="https://external.example",
            token="test-token",
            target_sandbox_id="sb-target",
        )

    def external_runner(*args: object, **kwargs: object) -> ModalSandboxExecResult:
        nonlocal external_calls
        external_calls += 1
        raise AssertionError("unexpected failures must not reach external fallback")

    monkeypatch.setattr("modal_computer_use.sandbox.modal_daemon_endpoint", endpoint)
    with pytest.raises(RuntimeError, match="programming failure"):
        run_modal_daemon_command_with_fallback(
            SimpleNamespace(),
            ("python", "worker.py"),
            modal_region="us-east",
            external_runner=external_runner,
        )
    assert external_calls == 0


def test_same_region_runner_does_not_fallback_for_terminal_modal_errors(
    monkeypatch,
) -> None:
    modal_exception = pytest.importorskip("modal.exception")

    for error_type in (
        modal_exception.AuthError,
        modal_exception.PermissionDeniedError,
        modal_exception.InvalidError,
        modal_exception.VersionError,
    ):
        external_calls = 0

        def endpoint(
            _computer: object,
            path: str,
            *,
            error_type: type[Exception] = error_type,
        ) -> SimpleNamespace:
            if path == "connect":
                raise error_type("terminal Modal failure")
            raise AssertionError("terminal failures must not prepare external fallback")

        def external_runner(*args: object, **kwargs: object) -> ModalSandboxExecResult:
            nonlocal external_calls
            external_calls += 1
            raise AssertionError("terminal failures must not dispatch external fallback")

        monkeypatch.setattr("modal_computer_use.sandbox.modal_daemon_endpoint", endpoint)

        with pytest.raises(error_type, match="terminal Modal failure"):
            run_modal_daemon_command_with_fallback(
                SimpleNamespace(),
                ("python", "worker.py"),
                modal_region="us-east",
                external_runner=external_runner,
            )

        assert external_calls == 0


def test_same_region_runner_falls_back_for_modal_availability_errors(monkeypatch) -> None:
    modal_exception = pytest.importorskip("modal.exception")

    for error_type in (
        modal_exception.ConnectionError,
        modal_exception.InternalFailure,
        modal_exception.NotFoundError,
        modal_exception.SandboxTerminatedError,
        modal_exception.ServiceError,
        modal_exception.TimeoutError,
    ):
        attempts: list[str] = []

        def endpoint(
            _computer: object,
            path: str,
            *,
            error_type: type[Exception] = error_type,
            attempts: list[str] = attempts,
        ) -> SimpleNamespace:
            attempts.append(path)
            if path == "connect":
                raise error_type("Modal endpoint unavailable")
            return SimpleNamespace(
                path=path,
                base_url="https://external.example",
                token="test-token",
                target_sandbox_id="sb-target",
            )

        monkeypatch.setattr("modal_computer_use.sandbox.modal_daemon_endpoint", endpoint)

        result = run_modal_daemon_command_with_fallback(
            SimpleNamespace(),
            ("python", "worker.py"),
            modal_region="us-east",
            external_runner=lambda *args, **kwargs: ModalSandboxExecResult(
                sandbox_id="sb-external",
                returncode=0,
                stdout="ok",
                stderr="",
            ),
        )

        assert attempts == ["connect", "inherited"]
        assert result.selected_path == "external"
        assert result.fallback_error_type == error_type.__name__


def test_same_region_runner_does_not_fallback_for_invalid_runner_environment(
    monkeypatch,
) -> None:
    external_calls = 0
    monkeypatch.setattr(
        "modal_computer_use.sandbox.modal_daemon_endpoint",
        lambda _computer, path: SimpleNamespace(
            path=path,
            base_url="https://connect.example",
            token="test-token",
            target_sandbox_id="sb-target",
        ),
    )

    def external_runner(*args: object, **kwargs: object) -> ModalSandboxExecResult:
        nonlocal external_calls
        external_calls += 1
        raise AssertionError("invalid runner env must not reach external fallback")

    with pytest.raises(ValueError, match="reserved daemon keys"):
        run_modal_daemon_command_with_fallback(
            SimpleNamespace(),
            ("python", "worker.py"),
            modal_region="us-east",
            env={"COMPUTER_USE_DAEMON_TOKEN": "test-caller-token"},
            external_runner=external_runner,
        )

    assert external_calls == 0


def test_same_region_runner_requires_explicit_external_fallback(monkeypatch) -> None:
    def endpoint(_computer: object, path: str) -> SimpleNamespace:
        if path == "connect":
            raise SandboxUnavailableError("connect preparation failed")
        raise AssertionError("implicit external fallback must not be prepared")

    monkeypatch.setattr("modal_computer_use.sandbox.modal_daemon_endpoint", endpoint)
    with pytest.raises(SandboxUnavailableError, match="connect preparation failed"):
        run_modal_daemon_command_with_fallback(
            SimpleNamespace(),
            ("python", "worker.py"),
            modal_region="us-east",
        )

    external_calls = 0

    def external_runner(*args: object, **kwargs: object) -> ModalSandboxExecResult:
        nonlocal external_calls
        external_calls += 1
        raise AssertionError("invalid commands must not reach external fallback")

    with pytest.raises(ValueError, match="command must not be empty"):
        run_modal_daemon_command_with_fallback(
            SimpleNamespace(),
            (),
            modal_region="us-east",
            external_runner=external_runner,
        )
    assert external_calls == 0


class BoundaryProbe:
    @classmethod
    def with_tcp(cls, port: int) -> str:
        return f"tcp:{port}"


class BoundaryApp:
    @classmethod
    def lookup(cls, app_name: str, **kwargs: Any) -> str:
        return f"app:{app_name}"


class BoundarySandboxObject:
    def __init__(self, tags: dict[str, str]) -> None:
        self.object_id = "sb-boundary"
        self._tags = tags
        self.fail_tcp = False
        self.terminate_wait_calls: list[bool] = []

    def wait_until_ready(self, *, timeout: int) -> None:
        if self.fail_tcp:
            raise TimeoutError("tcp readiness failed")

    def create_connect_token(self, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(url="https://connect.example", token="test-token")

    def terminate(self, *, wait: bool = False) -> None:
        self.terminate_wait_calls.append(wait)

    def get_tags(self) -> dict[str, str]:
        return dict(self._tags)

    def set_tags(self, tags: dict[str, str]) -> None:
        self._tags = dict(tags)


class BoundarySandbox:
    create_calls: ClassVar[list[dict[str, Any]]] = []
    created: ClassVar[BoundarySandboxObject | None] = None
    fail_tcp: ClassVar[bool] = False

    @classmethod
    def create(cls, *args: str, **kwargs: Any) -> BoundarySandboxObject:
        cls.create_calls.append(kwargs)
        tags = kwargs.get("tags")
        assert isinstance(tags, dict)
        cls.created = BoundarySandboxObject(tags)
        cls.created.fail_tcp = cls.fail_tcp
        return cls.created


def boundary_modal() -> SimpleNamespace:
    BoundarySandbox.create_calls = []
    BoundarySandbox.created = None
    BoundarySandbox.fail_tcp = False
    return SimpleNamespace(App=BoundaryApp, Probe=BoundaryProbe, Sandbox=BoundarySandbox)


def test_create_records_supported_and_unsupported_startup_stages(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", boundary_modal())
    monkeypatch.setattr(
        "modal_computer_use.client.DaemonClient.get_json",
        lambda *args, **kwargs: {"ready": True},
    )
    monkeypatch.setattr(
        "modal_computer_use.sandbox._attested_tunnel_parts",
        lambda *args, **kwargs: ("https://daemon.example", "test-token"),
    )
    timing = SessionStartupTiming()
    computer = ComputerSandbox.create(
        config=ComputerConfig(run_id="run-timing"),
        image=object(),
        timing=timing,
    )
    stages = timing.as_dict()["stages"]
    assert stages["scheduled"]["status"] == "unsupported"
    assert stages["daemon_started"]["status"] == "unsupported"
    assert all(
        stages[name]["status"] == "observed"
        for name in (
            "sandbox_create_started",
            "sandbox_registered",
            "tcp_ready",
            "connect_token_ready",
            "connect_ready",
            "attestation_ready",
            "tunnel_ready",
        )
    )
    assert computer.startup_timing is timing
    assert computer._readiness_failure_cleanup == "none"
    daemon_bearer = BoundarySandbox.create_calls[0]["env"][
        "COMPUTER_USE_TUNNEL_TOKEN"
    ]
    assert daemon_bearer
    assert modal_daemon_endpoint(computer, "target-loopback").token == daemon_bearer


def test_create_cleans_up_tcp_and_final_tunnel_readiness_failures(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", boundary_modal())
    BoundarySandbox.fail_tcp = True
    with pytest.raises(TimeoutError, match="tcp readiness failed"):
        ComputerSandbox.create(config=ComputerConfig(), image=object())
    assert BoundarySandbox.created is not None
    assert BoundarySandbox.created.terminate_wait_calls == [True]

    monkeypatch.setitem(__import__("sys").modules, "modal", boundary_modal())
    calls = 0

    def fail_final(*args: Any, **kwargs: Any) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError("tunnel readiness failed")
        return {"ready": True}

    clock = iter((0.0, 0.0, 121.0))
    monkeypatch.setattr("modal_computer_use.client.DaemonClient.get_json", fail_final)
    monkeypatch.setattr("modal_computer_use.sandbox.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("modal_computer_use.sandbox.time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "modal_computer_use.sandbox._attested_tunnel_parts",
        lambda *args, **kwargs: ("https://daemon.example", "test-token"),
    )
    with pytest.raises(TimeoutError, match="daemon did not become ready"):
        ComputerSandbox.create(config=ComputerConfig(), image=object())
    assert BoundarySandbox.created is not None
    assert BoundarySandbox.created.terminate_wait_calls == [True]


def test_create_validates_modal_tag_budget_before_allocation(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", boundary_modal())
    ComputerSandbox.create(
        config=ComputerConfig(run_id="run-123"),
        image=object(),
        tags={
            "benchmark": "modal-region-ab",
            "benchmark_run_id": "benchmark-123",
            "surface": "daemon-transport-floor",
        },
        wait=False,
    )
    assert len(BoundarySandbox.create_calls[0]["tags"]) == 10
    with pytest.raises(ConfigConflictError, match="at most 10 tags"):
        ComputerSandbox.create(
            config=ComputerConfig(run_id="run-123"),
            image=object(),
            owner="alice",
            tags={f"caller-{index}": "value" for index in range(4)},
            wait=False,
        )
    assert len(BoundarySandbox.create_calls) == 1


def test_set_tags_sends_complete_server_state_with_or_without_metadata(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "modal", boundary_modal())
    computer = ComputerSandbox.create(
        config=ComputerConfig(run_id="run-tags"),
        image=object(),
        tags={"computer-use.pool": "pool-a"},
        tag_profile="warm_pool",
        wait=False,
    )
    assert BoundarySandbox.created is not None
    computer.set_tags({"computer-use.pool_state": "ready"})
    assert BoundarySandbox.created._tags["computer-use.pool"] == "pool-a"
    assert BoundarySandbox.created._tags["computer-use.pool_state"] == "ready"
    BoundarySandbox.created._tags["computer-use.pool_state"] = "claimed"
    computer.set_tags({"computer-use.pool_claimed_at": "2026-07-18T00:00:00Z"})
    assert BoundarySandbox.created._tags["computer-use.pool_state"] == "claimed"

    remote = BoundarySandboxObject({"computer-use.pool": "pool-b"})
    attached_without_metadata = ComputerSandbox.local()
    attached_without_metadata._sandbox = remote
    attached_without_metadata.set_tags({"computer-use.pool_state": "claimed"})
    assert remote._tags == {
        "computer-use.pool": "pool-b",
        "computer-use.pool_state": "claimed",
    }
    computer.client.close()
    attached_without_metadata.client.close()


class FakeQueue:
    def __init__(self, values: list[dict[str, Any]] | None = None) -> None:
        self.values: list[Any] = []
        self.partitions: dict[str, list[Any]] = {}
        self.puts: list[dict[str, Any]] = []
        for value in values or []:
            partition = str(value.get("slot_name", ""))
            self.partitions.setdefault(partition, []).append(value)

    def put(
        self,
        value: dict[str, Any],
        *,
        block: bool,
        partition: str,
    ) -> None:
        assert block is False
        self.puts.append(value)
        self.partitions.setdefault(partition, []).append(value)

    def get(self, *, block: bool, partition: str) -> Any:
        assert block is False
        values = self.partitions.setdefault(partition, [])
        return values.pop(0) if values else None


class FakeProcessStdin:
    def __init__(self) -> None:
        self.eof = False

    def write_eof(self) -> None:
        self.eof = True

    def drain(self) -> None:
        return None


class FakeProcess:
    def __init__(self, *, running: bool, returncode: int = 0) -> None:
        self.running = running
        self.returncode = returncode
        self.stdin = FakeProcessStdin()

    def poll(self) -> int | None:
        return None if self.running else self.returncode

    def wait(self) -> int:
        self.running = False
        return self.returncode


class FakeComputer:
    def __init__(
        self,
        sandbox_id: str,
        *,
        actual_region: str = "us-east-1",
        remote_tags: dict[str, str] | None = None,
    ) -> None:
        self.sandbox_id = sandbox_id
        self.actual_region = actual_region
        self.terminated: list[bool] = []
        self.detached = False
        self.tag_updates: list[dict[str, str]] = []
        self.remote_tags = dict(remote_tags or {})

    def exec(self, *args: str, **kwargs: Any) -> FakeProcess:
        script = args[2]
        return FakeProcess(running="fcntl" in script)

    def metadata(self) -> SimpleNamespace:
        return SimpleNamespace(sandbox_id=self.sandbox_id)

    def runtime_region(self) -> str:
        return self.actual_region

    def ensure_browser_ready(self, config: ComputerConfig) -> None:
        assert config.browser is not None

    def first_valid_frame(self, config: ComputerConfig) -> bytes:
        return b"valid-frame"

    def poll(self) -> int | None:
        return None

    def tags(self) -> dict[str, str]:
        return dict(self.remote_tags)

    def get_tags(self) -> dict[str, str]:
        return dict(self.remote_tags)

    def set_tags(
        self,
        tags: dict[str, str],
        *,
        remove: set[str] | None = None,
    ) -> None:
        self.tag_updates.append(tags)
        self.remote_tags.update(tags)
        for key in remove or ():
            self.remote_tags.pop(key, None)

    def terminate(self, *, wait: bool = False) -> None:
        self.terminated.append(wait)

    def detach(self) -> None:
        self.detached = True


def _pool_config() -> ComputerConfig:
    return ComputerConfig(
        resources={"profile": "browser", "cpu": 4, "memory_mib": 8192},
        browser={"kind": "firefox", "prewarm": True},
        runtime={"timeout_seconds": 600, "modal_region": "us-east"},
    )


def _pool_ref(
    *,
    sandbox_id: str,
    slot_name: str,
    identity: str,
    state: str,
    now: datetime,
) -> SandboxRef:
    return SandboxRef(
        sandbox_id=sandbox_id,
        app_name="computer-app",
        name=slot_name,
        created_at=now,
        status="ready",
        tags={
            "computer-use.pool": "prod",
            "computer-use.pool_state": state,
            "computer-use.pool_identity": identity,
            "computer-use.pool_expires_at": (now + timedelta(seconds=500)).isoformat(),
            "computer-use.pool_ready_at": now.isoformat(),
            "computer-use.pool_actual_region": "us-east-1",
        },
    )


def _warm_entry_tags(entry: WarmPoolEntry) -> dict[str, str]:
    return {
        "computer-use.pool": entry.pool_name,
        "computer-use.pool_state": "ready",
        "computer-use.pool_identity": entry.config_identity,
        "computer-use.pool_queue_identity": entry.queue_identity,
    }


def _claim_entry(
    config: ComputerConfig,
    *,
    now: datetime | None = None,
    sandbox_id: str = "sb-warm",
) -> WarmPoolEntry:
    observed_at = now or datetime(2026, 7, 18, tzinfo=UTC)
    return WarmPoolEntry(
        sandbox_id=sandbox_id,
        slot_name="prod-000",
        pool_name="prod",
        app_name="computer-app",
        config_identity=pool_config_identity(config),
        queue_identity="queue-warm",
        created_at=observed_at - timedelta(seconds=20),
        ready_at=observed_at - timedelta(seconds=10),
        expires_at=observed_at + timedelta(seconds=500),
        requested_region="us-east",
        actual_region="us-east-1",
        cpu=4,
        memory_mib=8192,
    )


def test_fill_warm_pool_bounds_capacity_and_enqueues_only_valid_frames(monkeypatch) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    manager.registry = SimpleNamespace(list_sandboxes_with_refs=lambda tags: [])
    queue = FakeQueue()
    created: list[FakeComputer] = []

    def fake_create(**kwargs: Any) -> FakeComputer:
        computer = FakeComputer(f"sb-{len(created)}")
        created.append(computer)
        assert kwargs["name"] in {"prod-000", "prod-001"}
        assert kwargs["tags"]["computer-use.pool"] == "prod"
        return computer

    monkeypatch.setattr("modal_computer_use.manager.ComputerSandbox.create", fake_create)
    now = datetime(2026, 7, 18, tzinfo=UTC)
    result = manager.fill_warm_pool(
        config=_pool_config(),
        policy=WarmPoolPolicy(pool_name="prod", capacity=2),
        queue=queue,
        now=lambda: now,
    )

    assert result.created_count == 2
    assert len(queue.puts) == 2
    assert {item["slot_name"] for item in queue.puts} == {"prod-000", "prod-001"}
    assert all(computer.detached for computer in created)
    assert all(not computer.terminated for computer in created)


def test_fill_warm_pool_retires_incompatible_slot_before_replacement(monkeypatch) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    now = datetime(2026, 7, 18, tzinfo=UTC)
    stale = FakeComputer("sb-stale")
    stale_ref = _pool_ref(
        sandbox_id="sb-stale",
        slot_name="prod-000",
        identity="old-config",
        state="ready",
        now=now,
    )
    manager.registry = SimpleNamespace(list_sandboxes_with_refs=lambda tags: [(stale, stale_ref)])
    replacement = FakeComputer("sb-replacement")
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.create",
        lambda **kwargs: replacement,
    )

    result = manager.fill_warm_pool(
        config=_pool_config(),
        policy=WarmPoolPolicy(pool_name="prod", capacity=1),
        queue=FakeQueue(),
        now=lambda: now,
    )

    assert stale.terminated == [True]
    assert result.existing_count == 0
    assert result.created_count == 1


def test_fill_warm_pool_retires_ready_slots_after_capacity_reduction() -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    now = datetime(2026, 7, 18, tzinfo=UTC)
    config = _pool_config()
    identity = pool_config_identity(config)
    retained = FakeComputer("sb-retained")
    excess = FakeComputer("sb-excess")
    manager.registry = SimpleNamespace(
        list_sandboxes_with_refs=lambda tags: [
            (
                retained,
                _pool_ref(
                    sandbox_id="sb-retained",
                    slot_name="prod-000",
                    identity=identity,
                    state="ready",
                    now=now,
                ),
            ),
            (
                excess,
                _pool_ref(
                    sandbox_id="sb-excess",
                    slot_name="prod-001",
                    identity=identity,
                    state="ready",
                    now=now,
                ),
            ),
        ]
    )

    result = manager.fill_warm_pool(
        config=config,
        policy=WarmPoolPolicy(pool_name="prod", capacity=1),
        queue=FakeQueue(),
        now=lambda: now,
    )

    assert result.existing_count == 1
    assert result.created_count == 0
    assert retained.terminated == []
    assert excess.terminated == [True]


def test_fill_warm_pool_replaces_terminal_claimed_slot(monkeypatch) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    now = datetime(2026, 7, 18, tzinfo=UTC)
    config = _pool_config()
    ref = _pool_ref(
        sandbox_id="sb-finished",
        slot_name="prod-000",
        identity=pool_config_identity(config),
        state="claimed",
        now=now,
    )

    class FinishedComputer(FakeComputer):
        def poll(self) -> int | None:
            return 0

    finished = FinishedComputer("sb-finished", remote_tags=ref.tags)
    replacement = FakeComputer("sb-replacement")
    manager.registry = SimpleNamespace(list_sandboxes_with_refs=lambda tags: [(finished, ref)])
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.create",
        lambda **kwargs: replacement,
    )

    result = manager.fill_warm_pool(
        config=config,
        policy=WarmPoolPolicy(pool_name="prod", capacity=1),
        queue=FakeQueue(),
        now=lambda: now,
    )

    assert finished.terminated == [True]
    assert result.existing_count == 0
    assert result.created_count == 1


def test_fill_warm_pool_rebuilds_lost_ready_slot_partition() -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    now = datetime(2026, 7, 18, tzinfo=UTC)
    config = _pool_config()
    identity = pool_config_identity(config)
    ref = _pool_ref(
        sandbox_id="sb-ready",
        slot_name="prod-000",
        identity=identity,
        state="ready",
        now=now,
    )
    ready = FakeComputer("sb-ready", remote_tags=ref.tags)
    manager.registry = SimpleNamespace(list_sandboxes_with_refs=lambda tags: [(ready, ref)])
    queue = FakeQueue()
    queue.partitions["prod-000"] = [{"stale": True}]

    result = manager.fill_warm_pool(
        config=config,
        policy=WarmPoolPolicy(pool_name="prod", capacity=1),
        queue=queue,
        now=lambda: now,
    )

    queued = queue.partitions["prod-000"]
    assert result.existing_count == 1
    assert result.created_count == 0
    assert len(queued) == 1
    assert queued[0]["sandbox_id"] == "sb-ready"
    assert queued[0]["queue_identity"] == ready.remote_tags["computer-use.pool_queue_identity"]


def test_fill_warm_pool_never_restores_slot_claimed_after_registry_snapshot() -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    now = datetime(2026, 7, 18, tzinfo=UTC)
    config = _pool_config()
    ref = _pool_ref(
        sandbox_id="sb-racing-claim",
        slot_name="prod-000",
        identity=pool_config_identity(config),
        state="ready",
        now=now,
    )

    class ClaimRaceComputer(FakeComputer):
        def exec(self, *args: str, **kwargs: Any) -> FakeProcess:
            if "fcntl" in args[2]:
                self.remote_tags["computer-use.pool_state"] = "claimed"
            return super().exec(*args, **kwargs)

    racing = ClaimRaceComputer("sb-racing-claim", remote_tags=ref.tags)
    manager.registry = SimpleNamespace(list_sandboxes_with_refs=lambda tags: [(racing, ref)])
    queue = FakeQueue()

    result = manager.fill_warm_pool(
        config=config,
        policy=WarmPoolPolicy(pool_name="prod", capacity=1),
        queue=queue,
        now=lambda: now,
    )

    assert result.existing_count == 1
    assert queue.puts == []
    assert racing.remote_tags["computer-use.pool_state"] == "claimed"
    assert "computer-use.pool_queue_identity" not in racing.remote_tags


def test_fill_warm_pool_releases_unconfirmed_lock_holder(monkeypatch) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    now = datetime(2026, 7, 18, tzinfo=UTC)
    config = _pool_config()
    ref = _pool_ref(
        sandbox_id="sb-slow-lock",
        slot_name="prod-000",
        identity=pool_config_identity(config),
        state="ready",
        now=now,
    )

    class SlowLockComputer(FakeComputer):
        holder: FakeProcess | None = None

        def exec(self, *args: str, **kwargs: Any) -> FakeProcess:
            if "fcntl" in args[2]:
                self.holder = FakeProcess(running=True)
                return self.holder
            return FakeProcess(running=False, returncode=1)

    slow = SlowLockComputer("sb-slow-lock", remote_tags=ref.tags)
    manager.registry = SimpleNamespace(list_sandboxes_with_refs=lambda tags: [(slow, ref)])
    monkeypatch.setattr("modal_computer_use.manager.sleep", lambda *_: None)

    manager.fill_warm_pool(
        config=config,
        policy=WarmPoolPolicy(pool_name="prod", capacity=1),
        queue=FakeQueue(),
        now=lambda: now,
    )

    assert slow.holder is not None
    assert slow.holder.stdin.eof is True
    assert slow.holder.running is False


def test_fill_warm_pool_accepts_concurrent_fixed_name_winner(monkeypatch) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    now = datetime(2026, 7, 18, tzinfo=UTC)
    config = _pool_config()
    identity = pool_config_identity(config)
    winner = FakeComputer("sb-winner")
    winner_ref = _pool_ref(
        sandbox_id="sb-winner",
        slot_name="prod-000",
        identity=identity,
        state="provisioning",
        now=now,
    )
    registry_reads = iter(([], [(winner, winner_ref)]))
    manager.registry = SimpleNamespace(list_sandboxes_with_refs=lambda tags: next(registry_reads))

    def lose_fixed_name_race(**kwargs: Any) -> FakeComputer:
        raise RuntimeError("sandbox name is already in use")

    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.create",
        lose_fixed_name_race,
    )
    result = manager.fill_warm_pool(
        config=config,
        policy=WarmPoolPolicy(pool_name="prod", capacity=1),
        queue=FakeQueue(),
        now=lambda: now,
    )

    assert result.existing_count == 1
    assert result.created_count == 0


def test_fill_warm_pool_cleans_up_when_enqueue_fails(monkeypatch) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    manager.registry = SimpleNamespace(list_sandboxes_with_refs=lambda tags: [])
    computer = FakeComputer("sb-failed")

    class FailingQueue(FakeQueue):
        def put(
            self,
            value: dict[str, Any],
            *,
            block: bool,
            partition: str,
        ) -> None:
            assert block is False
            raise RuntimeError("queue unavailable")

    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.create", lambda **kwargs: computer
    )
    with pytest.raises(RuntimeError, match="queue unavailable"):
        manager.fill_warm_pool(
            config=_pool_config(),
            policy=WarmPoolPolicy(pool_name="prod", capacity=1),
            queue=FailingQueue(),
        )
    assert computer.terminated == [True]
    assert computer.detached is True


def test_fill_warm_pool_rejects_new_slot_that_ages_out_during_startup(monkeypatch) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    manager.registry = SimpleNamespace(list_sandboxes_with_refs=lambda tags: [])
    computer = FakeComputer("sb-aged")
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.create",
        lambda **kwargs: computer,
    )
    started_at = datetime(2026, 7, 18, tzinfo=UTC)
    times = iter((started_at, started_at, started_at + timedelta(seconds=590)))
    queue = FakeQueue()

    result = manager.fill_warm_pool(
        config=_pool_config(),
        policy=WarmPoolPolicy(pool_name="prod", capacity=1, min_remaining_seconds=30),
        queue=queue,
        now=lambda: next(times),
    )

    assert result.created_count == 0
    assert queue.puts == []
    assert computer.terminated == [True]
    assert computer.detached is True


def test_claim_warm_pool_rejects_stale_then_hits_and_is_one_shot(monkeypatch) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    config = _pool_config()
    policy = WarmPoolPolicy(pool_name="prod", capacity=2, min_remaining_seconds=30)
    now = datetime(2026, 7, 18, tzinfo=UTC)
    identity = pool_config_identity(config)
    stale = WarmPoolEntry(
        sandbox_id="sb-stale",
        slot_name="prod-000",
        pool_name="prod",
        app_name="computer-app",
        config_identity=identity,
        queue_identity="queue-stale",
        created_at=now - timedelta(seconds=590),
        ready_at=now - timedelta(seconds=580),
        expires_at=now + timedelta(seconds=10),
        requested_region="us-east",
        actual_region="us-east-1",
        cpu=4,
        memory_mib=8192,
    )
    valid = WarmPoolEntry(
        sandbox_id="sb-valid",
        slot_name="prod-001",
        pool_name="prod",
        app_name="computer-app",
        config_identity=identity,
        queue_identity="queue-valid",
        created_at=now - timedelta(seconds=20),
        ready_at=now - timedelta(seconds=10),
        expires_at=now + timedelta(seconds=580),
        requested_region="us-east",
        actual_region="us-east-1",
        cpu=4,
        memory_mib=8192,
    )
    queue = FakeQueue([stale.as_dict(), valid.as_dict()])
    attached: dict[str, FakeComputer] = {}
    entries_by_id = {stale.sandbox_id: stale, valid.sandbox_id: valid}

    def fake_attach(*, sandbox_id: str, **kwargs: Any) -> FakeComputer:
        computer = FakeComputer(
            sandbox_id,
            remote_tags=_warm_entry_tags(entries_by_id[sandbox_id]),
        )
        attached[sandbox_id] = computer
        return computer

    monkeypatch.setattr("modal_computer_use.manager.ComputerSandbox.attach", fake_attach)
    claim = manager.claim_warm_pool(
        config=config,
        policy=policy,
        queue=queue,
        now=lambda: now,
    )

    assert claim.metrics.hit is True
    assert claim.metrics.rejection_reasons == ("near_expiry",)
    assert attached["sb-stale"].terminated == [True]
    assert attached["sb-stale"].detached is True
    assert claim.entry == valid
    assert claim.computer._requested_modal_region == "us-east"
    claim.close()
    assert attached["sb-valid"].terminated == [True]
    assert attached["sb-valid"].detached is True


def test_claim_warm_pool_skips_stale_entry_when_retirement_attach_is_unavailable(
    monkeypatch,
) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    config = _pool_config()
    now = datetime(2026, 7, 18, tzinfo=UTC)
    stale = WarmPoolEntry.from_dict(
        {
            **_claim_entry(config, now=now, sandbox_id="sb-stale").as_dict(),
            "expires_at": (now + timedelta(seconds=10)).isoformat(),
        }
    )
    valid = WarmPoolEntry.from_dict(
        {
            **_claim_entry(config, now=now, sandbox_id="sb-valid").as_dict(),
            "slot_name": "prod-001",
            "queue_identity": "queue-valid",
        }
    )
    warm = FakeComputer("sb-valid", remote_tags=_warm_entry_tags(valid))

    def attach(*, sandbox_id: str, **kwargs: Any) -> FakeComputer:
        if sandbox_id == "sb-stale":
            raise SandboxUnavailableError("stale candidate is unavailable")
        return warm

    monkeypatch.setattr("modal_computer_use.manager.ComputerSandbox.attach", attach)

    claim = manager.claim_warm_pool(
        config=config,
        policy=WarmPoolPolicy(pool_name="prod", capacity=2),
        queue=FakeQueue([stale.as_dict(), valid.as_dict()]),
        now=lambda: now,
    )

    assert claim.computer is warm
    assert claim.metrics.rejection_reasons == ("near_expiry",)


def test_claim_warm_pool_propagates_programming_failure_retiring_stale_entry(
    monkeypatch,
) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    config = _pool_config()
    now = datetime(2026, 7, 18, tzinfo=UTC)
    stale = WarmPoolEntry.from_dict(
        {
            **_claim_entry(config, now=now, sandbox_id="sb-stale").as_dict(),
            "expires_at": (now + timedelta(seconds=10)).isoformat(),
        }
    )

    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.attach",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("programming failure")),
    )
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.create",
        lambda **kwargs: pytest.fail("programming failure must not create cold fallback"),
    )

    with pytest.raises(AssertionError, match="programming failure"):
        manager.claim_warm_pool(
            config=config,
            policy=WarmPoolPolicy(pool_name="prod", capacity=1),
            queue=FakeQueue([stale.as_dict()]),
            now=lambda: now,
        )


def test_claim_warm_pool_rechecks_lifetime_after_readiness_validation(monkeypatch) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    config = _pool_config()
    policy = WarmPoolPolicy(pool_name="prod", capacity=1, min_remaining_seconds=30)
    started_at = datetime(2026, 7, 18, tzinfo=UTC)
    entry = WarmPoolEntry(
        sandbox_id="sb-warm",
        slot_name="prod-000",
        pool_name="prod",
        app_name="computer-app",
        config_identity=pool_config_identity(config),
        queue_identity="queue-warm",
        created_at=started_at - timedelta(seconds=500),
        ready_at=started_at - timedelta(seconds=490),
        expires_at=started_at + timedelta(seconds=100),
        requested_region="us-east",
        actual_region="us-east-1",
        cpu=4,
        memory_mib=8192,
    )
    warm = FakeComputer("sb-warm", remote_tags=_warm_entry_tags(entry))
    cold = FakeComputer("sb-cold")
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.attach",
        lambda **kwargs: warm,
    )
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.create",
        lambda **kwargs: cold,
    )
    times = iter((started_at, started_at + timedelta(seconds=80)))

    claim = manager.claim_warm_pool(
        config=config,
        policy=policy,
        queue=FakeQueue([entry.as_dict()]),
        now=lambda: next(times),
    )

    assert claim.metrics.hit is False
    assert claim.metrics.cold_fallback is True
    assert claim.metrics.rejection_reasons == ("near_expiry",)
    assert warm.terminated == [True]
    assert warm.detached is True
    assert claim.computer is cold


def test_claim_warm_pool_skips_stale_queue_identity_in_same_partition(monkeypatch) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    config = _pool_config()
    now = datetime(2026, 7, 18, tzinfo=UTC)
    valid = WarmPoolEntry(
        sandbox_id="sb-warm",
        slot_name="prod-000",
        pool_name="prod",
        app_name="computer-app",
        config_identity=pool_config_identity(config),
        queue_identity="queue-current",
        created_at=now - timedelta(seconds=20),
        ready_at=now - timedelta(seconds=10),
        expires_at=now + timedelta(seconds=500),
        requested_region="us-east",
        actual_region="us-east-1",
        cpu=4,
        memory_mib=8192,
    )
    stale = WarmPoolEntry.from_dict({**valid.as_dict(), "queue_identity": "queue-stale"})
    warm = FakeComputer("sb-warm", remote_tags=_warm_entry_tags(valid))
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.attach",
        lambda **kwargs: warm,
    )

    claim = manager.claim_warm_pool(
        config=config,
        policy=WarmPoolPolicy(pool_name="prod", capacity=1),
        queue=FakeQueue([stale.as_dict(), valid.as_dict()]),
        now=lambda: now,
    )

    assert claim.metrics.hit is True
    assert claim.metrics.rejection_reasons == ("queue_identity_mismatch",)
    assert claim.entry == valid


def test_claim_warm_pool_ignores_entry_from_another_modal_app(monkeypatch) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    config = _pool_config()
    now = datetime(2026, 7, 18, tzinfo=UTC)
    foreign = WarmPoolEntry(
        sandbox_id="sb-foreign",
        slot_name="prod-000",
        pool_name="prod",
        app_name="other-app",
        config_identity=pool_config_identity(config),
        queue_identity="queue-foreign",
        created_at=now - timedelta(seconds=20),
        ready_at=now - timedelta(seconds=10),
        expires_at=now + timedelta(seconds=500),
        requested_region="us-east",
        actual_region="us-east-1",
        cpu=4,
        memory_mib=8192,
    )
    cold = FakeComputer("sb-cold")
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.attach",
        lambda **kwargs: pytest.fail("foreign app entry must not be attached"),
    )
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.create",
        lambda **kwargs: cold,
    )

    claim = manager.claim_warm_pool(
        config=config,
        policy=WarmPoolPolicy(pool_name="prod", capacity=1),
        queue=FakeQueue([foreign.as_dict()]),
        now=lambda: now,
    )

    assert claim.metrics.hit is False
    assert claim.metrics.rejection_reasons == ("app_mismatch",)
    assert claim.computer is cold


def test_claim_warm_pool_miss_records_claim_time_and_cold_fallback(monkeypatch) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    queue = FakeQueue()
    cold = FakeComputer("sb-cold")
    monkeypatch.setattr("modal_computer_use.manager.ComputerSandbox.create", lambda **kwargs: cold)
    ticks = iter((10.0, 10.025, 10.15, 10.2))
    claim = manager.claim_warm_pool(
        config=_pool_config(),
        policy=WarmPoolPolicy(pool_name="prod", capacity=2),
        queue=queue,
        monotonic_clock=lambda: next(ticks),
    )

    assert claim.metrics.hit is False
    assert claim.metrics.miss_reason == "empty"
    assert claim.metrics.cold_fallback is True
    assert claim.metrics.claim_elapsed_ms == pytest.approx(25.0)
    assert claim.metrics.request_to_authenticated_ms == pytest.approx(150.0)
    assert claim.metrics.request_to_first_frame_ms == pytest.approx(200.0)
    assert claim.metrics.actual_region == "us-east-1"
    assert claim.computer is cold


def test_claim_warm_pool_hit_records_auth_and_frame_before_claim_mutation(monkeypatch) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    config = _pool_config()
    now = datetime(2026, 7, 18, tzinfo=UTC)
    entry = WarmPoolEntry(
        sandbox_id="sb-warm",
        slot_name="prod-000",
        pool_name="prod",
        app_name="computer-app",
        config_identity=pool_config_identity(config),
        queue_identity="queue-warm",
        created_at=now - timedelta(seconds=20),
        ready_at=now - timedelta(seconds=10),
        expires_at=now + timedelta(seconds=500),
        requested_region="us-east",
        actual_region="us-east-1",
        cpu=4,
        memory_mib=8192,
    )
    warm = FakeComputer("sb-warm", remote_tags=_warm_entry_tags(entry))
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.attach",
        lambda **kwargs: warm,
    )
    ticks = iter((10.0, 10.02, 10.05, 10.08))

    claim = manager.claim_warm_pool(
        config=config,
        policy=WarmPoolPolicy(pool_name="prod", capacity=1),
        queue=FakeQueue([entry.as_dict()]),
        now=lambda: now,
        monotonic_clock=lambda: next(ticks),
    )

    assert claim.metrics.hit is True
    assert claim.metrics.request_to_authenticated_ms == pytest.approx(20.0)
    assert claim.metrics.request_to_first_frame_ms == pytest.approx(50.0)
    assert claim.metrics.claim_elapsed_ms == pytest.approx(80.0)
    assert warm.remote_tags["computer-use.pool_state"] == "claimed"


def test_claim_warm_pool_invalid_entry_is_counted_before_cold_fallback(monkeypatch) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    queue = FakeQueue()
    queue.partitions["prod-000"] = ["corrupt"]
    cold = FakeComputer("sb-cold")
    monkeypatch.setattr("modal_computer_use.manager.ComputerSandbox.create", lambda **kwargs: cold)
    claim = manager.claim_warm_pool(
        config=_pool_config(),
        policy=WarmPoolPolicy(pool_name="prod", capacity=1),
        queue=queue,
    )
    assert claim.metrics.miss_reason == "rejected"
    assert claim.metrics.rejection_reasons == ("invalid_entry",)


def test_claim_warm_pool_falls_back_when_warm_attach_is_unavailable(monkeypatch) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    config = _pool_config()
    entry = _claim_entry(config)
    cold = FakeComputer("sb-cold")

    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.attach",
        lambda **kwargs: (_ for _ in ()).throw(SandboxUnavailableError("gone")),
    )
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.create",
        lambda **kwargs: cold,
    )

    claim = manager.claim_warm_pool(
        config=config,
        policy=WarmPoolPolicy(pool_name="prod", capacity=1),
        queue=FakeQueue([entry.as_dict()]),
        now=lambda: entry.ready_at + timedelta(seconds=10),
    )

    assert claim.computer is cold
    assert claim.metrics.rejection_reasons == ("attach_unavailable",)


def test_claim_warm_pool_does_not_fallback_for_unexpected_attach_failure(
    monkeypatch,
) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    config = _pool_config()
    entry = _claim_entry(config)
    cold_creates = 0

    def fail_attach(**kwargs: Any) -> FakeComputer:
        raise AssertionError("programming failure")

    def create_cold(**kwargs: Any) -> FakeComputer:
        nonlocal cold_creates
        cold_creates += 1
        return FakeComputer("sb-cold")

    monkeypatch.setattr("modal_computer_use.manager.ComputerSandbox.attach", fail_attach)
    monkeypatch.setattr("modal_computer_use.manager.ComputerSandbox.create", create_cold)

    with pytest.raises(AssertionError, match="programming failure"):
        manager.claim_warm_pool(
            config=config,
            policy=WarmPoolPolicy(pool_name="prod", capacity=1),
            queue=FakeQueue([entry.as_dict()]),
            now=lambda: entry.ready_at + timedelta(seconds=10),
        )

    assert cold_creates == 0


def test_claim_warm_pool_does_not_fallback_for_terminal_modal_attach_error(
    monkeypatch,
) -> None:
    modal_exception = pytest.importorskip("modal.exception")
    manager = ComputerSandboxManager(app_name="computer-app")
    config = _pool_config()
    entry = _claim_entry(config)

    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.attach",
        lambda **kwargs: (_ for _ in ()).throw(
            modal_exception.AuthError("invalid Modal credentials")
        ),
    )
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.create",
        lambda **kwargs: pytest.fail("terminal attach errors must not create cold fallback"),
    )

    with pytest.raises(modal_exception.AuthError, match="invalid Modal credentials"):
        manager.claim_warm_pool(
            config=config,
            policy=WarmPoolPolicy(pool_name="prod", capacity=1),
            queue=FakeQueue([entry.as_dict()]),
            now=lambda: entry.ready_at + timedelta(seconds=10),
        )


def test_claim_warm_pool_does_not_fallback_for_daemon_authentication_failure(
    monkeypatch,
) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    config = _pool_config()
    entry = _claim_entry(config)

    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.attach",
        lambda **kwargs: (_ for _ in ()).throw(
            DaemonHTTPError("unauthorized", status_code=401)
        ),
    )
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.create",
        lambda **kwargs: pytest.fail("daemon authentication errors must remain terminal"),
    )

    with pytest.raises(DaemonHTTPError, match="unauthorized"):
        manager.claim_warm_pool(
            config=config,
            policy=WarmPoolPolicy(pool_name="prod", capacity=1),
            queue=FakeQueue([entry.as_dict()]),
            now=lambda: entry.ready_at + timedelta(seconds=10),
        )


def test_claim_warm_pool_keeps_unverified_tag_failure_terminal(monkeypatch) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    config = _pool_config()
    entry = _claim_entry(config)

    class UnverifiedComputer(FakeComputer):
        def tags(self) -> dict[str, str]:
            raise SandboxUnavailableError("live tags unavailable")

    warm = UnverifiedComputer("sb-warm", remote_tags=_warm_entry_tags(entry))
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.attach",
        lambda **kwargs: warm,
    )
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.create",
        lambda **kwargs: pytest.fail("unverified ownership must not create cold fallback"),
    )

    with pytest.raises(SandboxUnavailableError, match="live tags unavailable"):
        manager.claim_warm_pool(
            config=config,
            policy=WarmPoolPolicy(pool_name="prod", capacity=1),
            queue=FakeQueue([entry.as_dict()]),
            now=lambda: entry.ready_at + timedelta(seconds=10),
        )

    assert warm.terminated == []
    assert warm.detached is True


def test_claim_warm_pool_keeps_failed_locked_tag_recheck_terminal(monkeypatch) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    config = _pool_config()
    entry = _claim_entry(config)

    class RecheckFailureComputer(FakeComputer):
        tag_reads = 0

        def tags(self) -> dict[str, str]:
            self.tag_reads += 1
            if self.tag_reads == 2:
                raise SandboxUnavailableError("locked tag recheck unavailable")
            return super().tags()

    warm = RecheckFailureComputer("sb-warm", remote_tags=_warm_entry_tags(entry))
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.attach",
        lambda **kwargs: warm,
    )
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.create",
        lambda **kwargs: pytest.fail("ambiguous locked ownership must remain terminal"),
    )

    with pytest.raises(SandboxUnavailableError, match="locked tag recheck unavailable"):
        manager.claim_warm_pool(
            config=config,
            policy=WarmPoolPolicy(pool_name="prod", capacity=1),
            queue=FakeQueue([entry.as_dict()]),
            now=lambda: entry.ready_at + timedelta(seconds=10),
        )

    assert warm.terminated == []
    assert warm.detached is True


def test_claim_warm_pool_keeps_discard_tag_failure_terminal(monkeypatch) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    config = _pool_config()
    now = datetime(2026, 7, 18, tzinfo=UTC)
    entry = WarmPoolEntry.from_dict(
        {
            **_claim_entry(config, now=now).as_dict(),
            "expires_at": (now + timedelta(seconds=10)).isoformat(),
        }
    )

    class DiscardTagFailureComputer(FakeComputer):
        def tags(self) -> dict[str, str]:
            raise SandboxUnavailableError("discard tag read unavailable")

    warm = DiscardTagFailureComputer(
        "sb-warm",
        remote_tags=_warm_entry_tags(entry),
    )
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.attach",
        lambda **kwargs: warm,
    )
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.create",
        lambda **kwargs: pytest.fail("ambiguous discard must not create cold fallback"),
    )

    with pytest.raises(SandboxUnavailableError, match="discard tag read unavailable"):
        manager.claim_warm_pool(
            config=config,
            policy=WarmPoolPolicy(pool_name="prod", capacity=1),
            queue=FakeQueue([entry.as_dict()]),
            now=lambda: now,
        )

    assert warm.terminated == []
    assert warm.detached is True


def test_claim_warm_pool_records_browser_rejection_before_cold_fallback(
    monkeypatch,
) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    config = _pool_config()
    entry = _claim_entry(config)

    class UnreadyComputer(FakeComputer):
        def ensure_browser_ready(self, config: ComputerConfig) -> None:
            raise BrowserReadinessError("browser prewarm did not succeed")

    warm = UnreadyComputer("sb-warm", remote_tags=_warm_entry_tags(entry))
    cold = FakeComputer("sb-cold")
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.attach",
        lambda **kwargs: warm,
    )
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.create",
        lambda **kwargs: cold,
    )

    claim = manager.claim_warm_pool(
        config=config,
        policy=WarmPoolPolicy(pool_name="prod", capacity=1),
        queue=FakeQueue([entry.as_dict()]),
        now=lambda: entry.ready_at + timedelta(seconds=10),
    )

    assert claim.computer is cold
    assert claim.metrics.rejection_reasons == ("browser_not_ready",)
    assert warm.terminated == [True]
    assert warm.detached is True


def test_claim_warm_pool_records_first_frame_rejection_before_cold_fallback(
    monkeypatch,
) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    config = _pool_config()
    entry = _claim_entry(config)

    class InvalidFrameComputer(FakeComputer):
        def first_valid_frame(self, config: ComputerConfig) -> bytes:
            raise FrameValidationError("first frame could not be decoded")

    warm = InvalidFrameComputer("sb-warm", remote_tags=_warm_entry_tags(entry))
    cold = FakeComputer("sb-cold")
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.attach",
        lambda **kwargs: warm,
    )
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.create",
        lambda **kwargs: cold,
    )

    claim = manager.claim_warm_pool(
        config=config,
        policy=WarmPoolPolicy(pool_name="prod", capacity=1),
        queue=FakeQueue([entry.as_dict()]),
        now=lambda: entry.ready_at + timedelta(seconds=10),
    )

    assert claim.computer is cold
    assert claim.metrics.rejection_reasons == ("first_frame_invalid",)
    assert warm.terminated == [True]
    assert warm.detached is True


@pytest.mark.parametrize(
    ("phase", "error"),
    [
        ("browser", RuntimeError("browser programming failure")),
        ("first_frame", ValueError("frame programming failure")),
    ],
)
def test_claim_warm_pool_keeps_generic_phase_errors_terminal(
    monkeypatch,
    phase: str,
    error: Exception,
) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    config = _pool_config()
    entry = _claim_entry(config)

    class ProgrammingFailureComputer(FakeComputer):
        def ensure_browser_ready(self, config: ComputerConfig) -> None:
            if phase == "browser":
                raise error

        def first_valid_frame(self, config: ComputerConfig) -> bytes:
            if phase == "first_frame":
                raise error
            return super().first_valid_frame(config)

    warm = ProgrammingFailureComputer(
        "sb-warm",
        remote_tags=_warm_entry_tags(entry),
    )
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.attach",
        lambda **kwargs: warm,
    )
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.create",
        lambda **kwargs: pytest.fail("generic phase failures must not create cold fallback"),
    )

    with pytest.raises(type(error), match="programming failure"):
        manager.claim_warm_pool(
            config=config,
            policy=WarmPoolPolicy(pool_name="prod", capacity=1),
            queue=FakeQueue([entry.as_dict()]),
            now=lambda: entry.ready_at + timedelta(seconds=10),
        )

    assert warm.terminated == [True]
    assert warm.detached is True


def test_claim_warm_pool_treats_claim_transition_failure_as_terminal(monkeypatch) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    config = _pool_config()
    entry = _claim_entry(config)
    cold_creates = 0

    class TransitionFailureComputer(FakeComputer):
        def set_tags(
            self,
            tags: dict[str, str],
            *,
            remove: set[str] | None = None,
        ) -> None:
            raise RuntimeError("claim transition failed")

    warm = TransitionFailureComputer("sb-warm", remote_tags=_warm_entry_tags(entry))

    def create_cold(**kwargs: Any) -> FakeComputer:
        nonlocal cold_creates
        cold_creates += 1
        return FakeComputer("sb-cold")

    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.attach",
        lambda **kwargs: warm,
    )
    monkeypatch.setattr("modal_computer_use.manager.ComputerSandbox.create", create_cold)

    with pytest.raises(RuntimeError, match="claim transition failed"):
        manager.claim_warm_pool(
            config=config,
            policy=WarmPoolPolicy(pool_name="prod", capacity=1),
            queue=FakeQueue([entry.as_dict()]),
            now=lambda: entry.ready_at + timedelta(seconds=10),
        )

    assert cold_creates == 0
    assert warm.terminated == [True]
    assert warm.detached is True


def test_claim_warm_pool_preserves_candidate_failure_when_retirement_fails(
    monkeypatch,
) -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    config = _pool_config()
    entry = _claim_entry(config)

    class BrowserFailure(RuntimeError):
        pass

    class CleanupFailure(RuntimeError):
        pass

    class UncleanComputer(FakeComputer):
        def ensure_browser_ready(self, config: ComputerConfig) -> None:
            raise BrowserFailure("browser diagnostic payload")

        def terminate(self, *, wait: bool = False) -> None:
            raise CleanupFailure("cleanup diagnostic payload")

    warm = UncleanComputer("sb-warm", remote_tags=_warm_entry_tags(entry))
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.attach",
        lambda **kwargs: warm,
    )
    monkeypatch.setattr(
        "modal_computer_use.manager.ComputerSandbox.create",
        lambda **kwargs: pytest.fail("incomplete cleanup must not create cold fallback"),
    )

    with pytest.raises(BrowserFailure) as raised:
        manager.claim_warm_pool(
            config=config,
            policy=WarmPoolPolicy(pool_name="prod", capacity=1),
            queue=FakeQueue([entry.as_dict()]),
            now=lambda: entry.ready_at + timedelta(seconds=10),
        )

    notes = getattr(raised.value, "__notes__", [])
    assert notes == ["warm candidate cleanup also failed: terminate (CleanupFailure)"]
    assert "cleanup diagnostic payload" not in " ".join(notes)
    assert warm.detached is True


def test_warm_pool_claim_close_preserves_termination_failure_when_detach_fails() -> None:
    class TerminationFailure(RuntimeError):
        pass

    class DetachFailure(RuntimeError):
        pass

    class FailingComputer:
        def terminate(self, *, wait: bool) -> None:
            assert wait is True
            raise TerminationFailure("termination diagnostic payload")

        def detach(self) -> None:
            raise DetachFailure("detach diagnostic payload")

    claim = WarmPoolClaim(
        computer=FailingComputer(),
        metrics=SimpleNamespace(),
    )

    with pytest.raises(TerminationFailure) as raised:
        claim.close()

    notes = getattr(raised.value, "__notes__", [])
    assert notes == ["claim cleanup also failed: detach (DetachFailure)"]
    assert "detach diagnostic payload" not in " ".join(notes)


def test_reconcile_warm_pool_terminates_expired_and_abandoned_slots() -> None:
    manager = ComputerSandboxManager(app_name="computer-app")
    now = datetime(2026, 7, 18, tzinfo=UTC)
    config = _pool_config()
    identity = pool_config_identity(config)

    class PoolSandbox:
        def __init__(self) -> None:
            self.terminate_calls: list[bool] = []

        def terminate(self, *, wait: bool) -> None:
            self.terminate_calls.append(wait)

    expired = PoolSandbox()
    abandoned = PoolSandbox()
    claimed = PoolSandbox()

    def ref(sandbox_id: str, slot_name: str, state: str, expires_at: datetime) -> SandboxRef:
        return SandboxRef(
            sandbox_id=sandbox_id,
            app_name="computer-app",
            name=slot_name,
            created_at=now - timedelta(seconds=400),
            status="ready",
            tags={
                "computer-use.pool": "prod",
                "computer-use.pool_state": state,
                "computer-use.pool_identity": identity,
                "computer-use.pool_expires_at": expires_at.isoformat(),
            },
        )

    manager.registry = SimpleNamespace(
        list_sandboxes_with_refs=lambda tags: [
            (expired, ref("expired", "prod-000", "ready", now + timedelta(seconds=10))),
            (
                abandoned,
                ref("abandoned", "prod-001", "provisioning", now + timedelta(seconds=500)),
            ),
            (claimed, ref("claimed", "prod-002", "claimed", now + timedelta(seconds=500))),
        ]
    )
    result = manager.reconcile_warm_pool(
        config=config,
        policy=WarmPoolPolicy(pool_name="prod", capacity=3, min_remaining_seconds=30),
        now=now,
    )
    assert result.terminated == (
        ("expired", "near_expiry"),
        ("abandoned", "abandoned_provisioning"),
    )
    assert expired.terminate_calls == [True]
    assert abandoned.terminate_calls == [True]
    assert claimed.terminate_calls == []
