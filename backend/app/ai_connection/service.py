from __future__ import annotations

import asyncio
import secrets
import socket
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.openai_codex import OpenAICodexModel
from pydantic_ai.providers.openai_codex import (
    CredentialsRefreshError,
    OpenAICodexOAuthFlow,
    OpenAICodexProvider,
)

from backend.app.agents.subscription_proof import ProofResult, run_proof

from .storage import CredentialStore


class ConnectionStatus(BaseModel):
    status: Literal["disconnected", "connecting", "connected", "reauth_required", "limit_reached"]
    route: str = "pydantic-codex-candidate"
    live_verified: bool = False
    error: str | None = None


class ConnectionService:
    def __init__(
        self,
        *,
        data_dir: Path,
        callback_host: str,
        callback_port: int,
        oauth_timeout: float,
        flow_factory: Callable[[], OpenAICodexOAuthFlow],
        mcp_url: str,
        model_name: str,
    ) -> None:
        self.mcp_url = mcp_url
        self.model_name = model_name
        self.provider: OpenAICodexProvider | None = None
        self.runs: set[asyncio.Task[ProofResult]] = set()
        self.data_dir = data_dir
        self.store = CredentialStore(data_dir / "credentials")
        self.callback_host = callback_host
        self.callback_port = callback_port
        self.oauth_timeout = oauth_timeout
        self.flow_factory = flow_factory
        self.state = ConnectionStatus(
            status="connected" if self.store.path.exists() else "disconnected"
        )
        self.flow: OpenAICodexOAuthFlow | None = None
        self.lock = asyncio.Lock()
        self.server: uvicorn.Server | None = None
        self.server_task: asyncio.Task[None] | None = None
        self.timeout_task: asyncio.Task[None] | None = None

    async def start(self) -> dict[str, object]:
        async with self.lock:
            if self.flow is not None:
                raise HTTPException(409, "Подключение уже ожидает ответа браузера.")
            if self.provider or self.runs or self.store.path.exists():
                raise HTTPException(409, "Сначала отключите текущую подписку.")
            await self.stop_listener()
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.bind((self.callback_host, self.callback_port))
                listener.listen(16)
                listener.setblocking(False)
            except OSError:
                listener.close()
                self.state = ConnectionStatus(status="disconnected", error="callback_port_in_use")
                raise HTTPException(
                    409,
                    {
                        "code": "callback_port_in_use",
                        "message": "Порт OAuth 1455 занят. Освободите его и повторите подключение.",
                    },
                ) from None
            self.flow = self.flow_factory()
            callback = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
            callback.add_api_route("/auth/callback", self.callback, methods=["GET"])
            self.server = uvicorn.Server(
                uvicorn.Config(
                    callback,
                    access_log=False,
                    log_level="critical",
                    lifespan="off",
                )
            )
            self.server_task = asyncio.create_task(self.server.serve(sockets=[listener]))
            while not self.server.started:
                if self.server_task.done():
                    listener.close()
                    self.flow = None
                    raise HTTPException(503, "Не удалось открыть локальный callback.")
                await asyncio.sleep(0.005)
            self.state = ConnectionStatus(status="connecting")
            self.timeout_task = asyncio.create_task(self.expire())
            return {**self.state.model_dump(), "authorization_url": self.flow.authorization_url()}

    async def callback(self, request: Request) -> HTMLResponse:
        async with self.lock:
            flow = self.flow
            if flow is None:
                return HTMLResponse("Подключение уже завершено или отменено.", status_code=409)
            received_state = request.query_params.get("state", "")
            if not secrets.compare_digest(received_state, flow.state):
                return HTMLResponse("Неверное состояние OAuth. Начните подключение заново.", 400)
            if request.query_params.get("error"):
                self.flow = None
                self.state = ConnectionStatus(status="disconnected", error="oauth_denied")
                asyncio.create_task(self.stop_after_response(self.server))
                return HTMLResponse("Подключение отменено. Вернитесь в Language Tutor.", 400)
            code = request.query_params.get("code")
            if not code:
                return HTMLResponse("Отсутствует код авторизации.", 400)
            self.flow = None
            if self.timeout_task:
                self.timeout_task.cancel()
            try:
                async with asyncio.timeout(30):
                    credentials = await flow.exchange_code(code)
                self.store = CredentialStore(self.data_dir / "credentials")
                await self.store.save(credentials)
            except Exception:
                self.state = ConnectionStatus(
                    status="reauth_required", error="oauth_exchange_failed"
                )
                asyncio.create_task(self.stop_after_response(self.server))
                return HTMLResponse(
                    "Не удалось подключить подписку. Повторите вход в приложении.", 400
                )
            self.state = ConnectionStatus(status="connected")
            asyncio.create_task(self.stop_after_response(self.server))
            return HTMLResponse("Подписка подключена. Вернитесь в Language Tutor.")

    async def expire(self) -> None:
        await asyncio.sleep(self.oauth_timeout)
        await self.cancel(error="oauth_timeout")

    async def cancel(self, *, error: str | None = None) -> ConnectionStatus:
        async with self.lock:
            self.flow = None
            self.state = ConnectionStatus(
                status="connected" if self.store.path.exists() else "disconnected", error=error
            )
            await self.stop_listener()
            return self.state

    async def disconnect(self) -> ConnectionStatus:
        await self.cancel()
        self.store.disconnect()
        self.state = ConnectionStatus(status="disconnected")
        await self.close_provider()
        return self.state

    async def close_provider(self) -> None:
        pending = list(self.runs)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.provider:
            await self.provider.__aexit__(None, None, None)
            self.provider = None

    async def close(self) -> None:
        await self.cancel()
        await self.close_provider()

    async def proof(self) -> ProofResult:
        async with self.lock:
            if self.state.status not in ("connected", "limit_reached"):
                raise HTTPException(409, "Сначала подключите подписку.")
            if self.provider is None:
                try:
                    await self.store.load()
                except Exception:
                    self.state = ConnectionStatus(
                        status="reauth_required", error="connection_unavailable"
                    )
                    raise HTTPException(409, "Требуется повторное подключение подписки.") from None
                # Always supply our source: the provider's default reads Codex CLI credentials.
                self.provider = OpenAICodexProvider(credential_source=self.store)
                self.provider.client.max_retries = 0
                await self.provider.__aenter__()
            task = asyncio.create_task(
                run_proof(
                    model=OpenAICodexModel(self.model_name, provider=self.provider),
                    mcp_url=self.mcp_url,
                )
            )
            self.runs.add(task)
        try:
            result = await task
            if not self.store.enabled:
                raise HTTPException(409, "Подписка отключена.")
            self.state = ConnectionStatus(status="connected", live_verified=True)
            return result
        except CredentialsRefreshError:
            self.state = ConnectionStatus(status="reauth_required", error="refresh_failed")
            raise HTTPException(409, "Требуется повторное подключение подписки.") from None
        except ModelHTTPError as error:
            if error.status_code == 429:
                self.state = ConnectionStatus(status="limit_reached", error="subscription_limit")
                raise HTTPException(429, "Достигнут лимит подписки. Повторите позже.") from None
            if error.status_code in (401, 403):
                self.state = ConnectionStatus(
                    status="reauth_required", error="authorization_expired"
                )
                raise HTTPException(409, "Требуется повторное подключение подписки.") from None
            raise HTTPException(
                503, "Провайдер AI временно недоступен. Повторите запрос."
            ) from None
        except TimeoutError:
            raise HTTPException(504, "AI не ответил за 30 секунд. Повторите запрос.") from None
        except asyncio.CancelledError:
            raise HTTPException(409, "Запрос AI отменён.") from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                503, "Не удалось завершить проверку AI и MCP. Повторите запрос."
            ) from None
        finally:
            self.runs.discard(task)

    async def stop_after_response(self, server: uvicorn.Server | None) -> None:
        await asyncio.sleep(0.05)
        async with self.lock:
            if self.server is server:
                await self.stop_listener()

    async def stop_listener(self) -> None:
        if self.timeout_task and self.timeout_task is not asyncio.current_task():
            self.timeout_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.timeout_task
        self.timeout_task = None
        if self.server:
            self.server.should_exit = True
        if self.server_task:
            await self.server_task
        self.server = None
        self.server_task = None
