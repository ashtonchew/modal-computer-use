from __future__ import annotations

import json

from fastapi.testclient import TestClient

from modal_computer_use.cli import main as cli_main
from modal_computer_use.daemon.app import create_app
from modal_computer_use.daemon.settings import DaemonSettings
from modal_computer_use.models import TraceEntry
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
