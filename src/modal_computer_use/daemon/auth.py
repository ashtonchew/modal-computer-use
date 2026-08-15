from __future__ import annotations

import ipaddress
import json
import secrets

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from .errors import DaemonError
from .settings import DaemonSettings

OWNER_PROOF_HEADER = "x-computer-use-owner-proof"


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, settings: DaemonSettings) -> None:
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if self.settings.reject_query_tokens and "_modal_connect_token" in request.query_params:
            return _no_store(JSONResponse(
                status_code=401,
                headers={"x-computer-use-error-code": "query_token_rejected"},
                content={
                    "code": "query_token_rejected",
                    "message": "query-string tokens are disabled; use Authorization header",
                },
            ))
        if request.scope.get("path") in {"/healthz", "/readyz"}:
            return _no_store(await call_next(request))
        tunnel_auth_kind = _tunnel_auth_kind(request)
        if tunnel_auth_kind is not None:
            request.state.auth_kind = tunnel_auth_kind
            return _no_store(await call_next(request))
        if self.settings.local_token:
            if not _is_loopback_request(request):
                return _no_store(JSONResponse(
                    status_code=401,
                    headers={"x-computer-use-error-code": "local_token_requires_loopback"},
                    content={
                        "code": "local_token_requires_loopback",
                        "message": "local bearer token mode is restricted to loopback clients",
                    },
                ))
            expected = f"Bearer {self.settings.local_token}"
            if not secrets.compare_digest(request.headers.get("authorization", ""), expected):
                return _no_store(JSONResponse(
                    status_code=401,
                    headers={"x-computer-use-error-code": "unauthorized"},
                    content={"code": "unauthorized", "message": "invalid bearer token"},
                ))
            request.state.auth_kind = "local"
        elif self.settings.require_connect_user:
            verified_user_error = _verified_user_data_error(request)
            if verified_user_error is not None:
                return _no_store(verified_user_error)
            request.state.auth_kind = "connect"
        elif self.settings.tunnel_token:
            return _no_store(
                JSONResponse(
                    status_code=401,
                    headers={"x-computer-use-error-code": "unauthorized"},
                    content={"code": "unauthorized", "message": "invalid bearer token"},
                )
            )
        elif self.settings.allow_unauthenticated_loopback:
            if not _is_loopback_request(request):
                return _no_store(
                    JSONResponse(
                        status_code=401,
                        headers={"x-computer-use-error-code": "loopback_required"},
                        content={
                            "code": "loopback_required",
                            "message": "unauthenticated mode is restricted to loopback clients",
                        },
                    )
                )
            request.state.auth_kind = "unauthenticated_loopback"
        else:
            return _no_store(
                JSONResponse(
                    status_code=401,
                    headers={"x-computer-use-error-code": "authentication_required"},
                    content={
                        "code": "authentication_required",
                        "message": "daemon authentication is not configured",
                    },
                )
            )
        return _no_store(await call_next(request))


def _is_loopback_request(request: Request) -> bool:
    host = request.client.host if request.client else ""
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _verified_user_data_error(request: Request) -> JSONResponse | None:
    if not _is_trusted_connect_proxy_request(
        request,
        trust_private=request.app.state.settings.trust_private_connect_proxy,
    ):
        return JSONResponse(
            status_code=401,
            headers={"x-computer-use-error-code": "connect_token_required"},
            content={
                "code": "connect_token_required",
                "message": "Modal connect token required",
            },
        )
    raw = request.headers.get("x-verified-user-data")
    if not raw:
        return JSONResponse(
            status_code=401,
            headers={"x-computer-use-error-code": "connect_token_required"},
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
            headers={"x-computer-use-error-code": "invalid_verified_user_data"},
            content={
                "code": "invalid_verified_user_data",
                "message": "verified user metadata must be JSON",
            },
        )
    if not isinstance(metadata, dict) or metadata.get("sdk") != "modal-computer-use":
        return JSONResponse(
            status_code=401,
            headers={"x-computer-use-error-code": "invalid_verified_user_data"},
            content={
                "code": "invalid_verified_user_data",
                "message": "verified user metadata is not recognized",
            },
        )
    return None


def _tunnel_auth_kind(request: Request) -> str | None:
    token = _bearer_token(request)
    if not token:
        return None
    settings = request.app.state.settings
    if settings.tunnel_token and secrets.compare_digest(token, settings.tunnel_token):
        return "bootstrap_tunnel"
    sessions = request.app.state.tunnel_sessions
    return "minted_tunnel" if sessions.validate(token) else None


def _bearer_token(request: Request) -> str | None:
    value = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not value.startswith(prefix):
        return None
    token = value[len(prefix) :].strip()
    return token or None


def require_privileged_auth(request: Request) -> None:
    if getattr(request.state, "auth_kind", None) == "minted_tunnel":
        raise DaemonError(
            "minted tunnel sessions cannot access privileged process routes",
            status_code=403,
            code="privileged_auth_required",
        )


def require_owner_auth(request: Request) -> None:
    """Require the owner capability for lifecycle/display-stack mutations.

    Local and static-bootstrap authentication already proves ownership: the
    middleware accepted the configured owner bearer using a constant-time
    comparison. Connect and minted tunnel requests must carry the original
    static owner capability in ``X-Computer-Use-Owner-Proof``. A valid proof
    is accepted regardless of the transport auth kind; possession of that
    secret is the authority boundary.
    """

    auth_kind = getattr(request.state, "auth_kind", None)
    if auth_kind in {"local", "bootstrap_tunnel"}:
        provided = request.headers.get(OWNER_PROOF_HEADER)
        if provided is None or _owner_proof_matches(request, provided):
            return
    elif has_owner_proof(request):
        return
    raise DaemonError(
        "owner authorization is required",
        status_code=403,
        code="owner_authorization_required",
    )


def has_owner_proof(request: Request) -> bool:
    """Return whether the request carries a configured owner proof."""

    provided = request.headers.get(OWNER_PROOF_HEADER)
    return isinstance(provided, str) and bool(provided) and _owner_proof_matches(request, provided)


def _owner_proof_matches(request: Request, provided: str) -> bool:
    settings = request.app.state.settings
    matched = False
    # Evaluate every configured candidate. ``or`` would short-circuit after a
    # match and make the selected secret observable through timing.
    for expected in (settings.tunnel_token, settings.local_token):
        if isinstance(expected, str) and expected:
            matched = secrets.compare_digest(expected, provided) | matched
    return matched


def _is_trusted_connect_proxy_request(request: Request, *, trust_private: bool = False) -> bool:
    host = request.client.host if request.client else ""
    if host == "testclient":
        return True
    if host == "localhost":
        return trust_private
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return trust_private
    return trust_private and (address.is_private or address.is_link_local)


def _no_store(response: Response) -> Response:
    response.headers.setdefault("cache-control", "no-store")
    return response
