from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from modal_computer_use.daemon.desktop.xtest import (
    X11InputInjectionError,
    X11InputReleaseError,
    X11InputStateConflictError,
    X11InputUnavailableError,
)
from modal_computer_use.redaction import sanitize_payload, sanitize_text


class DaemonError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        code: str = "daemon_error",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.details = details or {}


def public_input_error(exc: Exception) -> DaemonError | None:
    """Map native input failures to the stable daemon error contract."""
    if isinstance(exc, X11InputStateConflictError):
        return DaemonError(
            "input target is already held",
            status_code=409,
            code="input_state_conflict",
            details={
                "retry_safe": True,
                "emission_state": "not_started",
            },
        )
    if isinstance(exc, X11InputUnavailableError):
        return DaemonError(
            "native input backend is unavailable before input emission",
            status_code=503,
            code="input_backend_unavailable",
            details={
                "input_backend": exc.input_backend,
                "retry_safe": True,
                "emission_state": "not_started",
            },
        )
    if isinstance(exc, X11InputReleaseError):
        return DaemonError(
            "input release may have been partially applied",
            status_code=500,
            code="input_may_be_partial",
            details={
                "input_backend": exc.input_backend,
                "retry_safe": True,
                "emission_state": "possibly_partial",
            },
        )
    if isinstance(exc, X11InputInjectionError):
        return DaemonError(
            "input may have been partially applied",
            status_code=500,
            code="input_may_be_partial",
            details={
                "input_backend": exc.input_backend,
                "retry_safe": False,
                "emission_state": "possibly_partial",
            },
        )
    return None


def to_http_exception(error: DaemonError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={
            "code": error.code,
            "message": sanitize_text(error.message),
            "details": sanitize_payload(error.details),
        },
    )
