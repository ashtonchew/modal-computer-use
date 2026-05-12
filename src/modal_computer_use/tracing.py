from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ValidationError

from .models import ActionBatchResult, CoordinateSpace, Point, Region, TraceEntry, parse_action
from .observability import get_tracer

if TYPE_CHECKING:
    from .sandbox import ComputerSandbox


class TraceWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, entry: TraceEntry) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json() + "\n")


def load_trace(path: str | Path) -> list[TraceEntry]:
    return ComputerTrace.load(path).entries


@dataclass(frozen=True)
class TraceValidationIssue:
    code: str
    message: str
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.line is not None:
            data["line"] = self.line
        return data


@dataclass(frozen=True)
class TraceValidationResult:
    ok: bool
    errors: list[TraceValidationIssue] = field(default_factory=list)
    warnings: list[TraceValidationIssue] = field(default_factory=list)
    entry_count: int = 0
    action_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
            "entry_count": self.entry_count,
            "action_count": self.action_count,
        }


@dataclass(frozen=True)
class ReplayStep:
    kind: Literal["execute", "skip"]
    line: int
    action_type: str
    action: dict[str, Any] | None = None
    reason: str | None = None
    status: Literal["planned", "executed", "failed", "skipped"] = "planned"
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind,
            "line": self.line,
            "action_type": self.action_type,
            "status": self.status,
        }
        if self.action is not None:
            data["action"] = self.action
        if self.reason is not None:
            data["reason"] = self.reason
        if self.result is not None:
            data["result"] = self.result
        if self.error is not None:
            data["error"] = self.error
        return data


@dataclass(frozen=True)
class TraceReplayPlan:
    ok: bool
    dry_run: bool
    steps: list[ReplayStep]
    validation: TraceValidationResult
    target: str | None = None

    @property
    def executable_count(self) -> int:
        return sum(1 for step in self.steps if step.kind == "execute")

    @property
    def skipped_count(self) -> int:
        return sum(1 for step in self.steps if step.kind == "skip")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "executable_count": self.executable_count,
            "skipped_count": self.skipped_count,
            "target": self.target,
            "steps": [step.to_dict() for step in self.steps],
            "validation": self.validation.to_dict(),
        }


@dataclass(frozen=True)
class _TraceRecord:
    line: int
    entry: TraceEntry | None = None
    error: TraceValidationIssue | None = None


