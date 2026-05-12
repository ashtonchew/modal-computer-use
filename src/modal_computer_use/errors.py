from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ComputerUseError(Exception):
    """Base exception for modal-computer-use."""


class ConfigurationError(ComputerUseError):
    """Raised when configuration is invalid or unsupported."""


class ConfigConflictError(ConfigurationError):
    """Raised when an existing sandbox does not match the requested config."""

    def __init__(
        self,
        message: str,
        *,
        requested_hash: str | None = None,
        existing_hash: str | None = None,
        sandbox_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.requested_hash = requested_hash
        self.existing_hash = existing_hash
        self.sandbox_id = sandbox_id


class AuthenticationError(ComputerUseError):
    """Raised when daemon authentication fails."""


class DaemonHTTPError(ComputerUseError):
    """Raised when the daemon returns a non-successful HTTP response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details or {}


class UnsupportedActionError(ComputerUseError):
    """Raised when an adapter or executor receives an unknown action."""


class ActionValidationError(ComputerUseError):
    """Raised when an action cannot be normalized or validated."""


class ArtifactPathError(ComputerUseError, ValueError):
    """Raised when an artifact path escapes the allowed root or is unsafe."""


class BudgetExceededError(ComputerUseError):
    """Raised when a run budget is exhausted."""


class ModalNotInstalledError(ComputerUseError):
    """Raised when Modal-specific APIs are requested without the modal extra."""


class SandboxUnavailableError(ComputerUseError):
    """Raised when a sandbox cannot be found or contacted."""


class SandboxAmbiguousError(SandboxUnavailableError):
    """Raised when an attach query matches multiple sandboxes."""


class ProcessExecutionError(ComputerUseError):
    """Raised when a desktop subprocess command fails."""


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    message: str
    details: dict[str, Any] | None = None
