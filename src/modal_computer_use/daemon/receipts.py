from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from modal_computer_use.daemon.errors import DaemonError, public_input_error
from modal_computer_use.daemon.leases import LeaseCredentials, MutationLease

OPERATION_SEQUENCE_HEADER = "x-computer-use-operation-sequence"
RECEIPT_PROTOCOL_VERSION = "1"
MAX_OPERATION_SEQUENCE = (1 << 63) - 1

ReceiptState = Literal["IN_PROGRESS", "COMPLETED", "INDETERMINATE"]


class _Headers(Protocol):
    def get(self, key: str, default: str | None = None) -> str | None: ...


@dataclass(frozen=True, slots=True)
class ReceiptHandle:
    run_id: str = field(repr=False)
    sequence: int
    fingerprint: str = field(repr=False)
    operation_kind: str
    started_at: float
    existing_state: ReceiptState | None = None


class ReceiptJournal:
    def __init__(self, runtime_dir: Path) -> None:
        self._runtime_dir = runtime_dir
        self._db_path = runtime_dir / "trajectory-receipts.sqlite3"
        self._key_path = runtime_dir / ".trajectory-receipts.key"
        self._executor: ThreadPoolExecutor | None = self._new_executor()
        self._key: bytes | None = None
        self._started = False
        self._recovery_required = False
        self._incident_id: str | None = None
        self._memory_classification: str | None = None
        self._volatile_recovery = False
        self._volatile_run_id: str | None = None

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def key_path(self) -> Path:
        return self._key_path

    async def start(self) -> None:
        if self._started:
            return
        if self._executor is None:
            self._executor = self._new_executor()
        loop = asyncio.get_running_loop()
        self._key = await loop.run_in_executor(self._executor, self._initialize)
        self._started = True
        status = await self.recovery_status()
        self._recovery_required = bool(status["recovery_required"])
        self._incident_id = status.get("incident_id")
        self._memory_classification = status.get("classification")

    async def close(self) -> None:
        executor = self._executor
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        self._executor = None
        self._started = False

    async def validate_acquire(self, run_id: str) -> None:
        if self._recovery_required:
            raise _recovery_required_error(self._incident_id)
        await self._call(self._validate_acquire_sync, run_id)

    async def activate_run(self, run_id: str) -> None:
        await self._call(self._activate_run_sync, run_id)

    async def ensure_mutation_allowed(self) -> None:
        if self._recovery_required:
            raise _recovery_required_error(self._incident_id)

    async def begin(
        self,
        *,
        lease: MutationLease,
        sequence: int,
        operation_kind: str,
        semantic_data: Any,
    ) -> ReceiptHandle:
        fingerprint = self._fingerprint(operation_kind, semantic_data)
        handle, cancellation = await self._call_to_completion(
            self._begin_sync,
            lease,
            sequence,
            operation_kind,
            fingerprint,
        )
        if cancellation is not None:
            if handle.existing_state is None:
                try:
                    await self._call_to_completion(
                        self._abandon_sync,
                        handle,
                        "cancelled_before_dispatch",
                    )
                except Exception as exc:
                    self._enter_volatile_recovery(
                        handle,
                        classification="cancellation_reconciliation_failed",
                    )
                    raise cancellation from exc
            raise cancellation
        return handle

    async def complete(
        self,
        handle: ReceiptHandle,
        *,
        classification: str = "completed",
    ) -> None:
        incident_id = self._enter_volatile_recovery(
            handle,
            classification="terminal_commit_failed",
        )
        _, cancellation = await self._call_to_completion(
            self._finish_sync,
            handle,
            "COMPLETED",
            classification,
            None,
        )
        self._clear_memory_recovery(incident_id)
        if cancellation is not None:
            raise cancellation

    async def abandon(self, handle: ReceiptHandle, *, classification: str) -> None:
        incident_id = self._enter_volatile_recovery(
            handle,
            classification="terminal_commit_failed",
        )
        _, cancellation = await self._call_to_completion(
            self._abandon_sync,
            handle,
            classification,
        )
        self._clear_memory_recovery(incident_id)
        if cancellation is not None:
            raise cancellation

    async def mark_indeterminate(
        self,
        handle: ReceiptHandle,
        *,
        classification: str,
    ) -> str:
        incident_id = self._enter_volatile_recovery(
            handle,
            classification=classification,
        )
        _, cancellation = await self._call_to_completion(
            self._finish_sync,
            handle,
            "INDETERMINATE",
            classification,
            incident_id,
        )
        self._volatile_recovery = False
        self._volatile_run_id = None
        if cancellation is not None:
            raise cancellation
        return incident_id

    async def seal_run(self, run_id: str, reason: str) -> None:
        await self._call(self._seal_run_sync, run_id, reason)

    async def recovery_status(self) -> dict[str, Any]:
        durable = await self._call(self._recovery_status_sync)
        if self._volatile_recovery:
            return {
                "recovery_required": True,
                "incident_id": self._incident_id,
                "classification": self._memory_classification,
            }
        return durable

    async def acknowledge(self, incident_id: str) -> dict[str, Any]:
        if self._volatile_recovery:
            current_incident = self._incident_id
            if current_incident is None or not secrets.compare_digest(
                current_incident, incident_id
            ):
                raise _recovery_incident_mismatch_error()
            result, cancellation = await self._call_to_completion(
                self._acknowledge_volatile_sync,
                incident_id,
                self._volatile_run_id,
                self._memory_classification or "terminal_commit_failed",
            )
            self._clear_memory_recovery(incident_id)
            if cancellation is not None:
                raise cancellation
            return result
        result, cancellation = await self._call_to_completion(
            self._acknowledge_sync,
            incident_id,
        )
        self._clear_memory_recovery(incident_id)
        if cancellation is not None:
            raise cancellation
        return result

    async def receipt_status(self, run_id: str, sequence: int) -> dict[str, Any]:
        return await self._call(self._receipt_status_sync, run_id, sequence)

    async def resolve(self, run_id: str, sequence: int) -> dict[str, Any]:
        return await self._call(self._resolve_sync, run_id, sequence)

    async def resolved_missing_proof(
        self,
        run_id: str,
        sequence: int,
    ) -> dict[str, Any] | None:
        return await self._call(self._resolved_missing_proof_sync, run_id, sequence)

    async def _call(self, function: Any, *args: Any) -> Any:
        result, cancellation = await self._call_to_completion(function, *args)
        if cancellation is not None:
            raise cancellation
        return result

    async def _call_to_completion(
        self,
        function: Any,
        *args: Any,
    ) -> tuple[Any, asyncio.CancelledError | None]:
        if not self._started:
            await self.start()
        loop = asyncio.get_running_loop()
        executor = self._executor
        if executor is None:
            raise RuntimeError("receipt journal executor is unavailable")
        future = loop.run_in_executor(executor, function, *args)
        cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                result = await asyncio.shield(future)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
                continue
            return result, cancellation

    def _enter_volatile_recovery(
        self,
        handle: ReceiptHandle,
        *,
        classification: str,
    ) -> str:
        incident_id = f"incident_{secrets.token_urlsafe(24)}"
        self._recovery_required = True
        self._incident_id = incident_id
        self._memory_classification = classification
        self._volatile_recovery = True
        self._volatile_run_id = handle.run_id
        return incident_id

    def _clear_memory_recovery(self, incident_id: str) -> None:
        if self._incident_id is None or not secrets.compare_digest(
            self._incident_id, incident_id
        ):
            return
        self._recovery_required = False
        self._incident_id = None
        self._memory_classification = None
        self._volatile_recovery = False
        self._volatile_run_id = None

    @staticmethod
    def _new_executor() -> ThreadPoolExecutor:
        return ThreadPoolExecutor(max_workers=1, thread_name_prefix="receipt-journal")

    def _initialize(self) -> bytes:
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        key = self._load_or_create_key()
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    next_sequence INTEGER NOT NULL,
                    sealed INTEGER NOT NULL DEFAULT 0,
                    seal_reason TEXT,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    operation_kind TEXT NOT NULL,
                    lease_epoch TEXT NOT NULL,
                    lease_fence INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    classification TEXT,
                    duration_ms REAL,
                    incident_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS recovery (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    recovery_required INTEGER NOT NULL,
                    incident_id TEXT,
                    classification TEXT,
                    updated_at REAL NOT NULL
                );
                """
            )
            now = time.time()
            connection.execute(
                "INSERT OR IGNORE INTO recovery VALUES (1, 0, NULL, NULL, ?)",
                (now,),
            )
            interrupted = connection.execute(
                "SELECT run_id, sequence FROM operations WHERE state = 'IN_PROGRESS'"
            ).fetchall()
            if interrupted:
                incident_id = f"incident_{secrets.token_urlsafe(24)}"
                connection.execute(
                    """UPDATE operations
                       SET state = 'INDETERMINATE', classification = 'daemon_restart',
                           incident_id = ?, updated_at = ?
                       WHERE state = 'IN_PROGRESS'""",
                    (incident_id, now),
                )
                connection.execute(
                    """UPDATE recovery SET recovery_required = 1, incident_id = ?,
                       classification = 'daemon_restart', updated_at = ? WHERE singleton = 1""",
                    (incident_id, now),
                )
            connection.execute(
                """UPDATE runs SET sealed = 1,
                   seal_reason = COALESCE(seal_reason, 'daemon_restart'), updated_at = ?
                   WHERE sealed = 0""",
                (now,),
            )
            connection.commit()
        return key

    def _load_or_create_key(self) -> bytes:
        try:
            descriptor = os.open(self._key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            os.chmod(self._key_path, 0o600)
            key = self._key_path.read_bytes()
            if len(key) != 32:
                raise RuntimeError("receipt HMAC key is invalid") from None
            return key
        key = secrets.token_bytes(32)
        try:
            os.write(descriptor, key)
        finally:
            os.close(descriptor)
        return key

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _fingerprint(self, operation_kind: str, semantic_data: Any) -> str:
        key = self._key
        if key is None:
            raise RuntimeError("receipt journal is not started")
        canonical = json.dumps(
            {"kind": operation_kind, "semantic": _json_value(semantic_data)},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hmac.new(key, canonical, hashlib.sha256).hexdigest()

    def _validate_acquire_sync(self, run_id: str) -> None:
        with self._connect() as connection:
            recovery = connection.execute(
                "SELECT recovery_required, incident_id FROM recovery WHERE singleton = 1"
            ).fetchone()
            if recovery is not None and recovery["recovery_required"]:
                raise _recovery_required_error(recovery["incident_id"])
            run = connection.execute(
                "SELECT sealed FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is not None and run["sealed"]:
                raise DaemonError(
                    "trajectory run is sealed",
                    status_code=409,
                    code="run_sealed",
                )

    def _activate_run_sync(self, run_id: str) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT sealed FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if existing is not None and existing["sealed"]:
                raise DaemonError(
                    "trajectory run is sealed", status_code=409, code="run_sealed"
                )
            connection.execute(
                "INSERT OR IGNORE INTO runs VALUES (?, 0, 0, NULL, ?)",
                (run_id, now),
            )
            connection.commit()

    def _begin_sync(
        self,
        lease: MutationLease,
        sequence: int,
        operation_kind: str,
        fingerprint: str,
    ) -> ReceiptHandle:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            recovery = connection.execute(
                "SELECT recovery_required, incident_id FROM recovery WHERE singleton = 1"
            ).fetchone()
            if recovery is not None and recovery["recovery_required"]:
                raise _recovery_required_error(recovery["incident_id"])
            existing = connection.execute(
                "SELECT * FROM operations WHERE run_id = ? AND sequence = ?",
                (lease.run_id, sequence),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(existing["fingerprint"], fingerprint):
                    raise DaemonError(
                        "operation sequence was already used for different semantics",
                        status_code=409,
                        code="run_sequence_conflict",
                    )
                return ReceiptHandle(
                    run_id=lease.run_id,
                    sequence=sequence,
                    fingerprint=fingerprint,
                    operation_kind=existing["operation_kind"],
                    started_at=now,
                    existing_state=existing["state"],
                )
            run = connection.execute(
                "SELECT next_sequence, sealed FROM runs WHERE run_id = ?",
                (lease.run_id,),
            ).fetchone()
            if run is None:
                connection.execute(
                    "INSERT INTO runs VALUES (?, 0, 0, NULL, ?)",
                    (lease.run_id, now),
                )
                expected = 0
            else:
                if run["sealed"]:
                    raise DaemonError(
                        "trajectory run is sealed", status_code=409, code="run_sealed"
                    )
                expected = int(run["next_sequence"])
            if sequence != expected:
                raise DaemonError(
                    "operation sequence is not the next expected value",
                    status_code=409,
                    code="operation_sequence_gap",
                    details={"expected_sequence": expected, "received_sequence": sequence},
                )
            connection.execute(
                """INSERT INTO operations
                   (run_id, sequence, fingerprint, operation_kind, lease_epoch, lease_fence,
                    state, classification, duration_ms, incident_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'IN_PROGRESS', NULL, NULL, NULL, ?, ?)""",
                (
                    lease.run_id,
                    sequence,
                    fingerprint,
                    operation_kind,
                    lease.epoch,
                    lease.fence,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE runs SET next_sequence = ?, updated_at = ? WHERE run_id = ?",
                (sequence + 1, now, lease.run_id),
            )
            connection.commit()
        return ReceiptHandle(
            run_id=lease.run_id,
            sequence=sequence,
            fingerprint=fingerprint,
            operation_kind=operation_kind,
            started_at=time.perf_counter(),
        )

    def _finish_sync(
        self,
        handle: ReceiptHandle,
        state: ReceiptState,
        classification: str,
        incident_id: str | None,
    ) -> None:
        now = time.time()
        duration_ms = max(0.0, (time.perf_counter() - handle.started_at) * 1000)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE operations SET state = ?, classification = ?, duration_ms = ?,
                   incident_id = ?, updated_at = ? WHERE run_id = ? AND sequence = ?""",
                (
                    state,
                    classification,
                    duration_ms,
                    incident_id,
                    now,
                    handle.run_id,
                    handle.sequence,
                ),
            )
            if state == "INDETERMINATE":
                connection.execute(
                    """UPDATE runs SET sealed = 1, seal_reason = 'indeterminate',
                       updated_at = ? WHERE run_id = ?""",
                    (now, handle.run_id),
                )
                connection.execute(
                    """UPDATE recovery SET recovery_required = 1, incident_id = ?,
                       classification = ?, updated_at = ? WHERE singleton = 1""",
                    (incident_id, classification, now),
                )
            connection.commit()

    def _abandon_sync(self, handle: ReceiptHandle, classification: str) -> None:
        del classification
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """DELETE FROM operations WHERE run_id = ? AND sequence = ?
                   AND state = 'IN_PROGRESS'""",
                (handle.run_id, handle.sequence),
            )
            connection.execute(
                "UPDATE runs SET next_sequence = ? WHERE run_id = ? AND next_sequence = ?",
                (handle.sequence, handle.run_id, handle.sequence + 1),
            )
            connection.commit()

    def _seal_run_sync(self, run_id: str, reason: str) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO runs (run_id, next_sequence, sealed, seal_reason, updated_at)
                   VALUES (?, 0, 1, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET sealed = 1,
                       seal_reason = excluded.seal_reason, updated_at = excluded.updated_at
                   WHERE runs.sealed = 0""",
                (run_id, reason, now),
            )
            connection.commit()

    def _recovery_status_sync(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM recovery WHERE singleton = 1"
            ).fetchone()
        required = bool(row["recovery_required"]) if row is not None else False
        return {
            "recovery_required": required,
            "incident_id": row["incident_id"] if required else None,
            "classification": row["classification"] if required else None,
        }

    def _acknowledge_sync(self, incident_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT recovery_required, incident_id FROM recovery WHERE singleton = 1"
            ).fetchone()
            if row is None or not row["recovery_required"]:
                raise DaemonError(
                    "target recovery is not required",
                    status_code=409,
                    code="recovery_not_required",
                )
            if not secrets.compare_digest(str(row["incident_id"]), incident_id):
                raise _recovery_incident_mismatch_error()
            connection.execute(
                """UPDATE recovery SET recovery_required = 0, incident_id = NULL,
                   classification = NULL, updated_at = ? WHERE singleton = 1""",
                (time.time(),),
            )
            connection.commit()
        return {"recovery_required": False, "acknowledged": True}

    def _acknowledge_volatile_sync(
        self,
        incident_id: str,
        run_id: str | None,
        classification: str,
    ) -> dict[str, Any]:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            interrupted_runs = {
                str(row["run_id"])
                for row in connection.execute(
                    "SELECT DISTINCT run_id FROM operations WHERE state = 'IN_PROGRESS'"
                ).fetchall()
            }
            if run_id is not None:
                interrupted_runs.add(run_id)
            connection.execute(
                """UPDATE operations
                   SET state = 'INDETERMINATE', classification = ?, incident_id = ?,
                       updated_at = ? WHERE state = 'IN_PROGRESS'""",
                (classification, incident_id, now),
            )
            for interrupted_run_id in interrupted_runs:
                connection.execute(
                    """UPDATE runs SET sealed = 1,
                       seal_reason = 'indeterminate', updated_at = ? WHERE run_id = ?""",
                    (now, interrupted_run_id),
                )
            connection.execute(
                """UPDATE recovery SET recovery_required = 0, incident_id = NULL,
                   classification = NULL, updated_at = ? WHERE singleton = 1""",
                (now,),
            )
            connection.commit()
        return {"recovery_required": False, "acknowledged": True}

    def _receipt_status_sync(self, run_id: str, sequence: int) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT sequence, operation_kind, state, classification, duration_ms,
                   incident_id FROM operations WHERE run_id = ? AND sequence = ?""",
                (run_id, sequence),
            ).fetchone()
        if row is None:
            return {"state": "MISSING", "run_id": run_id, "sequence": sequence}
        return {
            "state": row["state"],
            "run_id": run_id,
            "sequence": row["sequence"],
            "operation_kind": row["operation_kind"],
            "classification": row["classification"],
            "duration_ms": row["duration_ms"],
            "incident_id": row["incident_id"],
            "result_available": False,
        }

    def _resolve_sync(self, run_id: str, sequence: int) -> dict[str, Any]:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM operations WHERE run_id = ? AND sequence = ?",
                (run_id, sequence),
            ).fetchone()
            if row is not None:
                connection.commit()
                return self._receipt_status_sync(run_id, sequence)
            run = connection.execute(
                "SELECT next_sequence, sealed, seal_reason FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise DaemonError(
                    "trajectory run is sealed", status_code=409, code="run_sealed"
                )
            expected = int(run["next_sequence"])
            if run["sealed"]:
                if run["seal_reason"] == "resolved_missing" and sequence == expected:
                    connection.commit()
                    return _resolved_missing_status(run_id, sequence)
                raise DaemonError(
                    "trajectory run is sealed", status_code=409, code="run_sealed"
                )
            if sequence != expected:
                raise DaemonError(
                    "only the next missing operation can be resolved",
                    status_code=409,
                    code="operation_sequence_gap",
                    details={"expected_sequence": expected, "received_sequence": sequence},
                )
            connection.execute(
                """UPDATE runs SET sealed = 1, seal_reason = 'resolved_missing',
                   updated_at = ? WHERE run_id = ?""",
                (now, run_id),
            )
            connection.commit()
        return _resolved_missing_status(run_id, sequence)

    def _resolved_missing_proof_sync(
        self,
        run_id: str,
        sequence: int,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            run = connection.execute(
                "SELECT next_sequence, sealed, seal_reason FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if (
            run is None
            or not run["sealed"]
            or run["seal_reason"] != "resolved_missing"
            or int(run["next_sequence"]) != sequence
        ):
            return None
        return _resolved_missing_status(run_id, sequence)


def operation_sequence_from_headers(headers: _Headers) -> int | None:
    value = headers.get(OPERATION_SEQUENCE_HEADER)
    if value is None:
        return None
    return _parse_sequence(value)


def require_operation_sequence(value: Any) -> int:
    if value is None:
        raise DaemonError(
            "operation sequence is required while a lease is active",
            status_code=409,
            code="operation_sequence_required",
        )
    return _parse_sequence(value)


async def begin_mutation_receipt(
    state: Any,
    *,
    credentials: LeaseCredentials | None,
    sequence: Any,
    operation_kind: str,
    semantic_data: Any,
) -> ReceiptHandle | None:
    handle = await prepare_mutation_receipt(
        state,
        credentials=credentials,
        sequence=sequence,
        operation_kind=operation_kind,
        semantic_data=semantic_data,
    )
    require_new_receipt(handle)
    return handle


async def prepare_mutation_receipt(
    state: Any,
    *,
    credentials: LeaseCredentials | None,
    sequence: Any,
    operation_kind: str,
    semantic_data: Any,
) -> ReceiptHandle | None:
    await state.receipt_journal.ensure_mutation_allowed()
    async with state.lease_lock:
        lease: MutationLease | None = state.lease_coordinator.validate_mutation(credentials)
    if lease is None:
        return None
    resolved_sequence = require_operation_sequence(sequence)
    handle = await state.receipt_journal.begin(
        lease=lease,
        sequence=resolved_sequence,
        operation_kind=operation_kind,
        semantic_data=semantic_data,
    )
    return handle


def require_new_receipt(handle: ReceiptHandle | None) -> None:
    if handle is None:
        return
    if handle.existing_state == "COMPLETED":
        raise DaemonError(
            "durable operation receipt exists but its result is unavailable",
            status_code=409,
            code="operation_result_unavailable",
            details={"sequence": handle.sequence},
        )
    if handle.existing_state in {"IN_PROGRESS", "INDETERMINATE"}:
        raise _recovery_required_error(None)


async def finish_mutation_receipt(
    state: Any,
    handle: ReceiptHandle | None,
    exc: BaseException | None,
) -> None:
    if handle is None:
        return
    if exc is None:
        await state.receipt_journal.complete(handle)
        return
    if _is_retry_safe_before_dispatch(exc):
        await state.receipt_journal.abandon(handle, classification="not_started")
        return
    classification = _indeterminate_classification(exc)
    incident_id = await state.receipt_journal.mark_indeterminate(
        handle,
        classification=classification,
    )
    raise _recovery_required_error(incident_id) from exc


def _is_retry_safe_before_dispatch(exc: BaseException) -> bool:
    if not isinstance(exc, Exception):
        return False
    mapped = public_input_error(exc)
    error = mapped if mapped is not None else exc if isinstance(exc, DaemonError) else None
    return bool(
        isinstance(error, DaemonError)
        and error.details.get("retry_safe") is True
        and error.details.get("emission_state") == "not_started"
    )


def _indeterminate_classification(exc: BaseException) -> str:
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled_after_dispatch"
    if isinstance(exc, TimeoutError):
        return "timeout_after_dispatch"
    if isinstance(exc, Exception):
        mapped = public_input_error(exc)
        code = mapped.code if mapped is not None else getattr(exc, "code", None)
        if code == "input_may_be_partial":
            return "input_may_be_partial"
    return "dispatch_outcome_unknown"


def _parse_sequence(value: Any) -> int:
    if isinstance(value, bool):
        valid = False
    elif isinstance(value, int):
        valid = 0 <= value <= MAX_OPERATION_SEQUENCE
    elif isinstance(value, str):
        valid = value.isascii() and value.isdecimal()
        if valid:
            normalized = value.lstrip("0") or "0"
            maximum = str(MAX_OPERATION_SEQUENCE)
            valid = len(normalized) < len(maximum) or (
                len(normalized) == len(maximum) and normalized <= maximum
            )
            if valid:
                value = int(normalized)
    else:
        valid = False
    if not valid:
        raise DaemonError(
            f"operation sequence must be an integer from 0 to {MAX_OPERATION_SEQUENCE}",
            status_code=422,
            code="invalid_operation_sequence",
        )
    return int(value)


def _resolved_missing_status(run_id: str, sequence: int) -> dict[str, Any]:
    return {
        "state": "MISSING",
        "run_id": run_id,
        "sequence": sequence,
        "proven_not_applied": True,
        "run_sealed": True,
    }


def _recovery_incident_mismatch_error() -> DaemonError:
    return DaemonError(
        "recovery incident does not match",
        status_code=409,
        code="recovery_incident_mismatch",
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, bytes):
        return {"bytes_hmac_input": value.hex()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _recovery_required_error(incident_id: str | None) -> DaemonError:
    details = {"incident_id": incident_id} if incident_id else {}
    return DaemonError(
        "target recovery acknowledgment is required",
        status_code=409,
        code="recovery_required",
        details=details,
    )
