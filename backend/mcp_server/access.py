"""Authenticate internal MCP calls before selecting any learner data."""

import asyncio
from pathlib import Path

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from backend.app.accounts import AccountStore


class MCPAccess:
    def __init__(self, app: ASGIApp, directory: Path):
        self.app = app
        self.store = AccountStore(directory)

    def account_directory(self, authorization: str) -> Path | None:
        if not authorization.startswith("Bearer ") or len(authorization) > 100:
            return None
        with self.store.connect() as db:
            row = db.execute(
                "SELECT id FROM accounts WHERE mcp_token=?", (authorization[7:],)
            ).fetchone()
        return self.store.learner_directory(row["id"]) if row else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        directory = await asyncio.to_thread(
            self.account_directory, Headers(scope=scope).get("authorization", "")
        )
        if directory is None:
            await JSONResponse({"detail": "MCP authentication required"}, status_code=401)(
                scope,
                receive,
                send,
            )
            return
        scope.setdefault("state", {})["learner_directory"] = directory
        await self.app(scope, receive, send)
