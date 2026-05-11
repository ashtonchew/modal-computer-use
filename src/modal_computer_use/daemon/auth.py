from __future__ import annotations

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from .settings import DaemonSettings


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, settings: DaemonSettings) -> None:
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in {"/healthz", "/readyz"}:
            return await call_next(request)
        if self.settings.reject_query_tokens and "_modal_connect_token" in request.query_params:
            return JSONResponse(
                status_code=401,
                content={
                    "code": "query_token_rejected",
                    "message": "query-string tokens are disabled; use Authorization header",
                },
            )
        if self.settings.local_token:
            expected = f"Bearer {self.settings.local_token}"
            if request.headers.get("authorization") != expected:
                return JSONResponse(
                    status_code=401,
                    content={"code": "unauthorized", "message": "invalid bearer token"},
                )
        elif self.settings.require_connect_user and not request.headers.get("x-verified-user-data"):
            return JSONResponse(
                status_code=401,
                content={
                    "code": "connect_token_required",
                    "message": "Modal connect token required",
                },
            )
        return await call_next(request)
