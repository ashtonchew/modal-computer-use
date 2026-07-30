"""Modal 1.5.2 Function adapter and hosted App wiring."""

from __future__ import annotations

import os
from typing import Any

from .domain import (
    FunctionCallIdentity,
    PollOutcome,
    PollReason,
    PollState,
    ResolvedDesktop,
    ResolvedTask,
    TrajectoryOutcome,
    TrajectoryStatus,
)
from .http import build_run_gateway_app
from .service import RunGatewayService

try:
    import modal
except ImportError:
    modal = None

_DEFAULT_MODAL = object()


class ModalTrajectoryDispatcher:
    """Modal 1.5.2 adapter for one application-owned deployed Function."""

    def __init__(self, function: object, *, modal_runtime: Any = _DEFAULT_MODAL) -> None:
        if function is None:
            raise ValueError("a deployed trajectory Function is required")
        self._function = function
        self._modal = modal if modal_runtime is _DEFAULT_MODAL else modal_runtime

    @classmethod
    def from_name(
        cls,
        app_name: str,
        function_name: str,
        *,
        modal_runtime: Any = _DEFAULT_MODAL,
    ) -> ModalTrajectoryDispatcher:
        runtime = modal if modal_runtime is _DEFAULT_MODAL else modal_runtime
        if runtime is None:
            raise ImportError("Modal is required for hosted trajectory dispatch")
        return cls(
            runtime.Function.from_name(app_name, function_name),
            modal_runtime=runtime,
        )

    async def spawn(
        self,
        *,
        desktop: ResolvedDesktop,
        task: ResolvedTask,
        run_id: str,
    ) -> FunctionCallIdentity:
        call = await self._function.spawn.aio(desktop.handle, task.text, run_id)
        return FunctionCallIdentity(call.object_id)

    async def poll(self, call_id: FunctionCallIdentity) -> PollOutcome:
        runtime = self._modal
        if runtime is None:
            raise ImportError("Modal is required for hosted trajectory polling")
        exceptions = runtime.exception
        try:
            call = runtime.FunctionCall.from_id(call_id.reveal_to_backend())
            roots = await call.get_call_graph.aio()
            status = (
                getattr(roots[0], "status", None)
                if isinstance(roots, list) and len(roots) == 1
                else None
            )
            status_name = getattr(status, "name", None)
            if status_name == "PENDING":
                return PollOutcome(PollState.PENDING)
            if status_name in {"FAILURE", "INIT_FAILURE"}:
                return PollOutcome(PollState.FAILED)
            if status_name == "TIMEOUT":
                return PollOutcome(PollState.FAILED, PollReason.FUNCTION_TIMEOUT)
            if status_name == "TERMINATED":
                return PollOutcome(PollState.TERMINATED)
            # Call-graph metadata is best-effort. SUCCESS and incomplete or
            # malformed graph data converge on one authoritative result poll.
            return await _poll_result(call)
        except exceptions.InputCancellation:
            return PollOutcome(PollState.TERMINATED)
        except exceptions.OutputExpiredError:
            return PollOutcome(PollState.INDETERMINATE, PollReason.OUTPUT_EXPIRED)
        except exceptions.FunctionTimeoutError:
            return PollOutcome(PollState.FAILED, PollReason.FUNCTION_TIMEOUT)
        except TimeoutError:
            return PollOutcome(PollState.PENDING)
        except exceptions.NotFoundError:
            return PollOutcome(PollState.INDETERMINATE, PollReason.MISSING_CALL)
        except (
            exceptions.DeserializationError,
            exceptions.ExecutionError,
            exceptions.DataLossError,
        ):
            return PollOutcome(PollState.INDETERMINATE, PollReason.RESULT_DATA_LOSS)
        except (
            exceptions.ConnectionError,
            exceptions.ServiceError,
            exceptions.AuthError,
            exceptions.ResourceExhaustedError,
            exceptions.InternalFailure,
        ):
            return PollOutcome(PollState.UNAVAILABLE, PollReason.TRANSIENT_PROVIDER_ERROR)
        except exceptions.Error:
            return PollOutcome(PollState.INDETERMINATE)

    async def cancel(self, call_id: FunctionCallIdentity) -> None:
        runtime = self._modal
        if runtime is None:
            raise ImportError("Modal is required for hosted trajectory cancellation")
        call = runtime.FunctionCall.from_id(call_id.reveal_to_backend())
        await call.cancel.aio(terminate_containers=False)


async def _poll_result(call: Any) -> PollOutcome:
    raw_outcome = await call.get.aio(timeout=0)
    try:
        outcome = TrajectoryOutcome.validate(raw_outcome)
    except ValueError:
        return PollOutcome(PollState.INDETERMINATE, PollReason.INVALID_OUTCOME)
    return PollOutcome(PollState(outcome.status.value))


def build_default_service() -> RunGatewayService:
    raise RuntimeError(
        "inject the application's PrincipalResolver, SessionCatalog, TaskCatalog, "
        "durable RunStore, and TrajectoryDispatcher before deploying this example"
    )


if modal is None:
    app = None
else:
    app = modal.App("application-owned-run-gateway")
    _image = modal.Image.debian_slim().pip_install("modal-computer-use[modal]")

    @app.cls(
        image=_image,
        min_containers=int(os.environ.get("RUN_GATEWAY_MIN_CONTAINERS", "0")),
        scaledown_window=int(os.environ.get("RUN_GATEWAY_SCALEDOWN_WINDOW", "300")),
    )
    @modal.concurrent(max_inputs=100, target_inputs=80)
    class RunGateway:
        @modal.enter()
        def setup(self) -> None:
            self.service = build_default_service()

        @modal.asgi_app(requires_proxy_auth=True)
        def web(self):
            return build_run_gateway_app(self.service)


def build_modal_app() -> object:
    if app is None:
        raise ImportError("Modal is required to build the hosted run gateway")
    return app


__all__ = [
    "ModalTrajectoryDispatcher",
    "TrajectoryOutcome",
    "TrajectoryStatus",
    "app",
    "build_default_service",
    "build_modal_app",
    "modal",
]
