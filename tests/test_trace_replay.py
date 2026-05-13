from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from modal_computer_use.cli import main as cli_main
from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.models import ActionBatchResult, ActionItemResult, TraceEntry
from modal_computer_use.tracing import ComputerTrace


def _write_entries(path, entries: list[TraceEntry]) -> None:
    path.write_text(
        "\n".join(entry.model_dump_json() for entry in entries) + "\n",
        encoding="utf-8",
    )


def _trace_entry(**overrides) -> TraceEntry:
    data = {
        "call_id": "call_123",
        "source": "test",
        "normalized_action": {"type": "move", "x": 1, "y": 2},
        "result": {"ok": True, "elapsed_ms": 2},
        "elapsed_ms": 2,
    }
    data.update(overrides)
    return TraceEntry.model_validate(data)


def test_valid_trace_from_actions_route_validates_and_plans_dry_run(tmp_path) -> None:
    app = create_app(
        DaemonSettings(
            backend="mock",
            artifacts_dir=tmp_path / "artifacts",
            recordings_dir=tmp_path / "recordings",
            trace_dir=tmp_path / "traces",
            trace_actions=True,
            local_token="dev",
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer dev"}) as client:
        response = client.post(
            "/v1/actions/run",
            json={
                "actions": [{"type": "move", "x": 10, "y": 20}],
                "screenshot_after": True,
                "screenshot_options": {"storage": "artifact"},
            },
        )

    assert response.status_code == 200
    trace = ComputerTrace.load(tmp_path / "traces" / "actions.ndjson")
    result = trace.validate()
    assert result.ok is True
    assert result.entry_count == 2
    assert result.action_count == 2

    plan = trace.replay(dry_run=True)
    assert plan.ok is True
    assert [step.kind for step in plan.steps] == ["execute", "skip"]
    assert plan.steps[1].reason == "metadata pseudo-action"


def test_malformed_ndjson_reports_line_number(tmp_path) -> None:
    path = tmp_path / "actions.ndjson"
    path.write_text('{"call_id": "ok"}\n{"bad"\n', encoding="utf-8")

    result = ComputerTrace.load(path).validate()

    assert result.ok is False
    assert [error.code for error in result.errors] == ["invalid_trace_entry", "invalid_json"]
    assert result.errors[1].line == 2


def test_raw_typed_text_is_rejected_as_unsafe(tmp_path) -> None:
    path = tmp_path / "actions.ndjson"
    _write_entries(
        path,
        [
            _trace_entry(
                normalized_action={"type": "type", "text": "secret"},
                redactions=["text"],
            )
        ],
    )

    result = ComputerTrace.load(path).validate()

    assert result.ok is False
    assert result.errors[0].code == "unsafe_typed_text"


def test_nested_raw_typed_text_is_rejected_as_unsafe(tmp_path) -> None:
    path = tmp_path / "actions.ndjson"
    _write_entries(
        path,
        [
            _trace_entry(
                normalized_action={
                    "type": "hold_key",
                    "key": "shift",
                    "actions": [{"type": "type", "text": "secret"}],
                },
            )
        ],
    )

    result = ComputerTrace.load(path).validate()

    assert result.ok is False
    assert result.errors[0].code == "unsafe_typed_text"


def test_redacted_type_action_validates_but_is_not_executable_in_dry_run(tmp_path) -> None:
    path = tmp_path / "actions.ndjson"
    _write_entries(
        path,
        [
            _trace_entry(
                normalized_action={"type": "type", "text": {"redacted": True, "length": 6}},
                redactions=["text"],
            )
        ],
    )

    trace = ComputerTrace.load(path)
    assert trace.validate().ok is True
    plan = trace.replay(dry_run=True)
    assert plan.steps[0].kind == "skip"
    assert plan.steps[0].reason == "typed text is redacted"


def test_nested_redacted_type_action_validates_and_is_skipped_in_replay(tmp_path) -> None:
    path = tmp_path / "actions.ndjson"
    _write_entries(
        path,
        [
            _trace_entry(
                normalized_action={
                    "type": "hold_key",
                    "key": "shift",
                    "actions": [
                        {"type": "type", "text": {"redacted": True, "length": 6}}
                    ],
                },
                redactions=["actions[0].text"],
            )
        ],
    )

    trace = ComputerTrace.load(path)
    assert trace.validate().ok is True
    plan = trace.replay(dry_run=True)
    assert plan.steps[0].kind == "skip"
    assert plan.steps[0].reason == "nested typed text is redacted"


def test_real_replay_executes_supported_actions_and_skips_redacted_text(tmp_path) -> None:
    path = tmp_path / "actions.ndjson"
    _write_entries(
        path,
        [
            _trace_entry(normalized_action={"type": "move", "x": 1, "y": 2}),
            _trace_entry(
                normalized_action={"type": "type", "text": {"redacted": True, "length": 6}},
                redactions=["text"],
            ),
            _trace_entry(normalized_action={"type": "click", "button": "left"}),
        ],
    )
    target = _FakeReplayTarget()

    plan = ComputerTrace.load(path).replay(dry_run=False, target=target)

    assert plan.ok is True
    assert [step.status for step in plan.steps] == ["executed", "skipped", "executed"]
    assert target.actions.seen == [
        {"type": "move", "x": 1, "y": 2},
        {"type": "click", "button": "left"},
    ]


def test_real_replay_stops_on_target_action_failure(tmp_path) -> None:
    path = tmp_path / "actions.ndjson"
    _write_entries(
        path,
        [
            _trace_entry(normalized_action={"type": "move", "x": 1, "y": 2}),
            _trace_entry(normalized_action={"type": "move", "x": 3, "y": 4}),
        ],
    )
    target = _FakeReplayTarget(fail_on=1)

    plan = ComputerTrace.load(path).replay(dry_run=False, target=target)

    assert plan.ok is False
    assert [step.status for step in plan.steps] == ["executed", "failed"]
    assert plan.steps[1].error["code"] == "action_failed"
    assert len(target.actions.seen) == 2


def test_real_replay_sanitizes_screenshot_payloads(tmp_path) -> None:
    path = tmp_path / "actions.ndjson"
    _write_entries(path, [_trace_entry(normalized_action={"type": "screenshot"})])
    target = _FakeReplayTarget(
        output={
            "data_base64": "SECRET_SCREENSHOT_BASE64",
            "artifact_uri": "artifact://screenshots/after.png",
        }
    )

    plan = ComputerTrace.load(path).replay(dry_run=False, target=target)

    serialized = json.dumps(plan.to_dict())
    assert "SECRET_SCREENSHOT_BASE64" not in serialized
    assert plan.steps[0].result["results"][0]["output"]["data_base64"]["redacted"] is True
    assert "artifact://screenshots/after.png" in serialized


def test_replay_without_explicit_target_fails_closed(tmp_path) -> None:
    path = tmp_path / "actions.ndjson"
    _write_entries(path, [_trace_entry()])

    try:
        ComputerTrace.load(path).replay(dry_run=False)
    except ValueError as exc:
        assert "explicit target" in str(exc)
    else:
        raise AssertionError("expected real replay without target to fail")


def test_timeout_and_budget_errors_validate_with_stable_error_shape(tmp_path) -> None:
    path = tmp_path / "actions.ndjson"
    _write_entries(
        path,
        [
            _trace_entry(
                result={
                    "ok": False,
                    "elapsed_ms": 10,
                    "error_code": "timeout",
                    "error": "action timed out after 10 ms",
                    "output": {"code": "timeout", "timeout_ms": 10, "scope": "action"},
                },
                elapsed_ms=10,
                error={"code": "timeout", "message": "action timed out after 10 ms"},
            ),
            _trace_entry(
                normalized_action={"type": "click", "x": 1, "y": 2},
                result={
                    "ok": False,
                    "elapsed_ms": 0,
                    "error_code": "budget_exceeded",
                    "error": "action budget exceeded",
                    "output": {"code": "budget_exceeded"},
                },
                elapsed_ms=0,
                error={"code": "budget_exceeded", "message": "action budget exceeded"},
            ),
        ],
    )

    result = ComputerTrace.load(path).validate()

    assert result.ok is True


def test_unsafe_artifact_uri_is_rejected(tmp_path) -> None:
    path = tmp_path / "actions.ndjson"
    _write_entries(
        path,
        [
            _trace_entry(
                screenshot_after_uri="/tmp/screenshot.png",
                result={"ok": True, "elapsed_ms": 2, "output": {"artifact_uri": "../bad"}},
            )
        ],
    )

    result = ComputerTrace.load(path).validate()

    assert result.ok is False
    assert [error.code for error in result.errors] == [
        "unsafe_artifact_uri",
        "unsafe_artifact_uri",
    ]


@pytest.mark.parametrize(
    "uri",
    [
        "artifact://screenshots/%2e%2e/secret.png",
        "artifact://screenshots/%252e%252e/secret.png",
        "artifact://screenshots%2f%2e%2e%2fsecret.png",
        "artifact://manifest.ndjson",
        "artifact://.Secrets/x",
    ],
)
def test_encoded_or_control_artifact_uri_is_rejected(uri: str, tmp_path) -> None:
    path = tmp_path / "actions.ndjson"
    _write_entries(path, [_trace_entry(screenshot_after_uri=uri)])

    result = ComputerTrace.load(path).validate()

    assert result.ok is False
    assert result.errors[0].code == "unsafe_artifact_uri"


def test_malformed_coordinate_space_fails_trace_entry_validation(tmp_path) -> None:
    path = tmp_path / "actions.ndjson"
    payload = _trace_entry().model_dump(mode="json")
    payload["coordinate_space"] = {
        "desktop_width": 100,
        "desktop_height": 100,
        "image_width": 0,
        "image_height": 100,
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    result = ComputerTrace.load(path).validate()

    assert result.ok is False
    assert result.errors[0].code == "invalid_trace_entry"
    assert result.errors[0].line == 1


def test_trace_coordinate_bounds_error_is_surfaced_before_replay(tmp_path) -> None:
    path = tmp_path / "actions.ndjson"
    _write_entries(
        path,
        [
            _trace_entry(
                normalized_action={"type": "move", "x": 101, "y": 2},
                coordinate_space={
                    "desktop_width": 100,
                    "desktop_height": 100,
                    "image_width": 100,
                    "image_height": 100,
                },
            )
        ],
    )

    result = ComputerTrace.load(path).validate()

    assert result.ok is False
    assert result.errors[0].code == "coordinate_out_of_bounds"


def test_cli_validate_returns_nonzero_for_invalid_trace(tmp_path, capsys) -> None:
    path = tmp_path / "actions.ndjson"
    path.write_text('{"bad"\n', encoding="utf-8")

    exit_code = cli_main(["trace", "validate", str(path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.out)["errors"][0]["line"] == 1


def test_cli_validate_returns_nonzero_for_missing_trace(tmp_path, capsys) -> None:
    path = tmp_path / "missing.ndjson"

    exit_code = cli_main(["trace", "validate", str(path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.out)["errors"][0]["code"] == "trace_not_found"


def test_cli_replay_dry_run_returns_ordered_machine_readable_plan(tmp_path, capsys) -> None:
    path = tmp_path / "actions.ndjson"
    _write_entries(
        path,
        [
            _trace_entry(normalized_action={"type": "move", "x": 1, "y": 2}),
            _trace_entry(normalized_action={"type": "screenshot_after"}),
        ],
    )

    exit_code = cli_main(["trace", "replay", str(path), "--dry-run"])

    captured = capsys.readouterr()
    body = json.loads(captured.out)
    assert exit_code == 0
    assert body["executable_count"] == 1
    assert body["skipped_count"] == 1
    assert [step["kind"] for step in body["steps"]] == ["execute", "skip"]


def test_cli_real_replay_requires_explicit_target(tmp_path, capsys) -> None:
    path = tmp_path / "actions.ndjson"
    _write_entries(path, [_trace_entry()])

    with pytest.raises(SystemExit) as exc_info:
        cli_main(["trace", "replay", str(path)])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "real replay requires" in captured.err


def test_cli_real_replay_uses_explicit_base_url_target(tmp_path, capsys, monkeypatch) -> None:
    path = tmp_path / "actions.ndjson"
    _write_entries(path, [_trace_entry(normalized_action={"type": "move", "x": 1, "y": 2})])
    target = _FakeReplayTarget()

    monkeypatch.setattr("modal_computer_use.cli._trace_replay_target", lambda _args: target)

    exit_code = cli_main(
        ["trace", "replay", str(path), "--base-url", "http://127.0.0.1:8080", "--token", "dev"]
    )

    captured = capsys.readouterr()
    body = json.loads(captured.out)
    assert exit_code == 0
    assert body["dry_run"] is False
    assert body["steps"][0]["status"] == "executed"
    assert target.detached is True


class _FakeReplayActions:
    def __init__(
        self,
        *,
        fail_on: int | None = None,
        output: dict[str, object] | None = None,
    ) -> None:
        self.seen: list[dict[str, object]] = []
        self.fail_on = fail_on
        self.output = output or {}

    def run(self, actions, *, source: str = "sdk"):
        action = dict(actions[0])
        self.seen.append(action)
        ok = self.fail_on is None or len(self.seen) - 1 != self.fail_on
        return ActionBatchResult(
            ok=ok,
            call_id="call_replay",
            results=[
                ActionItemResult(
                    index=0,
                    type=str(action["type"]),
                    ok=ok,
                    elapsed_ms=1,
                    error_code=None if ok else "action_failed",
                    error=None if ok else "failed during replay",
                    output=self.output,
                )
            ],
        )


class _FakeReplayTarget:
    def __init__(
        self,
        *,
        fail_on: int | None = None,
        output: dict[str, object] | None = None,
    ) -> None:
        self.actions = _FakeReplayActions(fail_on=fail_on, output=output)
        self.detached = False

    def detach(self) -> None:
        self.detached = True
