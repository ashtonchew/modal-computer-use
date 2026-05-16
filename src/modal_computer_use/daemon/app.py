from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from modal_computer_use.artifacts import ArtifactStore
from modal_computer_use.daemon.auth import AuthMiddleware
from modal_computer_use.daemon.budget_policy import BudgetPolicy
from modal_computer_use.daemon.desktop import choose_backend
from modal_computer_use.daemon.desktop.recordings import RecordingRegistry
from modal_computer_use.daemon.errors import DaemonError
from modal_computer_use.daemon.logging import configure_logging
from modal_computer_use.daemon.settings import DaemonSettings, get_settings
from modal_computer_use.daemon.supervisor import Supervisor
from modal_computer_use.errors import ArtifactPathError, BudgetExceededError
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
    input,
    keyboard,
    lifecycle,
    mouse,
    processes,
    recordings,
    screenshots,
    session,
    windows,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    supervisor: Supervisor = app.state.supervisor
    await supervisor.start()
    yield
    await supervisor.stop()


def create_app(settings: DaemonSettings | None = None) -> FastAPI:
    configure_logging()
    settings = settings or get_settings()
    app = FastAPI(title="modal-computer-use daemon", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.supervisor = Supervisor(settings)
    app.state.backend = choose_backend(
        settings.backend,
        width=settings.desktop_width,
        height=settings.desktop_height,
        display=settings.display,
        browser=settings.browser,
    )
    app.state.input_lock = asyncio.Lock()
    app.state.artifacts = ArtifactStore(
        settings.artifacts_dir,
        persistent=settings.artifacts_persistent,
        persistent_verified=settings.artifacts_volume_mounted,
        max_total_bytes=settings.max_artifact_bytes,
    )
    app.state.recordings = RecordingRegistry(settings, artifact_store=app.state.artifacts)
    app.state.idempotency_cache = OrderedDict()
    app.state.action_count = 0
    app.state.screenshot_count = 0
    app.state.last_activity_at = time.monotonic()
    app.state.action_rate_window = deque()
    app.state.budget_policy = BudgetPolicy(app.state)
    app.state.tracer = get_tracer(
        enabled=settings.otel_enabled,
        name="modal_computer_use.daemon",
    )
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

    @app.exception_handler(FileNotFoundError)
    async def not_found_handler(_request: Request, _exc: FileNotFoundError) -> JSONResponse:
        return _error_response(
            status_code=404,
            code="not_found",
            content={"code": "not_found", "message": "resource not found", "details": {}},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
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
        lifecycle.router,
        processes.router,
        mouse.router,
        keyboard.router,
        clipboard.router,
        screenshots.router,
        recordings.router,
        display.router,
        windows.router,
        actions.router,
        artifacts.router,
        browser.router,
        apps.router,
        input.router,
        commands.router,
        debug.router,
        session.router,
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
    if request.url.path not in {"/v1/actions/run", "/v1/actions/validate"}:
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
        headers={"x-computer-use-error-code": code},
    )


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"
