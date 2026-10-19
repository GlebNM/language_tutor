"""Loopback browser access and bounded JSON bodies, without application accounts."""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_REQUEST_BYTES = 2_200_000


class LocalAccessMiddleware:
    def __init__(self, app: ASGIApp, allowed_origins: list[str]):
        self.app = app
        self.allowed_origins = frozenset(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if origin and origin not in self.allowed_origins:
            await JSONResponse({"detail": "Запрос с этого origin не разрешён."}, status_code=403)(
                scope, receive, send
            )
            return

        async def secured_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                ]
                if origin:
                    extra.extend(
                        [
                            (b"access-control-allow-origin", origin.encode()),
                            (b"vary", b"Origin"),
                        ]
                    )
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        if scope["method"] == "OPTIONS":
            response = Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type",
                },
            )
            await response(scope, receive, secured_send)
            return
        if scope["path"].startswith("/api/") and scope["method"] in {
            "POST",
            "PUT",
            "PATCH",
        }:
            if headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                # Empty control operations (OAuth start/cancel/proof) carry no document.
                no_body = headers.get("content-length", "0") == "0" and scope["path"].startswith(
                    "/api/ai/"
                )
                if not no_body:
                    await JSONResponse(
                        {"detail": "Используйте формат application/json."},
                        status_code=415,
                    )(scope, receive, secured_send)
                    return
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > MAX_REQUEST_BYTES:
                    await JSONResponse(
                        {"detail": "Запрос слишком большой. Файл должен быть не больше 2 МБ."},
                        status_code=413,
                    )(scope, receive, secured_send)
                    return
                if not message.get("more_body", False):
                    break
            consumed = False

            async def bounded_receive() -> Message:
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {
                        "type": "http.request",
                        "body": bytes(body),
                        "more_body": False,
                    }
                return await receive()

            await self.app(scope, bounded_receive, secured_send)
            return
        await self.app(scope, receive, secured_send)
