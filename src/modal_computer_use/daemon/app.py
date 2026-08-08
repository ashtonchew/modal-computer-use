from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from modal_computer_use import __version__
from modal_computer_use.artifacts import ArtifactStore
from modal_computer_use.daemon.auth import AuthMiddleware
from modal_computer_use.daemon.budget_policy import BudgetPolicy
from modal_computer_use.daemon.desktop import choose_backend
from modal_computer_use.daemon.desktop.recordings import RecordingRegistry
from modal_computer_use.daemon.desktop.xtest import (
    X11InputInjectionError,
    X11InputStateConflictError,
    X11InputUnavailableError,
)
from modal_computer_use.daemon.errors import DaemonError, public_input_error
from modal_computer_use.daemon.leases import LeaseCoordinator
from modal_computer_use.daemon.logging import configure_logging
from modal_computer_use.daemon.readiness import ReadinessCache
from modal_computer_use.daemon.receipts import ReceiptJournal
from modal_computer_use.daemon.request_limits import (
    RequestBodyLimitMiddleware,
    RequestBodyTooLarge,
)
from modal_computer_use.daemon.settings import DaemonSettings, get_settings
from modal_computer_use.daemon.supervisor import Supervisor
from modal_computer_use.daemon.tunnel_sessions import TunnelSessionStore
from modal_computer_use.daemon.websocket_admission import WebSocketAdmission
from modal_computer_use.errors import ArtifactPathError, BudgetExceededError
from modal_computer_use.models import ActionResult
from modal_computer_use.observability import get_tracer
from modal_computer_use.redaction import safe_exception_payload, sanitize_payload, sanitize_text

from .routes import (
    actions,
    apps,
    artifacts,
    browser,
    clipboard,
    commands,
    debug,
    display,
    health,
    hot_session,
    input,
    keyboard,
    leases,
    lifecycle,
    mouse,
    observations,
    processes,
    recordings,
    recovery,
    screenshots,
    session,
    steps,
    windows,
)

