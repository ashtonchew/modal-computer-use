"""Privileged single-trust-domain Modal ASGI session broker example.

The broker is a control plane: it creates, lists, inspects, and terminates
desktop sandboxes. It deliberately does not proxy screenshots or input actions;
callers use the returned daemon endpoint directly for the hot path.

This admin example accepts caller-selected owner labels and raw Sandbox IDs. It
does not perform application authentication or object-level tenant authorization
and must not be exposed as a multi-tenant service. Use ``modal_run_gateway.py``
as the reference boundary for application-owned hosted trajectory admission.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from modal_computer_use import (
    BrowserConfig,
    ComputerConfig,
    ComputerSandbox,
    ComputerSandboxManager,
    ResourceConfig,
)
from modal_computer_use.errors import SandboxUnavailableError


class CreateSessionRequest(BaseModel):
    run_id: str | None = None
    name: str | None = None
    owner: str | None = None
    modal_region: str | None = None
    ingress: Literal["connect", "tunnel", "attested-tunnel"] = "attested-tunnel"
    resource_profile: str | None = None
    browser: Literal["firefox", "chromium"] | None = None
    browser_prewarm: bool = True
    wait: bool = True
    include_daemon_token: bool = Field(
        default=False,
        description="Return the daemon bearer token in the response body. Treat it as a secret.",
    )


class SessionResponse(BaseModel):
    sandbox_id: str
    app_name: str
    name: str | None = None
    run_id: str | None = None
    owner: str | None = None
    created_at: str | None = None
    modal_region: str | None = None
    daemon_base_url: str | None = None
    daemon_token: str | None = None
    status: str = "unknown"


class SessionListResponse(BaseModel):
    sessions: list[SessionResponse]


@dataclass
class SessionBrokerService:
    manager: ComputerSandboxManager

    def create_session(self, request: CreateSessionRequest) -> SessionResponse:
        computer = self.manager.create(
            config=_session_config(request),
            name=request.name,
            owner=request.owner,
            wait=request.wait,
        )
        try:
            return _session_response_from_computer(
                computer,
                modal_region=request.modal_region,
                include_daemon_token=request.include_daemon_token,
            )
        finally:
            computer.detach()

    def get_session(self, sandbox_id: str, *, include_daemon_token: bool) -> SessionResponse:
        computer = self.manager.attach(sandbox_id=sandbox_id)
        try:
            return _session_response_from_computer(
                computer,
                modal_region=None,
                include_daemon_token=include_daemon_token,
            )
        finally:
            computer.detach()

    def list_sessions(self, *, owner: str | None = None) -> list[SessionResponse]:
        return [
            SessionResponse(
                sandbox_id=ref.sandbox_id,
                app_name=ref.app_name,
                name=ref.name,
                run_id=ref.run_id,
                owner=ref.owner,
                created_at=None if ref.created_at is None else ref.created_at.isoformat(),
                status=ref.status,
            )
            for ref in self.manager.list(owner=owner)
        ]

    def terminate_session(self, sandbox_id: str) -> None:
        self.manager.terminate(sandbox_id)


def build_session_broker_app(service: SessionBrokerService) -> FastAPI:
    app = FastAPI(
        title="privileged single-trust-domain modal-computer-use session broker",
        version="1.0.0",
        description="Administrative example; not a tenant authorization boundary.",
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/sessions", response_model=SessionResponse)
    def create_session(request: CreateSessionRequest) -> SessionResponse:
        return service.create_session(request)

    @app.get("/sessions", response_model=SessionListResponse)
    def list_sessions(owner: str | None = None) -> SessionListResponse:
        return SessionListResponse(sessions=service.list_sessions(owner=owner))

    @app.get("/sessions/{sandbox_id}", response_model=SessionResponse)
    def get_session(
        sandbox_id: str,
        include_daemon_token: bool = Query(False),
    ) -> SessionResponse:
        try:
            return service.get_session(
                sandbox_id,
                include_daemon_token=include_daemon_token,
            )
        except SandboxUnavailableError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/sessions/{sandbox_id}", status_code=204)
    def terminate_session(sandbox_id: str) -> None:
        try:
            service.terminate_session(sandbox_id)
        except SandboxUnavailableError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app


def build_default_service() -> SessionBrokerService:
    app_name = os.environ.get("COMPUTER_USE_BROKER_APP_NAME", "modal-computer-use")
    return SessionBrokerService(manager=ComputerSandboxManager(app_name=app_name))


try:
    import modal
except ImportError:
    modal = None
    app = None
else:
    app = modal.App("modal-computer-use-session-broker")
    _image = modal.Image.debian_slim().pip_install("modal-computer-use[modal]")

    @app.cls(
        image=_image,
        min_containers=int(os.environ.get("COMPUTER_USE_BROKER_MIN_CONTAINERS", "0")),
        scaledown_window=int(os.environ.get("COMPUTER_USE_BROKER_SCALEDOWN_WINDOW", "300")),
    )
    @modal.concurrent(max_inputs=100, target_inputs=80)
    class SessionBroker:
        @modal.enter()
        def setup(self) -> None:
            self.service = build_default_service()

        @modal.asgi_app(requires_proxy_auth=True)
        def web(self) -> FastAPI:
            return build_session_broker_app(self.service)


def build_modal_app() -> object:
    if app is None:
        raise ImportError("Modal is required to build the session broker app")
    return app


def _session_config(request: CreateSessionRequest) -> ComputerConfig:
    kwargs = {
        "run_id": request.run_id,
        "ingress": request.ingress,
        "runtime": {"modal_region": request.modal_region} if request.modal_region else {},
    }
    if request.resource_profile is not None:
        kwargs["resources"] = ResourceConfig(profile=request.resource_profile)
    if request.browser is not None:
        kwargs["browser"] = BrowserConfig(kind=request.browser, prewarm=request.browser_prewarm)
    return ComputerConfig(**kwargs)


def _session_response_from_computer(
    computer: ComputerSandbox,
    *,
    modal_region: str | None,
    include_daemon_token: bool,
) -> SessionResponse:
    metadata = computer.metadata()
    if metadata is None:
        raise RuntimeError("computer sandbox metadata unavailable")
    created_at = (
        None
        if metadata.created_at is None
        else metadata.created_at.astimezone(UTC).isoformat()
    )
    token = getattr(computer.client.transport, "token", None) if include_daemon_token else None
    return SessionResponse(
        sandbox_id=metadata.sandbox_id,
        app_name=metadata.app_name,
        name=metadata.name,
        run_id=metadata.run_id,
        owner=metadata.owner,
        created_at=created_at,
        modal_region=modal_region,
        daemon_base_url=computer.client.base_url,
        daemon_token=token,
        status=metadata.status,
    )


if __name__ == "__main__":
    service = build_default_service()
    print(
        {
            "created_at": datetime.now(UTC).isoformat(),
            "message": "Deploy this file with Modal to expose the ASGI session broker.",
            "service": service.manager.app_name,
        }
    )
