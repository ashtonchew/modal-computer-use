from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from .models import TraceEntry, parse_action


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

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind,
            "line": self.line,
            "action_type": self.action_type,
        }
        if self.action is not None:
            data["action"] = self.action
        if self.reason is not None:
            data["reason"] = self.reason
        return data


@dataclass(frozen=True)
class TraceReplayPlan:
    ok: bool
    dry_run: bool
    steps: list[ReplayStep]
    validation: TraceValidationResult

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
                normalized, entry.redactions, record.line
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

    def replay(self, *, dry_run: bool = True) -> TraceReplayPlan:
        if not dry_run:
            raise NotImplementedError("controlled trace replay is planned for v1.0")
        validation = self.validate()
        steps: list[ReplayStep] = []
        if not validation.ok:
            return TraceReplayPlan(ok=False, dry_run=True, steps=steps, validation=validation)

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
                    )
                )
                continue
            steps.append(
                ReplayStep(
                    kind="execute",
                    line=record.line,
                    action_type=action_type,
                    action=normalized,
                )
            )

        return TraceReplayPlan(ok=True, dry_run=True, steps=steps, validation=validation)


def _validate_normalized_action(
    normalized: dict[str, Any], redactions: list[str], line: int
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
    return [], []


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
