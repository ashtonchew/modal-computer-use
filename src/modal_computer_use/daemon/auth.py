from __future__ import annotations

import ipaddress
import json

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
        if self.settings.reject_query_tokens and "_modal_connect_token" in request.query_params:
            return JSONResponse(
                status_code=401,
                content={
                    "code": "query_token_rejected",
                    "message": "query-string tokens are disabled; use Authorization header",
                },
            )
        if request.url.path in {"/healthz", "/readyz"}:
            return await call_next(request)
        if self.settings.local_token:
            if not _is_loopback_request(request):
                return JSONResponse(
                    status_code=401,
                    content={
                        "code": "local_token_requires_loopback",
                        "message": "local bearer token mode is restricted to loopback clients",
                    },
                )
            expected = f"Bearer {self.settings.local_token}"
            if request.headers.get("authorization") != expected:
                return JSONResponse(
                    status_code=401,
                    content={"code": "unauthorized", "message": "invalid bearer token"},
                )
        elif self.settings.require_connect_user:
            verified_user_error = _verified_user_data_error(request)
            if verified_user_error is not None:
                return verified_user_error
        return await call_next(request)


def _is_loopback_request(request: Request) -> bool:
    host = request.client.host if request.client else ""
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _verified_user_data_error(request: Request) -> JSONResponse | None:
    if not _is_trusted_connect_proxy_request(request):
        return JSONResponse(
            status_code=401,
            content={
                "code": "connect_token_required",
                "message": "Modal connect token required",
            },
        )
    raw = request.headers.get("x-verified-user-data")
    if not raw:
        return JSONResponse(
            status_code=401,
            content={
                "code": "connect_token_required",
                "message": "Modal connect token required",
            },
        )
    try:
        metadata = json.loads(raw)
    except json.JSONDecodeError:
        return JSONResponse(
            status_code=401,
            content={
                "code": "invalid_verified_user_data",
                "message": "verified user metadata must be JSON",
            },
        )
    if not isinstance(metadata, dict) or metadata.get("sdk") != "modal-computer-use":
        return JSONResponse(
            status_code=401,
            content={
                "code": "invalid_verified_user_data",
                "message": "verified user metadata is not recognized",
            },
        )
    return None


def _is_trusted_connect_proxy_request(request: Request) -> bool:
    host = request.client.host if request.client else ""
    if host in {"localhost", "testclient"}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local