logger = logging.getLogger("modal_computer_use.daemon.app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    supervisor: Supervisor = app.state.supervisor
    await app.state.receipt_journal.start()
    await supervisor.start()
    app.state.browser_prewarm = None
    startup_url = app.state.settings.browser_open_url_on_start
    recovery_status = await app.state.receipt_journal.recovery_status()
    if recovery_status["recovery_required"]:
        app.state.browser_prewarm = ActionResult(
            ok=False,
            message="browser startup skipped while target recovery is required",
            output={"code": "recovery_required"},
        )
    elif startup_url:
        try:
            app.state.browser_prewarm = await app.state.backend.open_url(
                startup_url,
                wait_for_window=True,
            )
        except Exception as exc:
            logger.warning("browser startup url failed", exc_info=True)
            app.state.browser_prewarm = ActionResult(
                ok=False,
                message="browser startup url failed",
                output={"error": type(exc).__name__},
            )
    elif app.state.settings.browser_prewarm:
        try:
            app.state.browser_prewarm = await app.state.backend.prewarm_browser()
        except Exception as exc:
            logger.warning("browser prewarm failed", exc_info=True)
            app.state.browser_prewarm = ActionResult(
                ok=False,
                message="browser prewarm failed",
                output={"error": type(exc).__name__},
            )
    try:
        yield
    finally:
        try:
            lease_expiry_task = app.state.lease_expiry_task
            if lease_expiry_task is not None:
                lease_expiry_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await lease_expiry_task
            try:
                await asyncio.to_thread(app.state.recordings.shutdown)
            finally:
                await supervisor.stop()
        finally:
            try:
                app.state.backend.close()
            finally:
                await app.state.receipt_journal.close()


def create_app(settings: DaemonSettings | None = None) -> FastAPI:
    configure_logging()
    settings = settings or get_settings()
    app = FastAPI(
        title="modal-computer-use daemon",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.supervisor = Supervisor(settings)
    app.state.backend = choose_backend(
        settings.backend,
        width=settings.desktop_width,
        height=settings.desktop_height,
        display=settings.display,
        browser=settings.browser,
        browser_profile_dir=settings.browser_profile_dir,
        browser_launch_args=settings.browser_launch_args,
        browser_gpu_mode=settings.browser_gpu_mode,
        input_backend=settings.input_backend,
        subprocess_backend=settings.subprocess_backend,
    )
    app.state.input_lock = asyncio.Lock()
    app.state.lease_lock = asyncio.Lock()
    app.state.lease_coordinator = LeaseCoordinator()
    app.state.lease_expiry_task = None
    app.state.receipt_journal = ReceiptJournal(settings.runtime_dir)
    app.state.readiness_cache = ReadinessCache(settings.readiness_cache_ttl_ms)
    app.state.artifacts = ArtifactStore(
        settings.artifacts_dir,
        persistent=settings.artifacts_persistent,
        persistent_verified=settings.artifacts_volume_mounted,
        max_total_bytes=settings.max_artifact_bytes,
    )
    app.state.recordings = RecordingRegistry(settings, artifact_store=app.state.artifacts)
    app.state.idempotency_cache = OrderedDict()
    app.state.tunnel_sessions = TunnelSessionStore(max_sessions=settings.max_tunnel_sessions)
    app.state.websocket_admission = WebSocketAdmission(
        hot_limit=settings.max_hot_session_connections,
        observation_limit=settings.max_observation_connections,
    )
    app.state.action_count = 0
    app.state.screenshot_count = 0
    app.state.last_activity_at = time.monotonic()
    app.state.action_rate_window = deque()
    app.state.budget_policy = BudgetPolicy(app.state)
    app.state.tracer = get_tracer(
        enabled=settings.otel_enabled,
        name="modal_computer_use.daemon",
    )
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=settings.max_json_body_bytes)
    app.add_middleware(AuthMiddleware, settings=settings)

    @app.middleware("http")
    async def trace_route(request: Request, call_next):
        with app.state.tracer.span(
            "daemon.route",
            {
                "http.method": request.method,
            },
        ) as span:
            try:
                response = await call_next(request)
            except Exception as exc:
                span.set_attribute("http.route", _route_template(request))
                span.record_exception(exc)
                raise
            span.set_attribute("http.route", _route_template(request))
            span.set_attribute("http.status_code", response.status_code)
            error_code = response.headers.get("x-computer-use-error-code")
            if error_code:
                span.set_attribute("error.code", error_code)
            return response

    @app.exception_handler(DaemonError)
    async def daemon_error_handler(_request: Request, exc: DaemonError) -> JSONResponse:
        details = sanitize_payload(exc.details)
        if isinstance(details, dict) and isinstance(exc.details.get("budgets"), dict):
            details["budgets"] = exc.details["budgets"]
        return _error_response(
            status_code=exc.status_code,
            code=exc.code,
            content={
                "code": exc.code,
                "message": sanitize_text(exc.message),
                "details": details,
            },
        )

    @app.exception_handler(X11InputUnavailableError)
    @app.exception_handler(X11InputInjectionError)
    @app.exception_handler(X11InputStateConflictError)
    async def native_input_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        mapped_error = public_input_error(exc)
        if mapped_error is None:  # pragma: no cover - decorators constrain the exception types.
            raise exc
        return _error_response(
            status_code=mapped_error.status_code,
            code=mapped_error.code,
            content={
                "code": mapped_error.code,
                "message": mapped_error.message,
                "details": mapped_error.details,
            },
        )

    @app.exception_handler(ArtifactPathError)
    async def artifact_path_error_handler(
        _request: Request, exc: ArtifactPathError
    ) -> JSONResponse:
        return _error_response(
            status_code=400,
            code="unsafe_artifact_path",
            content={
                "code": "unsafe_artifact_path",
                "message": sanitize_text(str(exc)),
                "details": {},
            },
        )

    @app.exception_handler(BudgetExceededError)
    async def budget_exceeded_error_handler(
        _request: Request, exc: BudgetExceededError
    ) -> JSONResponse:
        return _error_response(
            status_code=429,
            code="budget_exceeded",
            content={
                "code": "budget_exceeded",
                "message": sanitize_text(str(exc)),
                "details": {},
            },
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        code = (
            "action_validation_failed"
            if _is_action_payload_validation_error(request, exc)
            else "validation_error"
        )
        message = (
            "action validation failed"
            if code == "action_validation_failed"
            else "request validation failed"
        )
        return _error_response(
            status_code=422,
            code=code,
            content={
                "code": code,
                "message": message,
                "details": {"errors": _validation_errors_without_inputs(exc)},
            },
        )

    @app.exception_handler(RequestBodyTooLarge)
    async def request_body_too_large_handler(
        _request: Request, exc: RequestBodyTooLarge
    ) -> JSONResponse:
        return _error_response(
            status_code=413,
            code="request_body_too_large",
            content={
                "code": "request_body_too_large",
                "message": "request body exceeds the configured byte limit",
                "details": {"max_bytes": exc.max_bytes},
            },
        )

    @app.exception_handler(FileNotFoundError)
    async def not_found_handler(_request: Request, _exc: FileNotFoundError) -> JSONResponse:
        return _error_response(
            status_code=404,
            code="not_found",
            content={"code": "not_found", "message": "resource not found", "details": {}},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        mapped_error = public_input_error(exc)
        if mapped_error is not None:
            return _error_response(
                status_code=mapped_error.status_code,
                code=mapped_error.code,
                content={
                    "code": mapped_error.code,
                    "message": mapped_error.message,
                    "details": mapped_error.details,
                },
            )
        return _error_response(
            status_code=500,
            code="internal_error",
            content={
                "code": "internal_error",
                "message": "internal server error",
                "details": safe_exception_payload(exc),
            },
        )

    for router in (
        health.router,
        leases.router,
        recovery.router,
        lifecycle.router,
        processes.router,
        mouse.router,
        observations.router,
        keyboard.router,
        clipboard.router,
        screenshots.router,
        recordings.router,
        display.router,
        windows.router,
        actions.router,
        steps.router,
        artifacts.router,
        browser.router,
        apps.router,
        input.router,
        commands.router,
        debug.router,
        session.router,
        hot_session.router,
        recordings.dashboard_router,
    ):
        app.include_router(router)
    return app


def _validation_errors_without_inputs(exc: RequestValidationError) -> list[dict[str, object]]:
    errors: list[dict[str, object]] = []
    for item in exc.errors():
        errors.append(
            {
                "loc": item.get("loc", ()),
                "msg": item.get("msg", "validation error"),
                "type": item.get("type", "value_error"),
            }
        )
    return errors


def _is_action_payload_validation_error(request: Request, exc: RequestValidationError) -> bool:
    if request.scope.get("path") not in {
        "/v1/actions/run",
        "/v1/actions/validate",
        "/v1/steps",
    }:
        return False
    for item in exc.errors():
        loc = item.get("loc", ())
        if len(loc) >= 2 and loc[0] == "body" and loc[1] == "actions":
            return True
    return False


def _error_response(*, status_code: int, code: str, content: dict[str, object]) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers={"x-computer-use-error-code": code, "cache-control": "no-store"},
    )


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"