class ComputerTrace:
    def __init__(
        self,
        path: str | Path,
        records: list[_TraceRecord],
        load_errors: list[TraceValidationIssue] | None = None,
    ) -> None:
        self.path = Path(path)
        self._records = records
        self._load_errors = load_errors or []

    @property
    def entries(self) -> list[TraceEntry]:
        return [record.entry for record in self._records if record.entry is not None]

    @classmethod
    def load(cls, path: str | Path) -> ComputerTrace:
        trace_path = Path(path)
        if not trace_path.exists():
            return cls(
                trace_path,
                [],
                [
                    TraceValidationIssue(
                        code="trace_not_found",
                        message=f"trace file does not exist: {trace_path}",
                    )
                ],
            )

        records: list[_TraceRecord] = []
        with trace_path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    records.append(
                        _TraceRecord(
                            line=line_number,
                            error=TraceValidationIssue(
                                code="invalid_json",
                                message=f"line {line_number}: {exc.msg}",
                                line=line_number,
                            ),
                        )
                    )
                    continue
                try:
                    entry = TraceEntry.model_validate(raw)
                except ValidationError as exc:
                    records.append(
                        _TraceRecord(
                            line=line_number,
                            error=TraceValidationIssue(
                                code="invalid_trace_entry",
                                message=f"line {line_number}: {exc.errors()[0]['msg']}",
                                line=line_number,
                            ),
                        )
                    )
                    continue
                records.append(_TraceRecord(line=line_number, entry=entry))
        return cls(trace_path, records)

    def validate(self) -> TraceValidationResult:
        errors = list(self._load_errors)
        errors.extend(record.error for record in self._records if record.error is not None)
        warnings: list[TraceValidationIssue] = []
        action_count = 0

        for record in self._records:
            if record.entry is None:
                continue
            entry = record.entry
            normalized = entry.normalized_action
            if normalized is None:
                errors.append(
                    TraceValidationIssue(
                        code="missing_normalized_action",
                        message="trace entry is missing normalized_action",
                        line=record.line,
                    )
                )
                continue
            action_type = normalized.get("type")
            if not isinstance(action_type, str) or not action_type:
                errors.append(
                    TraceValidationIssue(
                        code="invalid_normalized_action",
                        message="normalized_action.type must be a non-empty string",
                        line=record.line,
                    )
                )
                continue

            action_count += 1
            errors.extend(_validate_result(entry, record.line))
            errors.extend(_validate_error_shape(entry, record.line))
            errors.extend(_validate_artifact_uris(entry, record.line))

            if action_type == "screenshot_after":
                continue
            action_errors, action_warnings = _validate_normalized_action(
                normalized, entry.redactions, record.line, entry.coordinate_space
            )
            errors.extend(action_errors)
            warnings.extend(action_warnings)

        return TraceValidationResult(
            ok=not errors,
            errors=errors,
            warnings=warnings,
            entry_count=len(self.entries),
            action_count=action_count,
        )

    def replay(
        self,
        *,
        dry_run: bool = True,
        target: ComputerSandbox | None = None,
        source: str = "trace-replay",
        stop_on_error: bool = True,
    ) -> TraceReplayPlan:
        if not dry_run and target is None:
            raise ValueError("controlled trace replay requires an explicit target sandbox")
        validation = self.validate()
        steps: list[ReplayStep] = []
        if not validation.ok:
            return TraceReplayPlan(ok=False, dry_run=dry_run, steps=steps, validation=validation)

        for record in self._records:
            if record.entry is None or record.entry.normalized_action is None:
                continue
            normalized = record.entry.normalized_action
            action_type = str(normalized["type"])
            if action_type == "screenshot_after":
                steps.append(
                    ReplayStep(
                        kind="skip",
                        line=record.line,
                        action_type=action_type,
                        reason="metadata pseudo-action",
                        status="skipped" if not dry_run else "planned",
                    )
                )
                continue
            if action_type == "type" and _is_redacted_text(normalized.get("text")):
                steps.append(
                    ReplayStep(
                        kind="skip",
                        line=record.line,
                        action_type=action_type,
                        action=normalized,
                        reason="typed text is redacted",
                        status="skipped" if not dry_run else "planned",
                    )
                )
                continue

            if dry_run:
                steps.append(
                    ReplayStep(
                        kind="execute",
                        line=record.line,
                        action_type=action_type,
                        action=normalized,
                    )
                )
                continue

            assert target is not None
            try:
                with get_tracer(name="modal_computer_use.trace_replay").span(
                    "trace.replay.step",
                    {
                        "trace.line": record.line,
                        "action.type": action_type,
                    },
                ):
                    result = target.actions.run([normalized], source=source)
            except Exception as exc:
                steps.append(
                    ReplayStep(
                        kind="execute",
                        line=record.line,
                        action_type=action_type,
                        action=normalized,
                        status="failed",
                        error={
                            "code": "replay_action_failed",
                            "message": str(exc),
                            "type": type(exc).__name__,
                        },
                    )
                )
                if stop_on_error:
                    break
                continue

            result_payload = _safe_batch_result(result)
            first = result.results[0] if result.results else None
            if first is not None and not first.ok:
                steps.append(
                    ReplayStep(
                        kind="execute",
                        line=record.line,
                        action_type=action_type,
                        action=normalized,
                        status="failed",
                        result=result_payload,
                        error={
                            "code": first.error_code or "replay_action_failed",
                            "message": first.error or "trace replay action failed",
                        },
                    )
                )
                if stop_on_error:
                    break
                continue

            steps.append(
                ReplayStep(
                    kind="execute",
                    line=record.line,
                    action_type=action_type,
                    action=normalized,
                    status="executed",
                    result=result_payload,
                )
            )

        ok = validation.ok and all(step.status != "failed" for step in steps)
        return TraceReplayPlan(
            ok=ok,
            dry_run=dry_run,
            steps=steps,
            validation=validation,
            target=_target_label(target) if target is not None else None,
        )


