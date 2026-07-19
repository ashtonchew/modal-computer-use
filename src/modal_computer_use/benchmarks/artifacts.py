from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

SANITIZER_VERSION = 1
ARTIFACT_STATUSES = {
    "candidate",
    "current_reference",
    "historical",
    "rejected",
    "superseded",
}
_EPHEMERAL_KEYS = {
    "benchmark_run_id",
    "modal_run_id",
    "modal_sandbox_id",
}
_SECRET_KEYS = {
    "access_key",
    "api_key",
    "authorization",
    "bearer",
    "client_secret",
    "credential",
    "credentials",
    "password",
    "private_key",
    "secret",
    "secret_key",
    "token",
}
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def sanitize_provider_benchmark(
    raw_payload: dict[str, Any],
    *,
    raw_bytes: bytes,
    raw_artifact_path: str,
    harness_commit: str,
    harness_state: str,
    status: str,
    scope: str,
    status_reason: str | None = None,
    harness_diff_sha256: str | None = None,
) -> dict[str, Any]:
    _validate_provenance_inputs(
        raw_artifact_path=raw_artifact_path,
        harness_commit=harness_commit,
        harness_state=harness_state,
        status=status,
        scope=scope,
        status_reason=status_reason,
        harness_diff_sha256=harness_diff_sha256,
    )
    payload = _sanitize_value(copy.deepcopy(raw_payload))
    if not isinstance(payload, dict):
        raise ValueError("provider benchmark payload must be a JSON object")
    payload["base_url"] = None
    provenance = {
        "status": status,
        "scope": scope,
        "harness_commit": harness_commit,
        "raw_artifact_path": raw_artifact_path,
        "raw_artifact_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "raw_artifact_tracked": False,
        "sanitizer": "modal_computer_use.benchmarks.artifacts",
        "sanitizer_version": SANITIZER_VERSION,
        "harness_state": harness_state,
        "sanitization": (
            "removed ephemeral base URLs, benchmark run IDs, Modal run IDs, "
            "and Modal sandbox IDs"
        ),
    }
    if status_reason is not None:
        provenance["status_reason"] = status_reason
    if harness_diff_sha256 is not None:
        provenance["harness_diff_sha256"] = harness_diff_sha256
    payload["provenance"] = provenance
    validate_sanitized_provider_benchmark(payload)
    return payload


def serialize_provider_benchmark(payload: dict[str, Any]) -> str:
    return f"{json.dumps(payload, indent=2, sort_keys=True)}\n"


def generate_sanitized_provider_benchmark(
    *,
    raw_path: Path,
    output_path: Path,
    raw_artifact_path: str,
    harness_commit: str,
    harness_state: str,
    status: str,
    scope: str,
    status_reason: str | None = None,
    harness_diff_sha256: str | None = None,
    check: bool = False,
) -> bool:
    raw_bytes = raw_path.read_bytes()
    raw_payload = json.loads(raw_bytes)
    if not isinstance(raw_payload, dict):
        raise ValueError("provider benchmark payload must be a JSON object")
    sanitized = sanitize_provider_benchmark(
        raw_payload,
        raw_bytes=raw_bytes,
        raw_artifact_path=raw_artifact_path,
        harness_commit=harness_commit,
        harness_state=harness_state,
        status=status,
        scope=scope,
        status_reason=status_reason,
        harness_diff_sha256=harness_diff_sha256,
    )
    rendered = serialize_provider_benchmark(sanitized)
    if check:
        return output_path.is_file() and output_path.read_text(encoding="utf-8") == rendered
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")
    return True


