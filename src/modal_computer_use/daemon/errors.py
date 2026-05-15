from __future__ import annotations

from typing import Any

from fastapi import HTTPException

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


def to_http_exception(error: DaemonError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={
            "code": error.code,
            "message": sanitize_text(error.message),
            "details": sanitize_payload(error.details),
        },
    )