def _validate_normalized_action(
    normalized: dict[str, Any],
    redactions: list[str],
    line: int,
    entry_coordinate_space: CoordinateSpace | None = None,
) -> tuple[list[TraceValidationIssue], list[TraceValidationIssue]]:
    if normalized.get("type") == "type":
        text = normalized.get("text")
        if isinstance(text, str):
            return [
                TraceValidationIssue(
                    code="unsafe_typed_text",
                    message="type action normalized_action.text must be redacted",
                    line=line,
                )
            ], []
        if not _is_redacted_text(text):
            return [
                TraceValidationIssue(
                    code="invalid_redacted_text",
                    message=(
                        "type action normalized_action.text must be "
                        '{"redacted": true, "length": <int>}'
                    ),
                    line=line,
                )
            ], []
        if "text" not in redactions and "typed_text" not in redactions:
            return [
                TraceValidationIssue(
                    code="missing_text_redaction",
                    message='type action redactions must include "text"',
                    line=line,
                )
            ], []
        if "typed_text" in redactions and "text" not in redactions:
            return [], [
                TraceValidationIssue(
                    code="legacy_text_redaction",
                    message=(
                        'redactions should use "text"; '
                        '"typed_text" is accepted for traces from older docs'
                    ),
                    line=line,
                )
            ]
        return [], []

    try:
        parse_action(normalized)
    except Exception as exc:
        return [
            TraceValidationIssue(
                code="invalid_normalized_action",
                message=f"normalized_action does not match ComputerAction: {exc}",
                line=line,
            )
        ], []
    coordinate_space = entry_coordinate_space or _coordinate_space_from_action_metadata(normalized)
    if coordinate_space is not None:
        return _validate_action_bounds(normalized, coordinate_space, line), []
    return [], []


def _coordinate_space_from_action_metadata(normalized: dict[str, Any]) -> CoordinateSpace | None:
    raw = normalized.get("coordinate_space")
    if raw is None:
        metadata = normalized.get("metadata")
        if isinstance(metadata, dict):
            raw = metadata.get("coordinate_space")
    if raw is None:
        return None
    try:
        return CoordinateSpace.model_validate(raw)
    except ValidationError:
        return None


def _validate_action_bounds(
    normalized: dict[str, Any],
    coordinate_space: CoordinateSpace,
    line: int,
) -> list[TraceValidationIssue]:
    errors: list[TraceValidationIssue] = []
    for label, point in _normalized_points(normalized):
        if point.x >= coordinate_space.desktop_width:
            errors.append(
                TraceValidationIssue(
                    code="coordinate_out_of_bounds",
                    message=(
                        f"{label}.x {point.x} exceeds desktop width "
                        f"{coordinate_space.desktop_width}"
                    ),
                    line=line,
                )
            )
        if point.y >= coordinate_space.desktop_height:
            errors.append(
                TraceValidationIssue(
                    code="coordinate_out_of_bounds",
                    message=(
                        f"{label}.y {point.y} exceeds desktop height "
                        f"{coordinate_space.desktop_height}"
                    ),
                    line=line,
                )
            )
    region = normalized.get("region")
    if isinstance(region, dict):
        try:
            parsed_region = Region.model_validate(region)
        except ValidationError:
            parsed_region = None
        if parsed_region is not None and (
            parsed_region.right > coordinate_space.desktop_width
            or parsed_region.bottom > coordinate_space.desktop_height
        ):
            errors.append(
                TraceValidationIssue(
                    code="coordinate_out_of_bounds",
                    message="region extends beyond trace desktop geometry",
                    line=line,
                )
            )
    return errors


def _normalized_points(
    action: dict[str, Any],
    *,
    prefix: str = "action",
) -> list[tuple[str, Point]]:
    points: list[tuple[str, Point]] = []
    x = action.get("x")
    y = action.get("y")
    if isinstance(x, int) and isinstance(y, int):
        points.append((prefix, Point(x=x, y=y)))
    for key_prefix in ("start", "end"):
        px = action.get(f"{key_prefix}_x")
        py = action.get(f"{key_prefix}_y")
        if isinstance(px, int) and isinstance(py, int):
            points.append((f"{prefix}.{key_prefix}", Point(x=px, y=py)))
    path = action.get("path")
    if isinstance(path, list):
        for index, item in enumerate(path):
            if not isinstance(item, dict):
                continue
            px = item.get("x")
            py = item.get("y")
            if isinstance(px, int) and isinstance(py, int):
                points.append((f"{prefix}.path[{index}]", Point(x=px, y=py)))
    nested = action.get("actions")
    if isinstance(nested, list):
        for index, item in enumerate(nested):
            if isinstance(item, dict):
                points.extend(_normalized_points(item, prefix=f"{prefix}.actions[{index}]"))
    return points