def validate_sanitized_provider_benchmark(payload: dict[str, Any]) -> None:
    if payload.get("base_url") is not None:
        raise ValueError("sanitized provider benchmark base_url must be null")
    _validate_safe_value(payload)
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("sanitized provider benchmark requires provenance")
    commit = provenance.get("harness_commit")
    digest = provenance.get("raw_artifact_sha256")
    if not isinstance(commit, str) or _COMMIT_PATTERN.fullmatch(commit) is None:
        raise ValueError("provenance harness_commit must be a full Git commit")
    if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
        raise ValueError("provenance raw_artifact_sha256 must be a SHA-256 digest")
    raw_path = provenance.get("raw_artifact_path")
    if not isinstance(raw_path, str) or not _is_safe_relative_path(raw_path):
        raise ValueError("provenance raw_artifact_path must be repository-relative")
    status = provenance.get("status")
    if status not in ARTIFACT_STATUSES:
        raise ValueError("provenance status is unsupported")
    scope = provenance.get("scope")
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("provenance scope must not be empty")
    if provenance.get("raw_artifact_tracked") is not False:
        raise ValueError("raw provider benchmark artifacts must remain untracked")
    sanitizer = provenance.get("sanitizer")
    sanitizer_version = provenance.get("sanitizer_version")
    if not isinstance(sanitizer, str) or not sanitizer:
        raise ValueError("provenance sanitizer must be named")
    if (
        isinstance(sanitizer_version, bool)
        or not isinstance(sanitizer_version, int)
        or sanitizer_version < 0
    ):
        raise ValueError("provenance sanitizer_version must be a nonnegative integer")
    if status in {"rejected", "superseded"}:
        status_reason = provenance.get("status_reason")
        if not isinstance(status_reason, str) or not status_reason.strip():
            raise ValueError(f"{status} provenance requires status_reason")
    diff_digest = provenance.get("harness_diff_sha256")
    harness_state = provenance.get("harness_state")
    if diff_digest is not None:
        if not isinstance(diff_digest, str) or _SHA256_PATTERN.fullmatch(diff_digest) is None:
            raise ValueError("provenance harness_diff_sha256 must be a SHA-256 digest")
        if harness_state != "dirty":
            raise ValueError("provenance harness_state must be dirty when a digest is present")
    elif status == "candidate":
        raise ValueError("candidate provenance requires harness_diff_sha256")
    elif harness_state not in {None, "clean"}:
        raise ValueError("provenance harness_state must be clean when no digest is present")
    if status == "current_reference" and (diff_digest is not None or harness_state != "clean"):
        raise ValueError("current_reference provenance requires a clean harness")


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = _normalize_key(key)
            if normalized in _EPHEMERAL_KEYS:
                continue
            if normalized == "base_url":
                sanitized[key] = None
                continue
            sanitized[key] = _sanitize_value(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item) for item in value]
    return value


def _validate_safe_value(value: Any, *, key: str | None = None) -> None:
    if key is not None:
        normalized = _normalize_key(key)
        if normalized in _EPHEMERAL_KEYS:
            raise ValueError(f"sanitized provider benchmark contains ephemeral key: {key}")
        if normalized in _SECRET_KEYS or normalized.endswith(("_token", "_secret", "_password")):
            raise ValueError(f"sanitized provider benchmark contains secret-bearing key: {key}")
    if isinstance(value, dict):
        for item_key, item in value.items():
            _validate_safe_value(item, key=str(item_key))
        return
    if isinstance(value, list):
        for item in value:
            _validate_safe_value(item)
        return
    if isinstance(value, str):
        if re.search(r"Authorization:\s*Bearer\s+\S+", value, flags=re.IGNORECASE):
            raise ValueError("sanitized provider benchmark contains a bearer credential")
        if value.startswith(("http://", "https://")):
            parsed = urlsplit(value)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("sanitized provider benchmark contains a credentialed URL")


def _validate_provenance_inputs(
    *,
    raw_artifact_path: str,
    harness_commit: str,
    harness_state: str,
    status: str,
    scope: str,
    status_reason: str | None,
    harness_diff_sha256: str | None,
) -> None:
    if not _is_safe_relative_path(raw_artifact_path):
        raise ValueError("raw_artifact_path must be repository-relative")
    if _COMMIT_PATTERN.fullmatch(harness_commit) is None:
        raise ValueError("harness_commit must be a full Git commit")
    if status not in ARTIFACT_STATUSES:
        raise ValueError("unsupported provenance status")
    if harness_state not in {"clean", "dirty"}:
        raise ValueError("harness_state must be clean or dirty")
    if not scope.strip():
        raise ValueError("scope must not be empty")
    if status in {"rejected", "superseded"} and not (status_reason or "").strip():
        raise ValueError(f"{status} provenance requires status_reason")
    if harness_diff_sha256 is not None and _SHA256_PATTERN.fullmatch(harness_diff_sha256) is None:
        raise ValueError("harness_diff_sha256 must be a SHA-256 digest")
    if status == "candidate" and harness_diff_sha256 is None:
        raise ValueError("candidate provenance requires harness_diff_sha256")
    if harness_state == "dirty" and harness_diff_sha256 is None:
        raise ValueError("dirty harness provenance requires harness_diff_sha256")
    if harness_state == "clean" and harness_diff_sha256 is not None:
        raise ValueError("clean harness provenance must not include harness_diff_sha256")
    if status == "current_reference" and harness_state != "clean":
        raise ValueError("current_reference provenance requires a clean harness")


def _is_safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts


def _normalize_key(key: str) -> str:
    underscored = re.sub(r"(?<!^)(?=[A-Z])", "_", key)
    return underscored.lower().replace("-", "_")
