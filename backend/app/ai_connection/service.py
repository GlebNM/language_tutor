from __future__ import annotations

import asyncio
import secrets
import socket
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Literal

import httpx2
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from openai import AsyncOpenAI
from pydantic import BaseModel
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.providers.openai import OpenAIProvider

from backend.app.agents.subscription_proof import ProofResult, run_proof

from .siwc import DIRECT_SCOPE, SIWCOAuthFlow, SubscriptionError, TokenManager
from .siwc_model import SIWCTransport, siwc_model
from .storage import CredentialStore


class ConnectionStatus(BaseModel):
    status: Literal["disconnected", "connecting", "connected", "reauth_required", "limit_reached"]
    route: str = "siwc"
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
        flow_factory: Callable[..., SIWCOAuthFlow],
        mcp_url: str,
        model_name: str,
    ) -> None:
        self.mcp_url = mcp_url
        self.model_name = model_name
        self.provider: OpenAIProvider | None = None
        self.http_client: httpx2.AsyncClient | None = None
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
        self.flow: SIWCOAuthFlow | None = None
        self.lock = asyncio.Lock()
        self.server: uvicorn.Server | None = None
        self.server_task: asyncio.Task[None] | None = None
        self.timeout_task: asyncio.Task[None] | None = None

    async def inspect(self) -> ConnectionStatus:
        if self.state.status == "connected":
            try:
                credentials = await self.store.load()
                if DIRECT_SCOPE not in credentials.scopes:
                    self.state = ConnectionStatus(
                        status="reauth_required", error="plan_permission_missing"
                    )
            except Exception:
                self.state = ConnectionStatus(
                    status="reauth_required", error="connection_unavailable"
                )
        return self.state

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
                        "message": (
                            f"Порт OAuth {self.callback_port} занят. Измените AI_CALLBACK_PORT."
                        ),
                    },
                ) from None
            self.store = CredentialStore(self.data_dir / "credentials")
            self.flow = self.flow_factory(
                store=self.store,
                redirect_uri=f"http://127.0.0.1:{self.callback_port}/auth/callback",
            )
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
                    credentials = await flow.exchange_code(
                        code, client_id=request.query_params.get("client_id")
                    )
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
            self.state = ConnectionStatus(
                status="connected" if DIRECT_SCOPE in credentials.scopes else "reauth_required",
                error=None if DIRECT_SCOPE in credentials.scopes else "plan_permission_missing",
            )
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
        if self.http_client:
            await self.http_client.aclose()
            self.http_client = None
        self.provider = None

    async def close(self) -> None:
        await self.cancel()
        await self.close_provider()

    async def connected_proof(self) -> ProofResult:
        async with asyncio.timeout(30):
            async with self.lock:
                if self.provider is None:
                    await self.store.load()
                    self.http_client = httpx2.AsyncClient(
                        transport=SIWCTransport(TokenManager(self.store)),
                        timeout=10,
                    )
                    self.provider = OpenAIProvider(
                        openai_client=AsyncOpenAI(
                            api_key="subscription-oauth",
                            base_url="https://api.openai.com/v1",
                            http_client=self.http_client,
                            max_retries=0,
                        )
                    )
                client = self.http_client
                provider = self.provider
            if client is None:
                raise HTTPException(503, "Подключение AI недоступно.")
            catalog_response = await client.get("https://api.openai.com/v1/models")
            if catalog_response.is_error:
                if catalog_response.status_code in (401, 403):
                    raise SubscriptionError(
                        "model_catalog_unauthorized", catalog_response.status_code
                    )
                raise HTTPException(503, "Не удалось получить модели подписки. Повторите запрос.")
            catalog = catalog_response.json().get("models", [])
            available = [item["slug"] for item in catalog if item.get("visibility") == "list"]
            selected = self.model_name or (available[0] if available else "")
            if not selected or selected not in available:
                raise HTTPException(409, "Модель недоступна подписке. Проверьте AI_MODEL.")
            return await run_proof(model=siwc_model(selected, provider), mcp_url=self.mcp_url)

    async def proof(self) -> ProofResult:
        async with self.lock:
            if self.state.status not in ("connected", "limit_reached"):
                raise HTTPException(409, "Сначала подключите подписку.")
            task = asyncio.create_task(self.connected_proof())
            self.runs.add(task)
        try:
            result = await task
            if not self.store.enabled:
                raise HTTPException(409, "Подписка отключена.")
            self.state = ConnectionStatus(status="connected", live_verified=True)
            return result
        except SubscriptionError as error:
            if error.status == 429:
                self.state = ConnectionStatus(status="limit_reached", error="subscription_limit")
                raise HTTPException(429, "Достигнут лимит подписки. Повторите позже.") from None
            if error.status == 403:
                raise HTTPException(
                    403, "Подписка или политика OpenAI не разрешает этот запрос."
                ) from None
            if error.status in (400, 401):
                self.state = ConnectionStatus(
                    status="reauth_required", error="authorization_expired"
                )
                raise HTTPException(409, "Требуется повторное подключение подписки.") from None
            raise HTTPException(503, "Подписка временно недоступна. Повторите позже.") from None
        except ModelHTTPError as error:
            if error.status_code == 429:
                self.state = ConnectionStatus(status="limit_reached", error="subscription_limit")
                raise HTTPException(429, "Достигнут лимит подписки. Повторите позже.") from None
            if error.status_code == 403:
                raise HTTPException(
                    403, "Подписка или политика OpenAI не разрешает этот запрос."
                ) from None
            if error.status_code == 401:
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