def _validate_result(entry: TraceEntry, line: int) -> list[TraceValidationIssue]:
    errors: list[TraceValidationIssue] = []
    result = entry.result
    if result is None:
        return [
            TraceValidationIssue(
                code="missing_result",
                message="trace entry is missing result",
                line=line,
            )
        ]
    if not isinstance(result.get("ok"), bool):
        errors.append(
            TraceValidationIssue(
                code="invalid_result",
                message="result.ok must be a boolean",
                line=line,
            )
        )
    result_elapsed = result.get("elapsed_ms")
    if entry.elapsed_ms is not None and result_elapsed is not None:
        if not isinstance(result_elapsed, int | float) or result_elapsed < 0:
            errors.append(
                TraceValidationIssue(
                    code="invalid_result",
                    message="result.elapsed_ms must be a non-negative number when present",
                    line=line,
                )
            )
        elif abs(float(result_elapsed) - entry.elapsed_ms) > 1:
            errors.append(
                TraceValidationIssue(
                    code="elapsed_mismatch",
                    message="entry elapsed_ms and result.elapsed_ms differ by more than 1 ms",
                    line=line,
                )
            )
    if result.get("ok") is False and entry.error is None:
        errors.append(
            TraceValidationIssue(
                code="missing_error",
                message="failed result must include trace error shape",
                line=line,
            )
        )
    return errors


def _validate_error_shape(entry: TraceEntry, line: int) -> list[TraceValidationIssue]:
    if entry.error is None:
        return []
    code = entry.error.get("code")
    message = entry.error.get("message")
    errors: list[TraceValidationIssue] = []
    if not isinstance(code, str) or not code:
        errors.append(
            TraceValidationIssue(
                code="invalid_error",
                message="error.code must be a non-empty string",
                line=line,
            )
        )
    if not isinstance(message, str) or not message:
        errors.append(
            TraceValidationIssue(
                code="invalid_error",
                message="error.message must be a non-empty string",
                line=line,
            )
        )
    return errors


def _validate_artifact_uris(entry: TraceEntry, line: int) -> list[TraceValidationIssue]:
    errors: list[TraceValidationIssue] = []
    for field_name in ("screenshot_before_uri", "screenshot_after_uri"):
        uri = getattr(entry, field_name)
        if uri is None:
            continue
        if not _is_safe_artifact_uri(uri):
            errors.append(
                TraceValidationIssue(
                    code="unsafe_artifact_uri",
                    message=f"{field_name} must be a safe artifact:// URI",
                    line=line,
                )
            )
    result = entry.result or {}
    output = result.get("output")
    if isinstance(output, dict):
        uri = output.get("artifact_uri")
        if isinstance(uri, str) and not _is_safe_artifact_uri(uri):
            errors.append(
                TraceValidationIssue(
                    code="unsafe_artifact_uri",
                    message="result.output.artifact_uri must be a safe artifact:// URI",
                    line=line,
                )
            )
    return errors


def _is_safe_artifact_uri(uri: str) -> bool:
    if not uri.startswith("artifact://"):
        return False
    path = uri.removeprefix("artifact://")
    if not path or path.startswith("/") or "\\" in path:
        return False
    parts = path.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _is_redacted_text(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("redacted") is True
        and isinstance(value.get("length"), int)
        and value["length"] >= 0
    )


def _safe_batch_result(result: ActionBatchResult) -> dict[str, Any]:
    payload = result.model_dump(mode="json")
    return _redact_replay_payload(payload)


def _redact_replay_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"data_base64", "bytes"} and item is not None:
                redacted[key] = {"redacted": True, "reason": "screenshot bytes"}
            else:
                redacted[key] = _redact_replay_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_replay_payload(item) for item in value]
    return value


def _target_label(target: object | None) -> str | None:
    if target is None:
        return None
    metadata = getattr(target, "metadata", lambda: None)()
    if metadata is not None:
        sandbox_id = getattr(metadata, "sandbox_id", None)
        run_id = getattr(metadata, "run_id", None)
        if sandbox_id and run_id:
            return f"{sandbox_id} ({run_id})"
        if sandbox_id:
            return str(sandbox_id)
        if run_id:
            return str(run_id)
    client = getattr(target, "client", None)
    base_url = getattr(client, "base_url", None)
    return str(base_url) if base_url else "explicit-target"
