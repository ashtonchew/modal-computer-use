from __future__ import annotations

from typing import Any

from modal_computer_use.operation_kinds import STABLE_OPERATION_KINDS


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


class BrowserReadinessError(ComputerUseError, RuntimeError):
    """Raised when the configured browser has not reached its required ready state."""


class FrameValidationError(ComputerUseError, ValueError):
    """Raised when an observed frame is empty, invalid, or incompatible."""


class DaemonHTTPError(ComputerUseError):
    """Raised when the daemon returns a non-successful HTTP response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details or {}
        self.retry_after_seconds = retry_after_seconds

    @property
    def retry_after_ms(self) -> int | None:
        value = self.details.get("retry_after_ms")
        return value if isinstance(value, int) and not isinstance(value, bool) else None


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


class SessionBorrowError(ComputerUseError):
    """Base class for safe session-borrow failures."""

    message = "the session could not be borrowed"

    def __init__(self, *_redacted: object, **_redacted_details: object) -> None:
        super().__init__(self.message)


class SessionCompatibilityError(SessionBorrowError):
    """Raised when a session does not support the requested handoff protocol."""

    message = "the session is not compatible with the requested handoff protocol"


class SessionEnvironmentMismatchError(SessionCompatibilityError):
    """Raised when the runtime environment does not match the session policy."""

    message = "the runtime environment does not match the session policy"


class SessionPlacementMismatchError(SessionCompatibilityError):
    """Raised when Function placement does not match the session policy."""

    message = "the Function placement does not match the session policy"


class SessionPlacementMissingError(SessionCompatibilityError):
    """Raised when required Function placement metadata is absent."""

    message = "required Function placement metadata is missing"


class SessionPlacementMalformedError(SessionCompatibilityError):
    """Raised when Function placement metadata cannot identify a region."""

    message = "Function placement metadata is malformed"


class SessionPlacementUnverifiableError(SessionCompatibilityError):
    """Raised when Function or target placement cannot be verified."""

    message = "Function or target placement could not be verified"


class SessionDaemonProtocolError(SessionCompatibilityError):
    """Raised when the daemon lacks the required trajectory protocol."""

    message = "the daemon does not support the required trajectory protocol"


class SessionTargetMismatchError(SessionBorrowError):
    """Raised when the live target does not match the session handle."""

    message = "the live target does not match the session handle"


class SessionBusyError(SessionBorrowError):
    """Raised when another run owns the session lease."""

    message = "the session is already borrowed"


class SessionLeaseLostError(SessionBorrowError):
    """Raised when a borrower no longer owns the session lease."""

    message = "the session lease was lost"


class RunSequenceConflictError(SessionBorrowError):
    """Raised when a run operation conflicts with the accepted sequence."""

    message = "the run operation sequence conflicts with session state"


class ActionOutcomeUnknownError(SessionBorrowError):
    """Raised when an action may have executed but its outcome is unknown."""

    message = "the action outcome is unknown"


class OperationResultUnavailableError(SessionBorrowError):
    """Raised when a retained operation result is no longer available."""

    message = "the operation result is unavailable"

    def __init__(
        self,
        *,
        sequence: int,
        operation_kind: str | None,
    ) -> None:
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("sequence must be a nonnegative integer")
        if operation_kind is not None and (
            not isinstance(operation_kind, str)
            or operation_kind not in STABLE_OPERATION_KINDS
        ):
            raise ValueError("operation_kind must be a stable daemon operation kind or None")
        super().__init__()
        self.sequence = sequence
        self.operation_kind = operation_kind


class OperationNotAppliedError(SessionBorrowError):
    """Raised when durable receipt resolution proves an operation did not run."""

    message = "the operation was not applied"


class SessionRecoveryRequiredError(SessionBorrowError):
    """Raised when explicit recovery is required before further operations."""

    message = "the session requires recovery before further operations"
