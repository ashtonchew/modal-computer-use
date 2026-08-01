from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or self.max_bytes == 0
            or _is_streamed_artifact_upload(scope)
        ):
            await self.app(scope, receive, send)
            return

        content_length = _content_length(scope)
        if content_length is not None and content_length > self.max_bytes:
            await _send_too_large(send, self.max_bytes)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise RequestBodyTooLarge(self.max_bytes)
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestBodyTooLarge:
            await _send_too_large(send, self.max_bytes)


class RequestBodyTooLarge(Exception):
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        super().__init__("request body exceeds the configured byte limit")


def _is_streamed_artifact_upload(scope: Scope) -> bool:
    return scope.get("method") == "PUT" and str(scope.get("path", "")).startswith(
        "/v1/artifacts/"
    )


def _content_length(scope: Scope) -> int | None:
    for raw_name, raw_value in scope.get("headers", ()):
        if raw_name.lower() != b"content-length":
            continue
        try:
            value = int(raw_value)
        except ValueError:
            return None
        return value if value >= 0 else None
    return None


async def _send_too_large(send: Send, max_bytes: int) -> None:
    body = json.dumps(
        {
            "code": "request_body_too_large",
            "message": "request body exceeds the configured byte limit",
            "details": {"max_bytes": max_bytes},
        },
        separators=(",", ":"),
    ).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
        (b"x-computer-use-error-code", b"request_body_too_large"),
    ]
    await send({"type": "http.response.start", "status": 413, "headers": headers})
    await send({"type": "http.response.body", "body": body})
