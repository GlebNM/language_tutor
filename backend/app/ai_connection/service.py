from __future__ import annotations

import asyncio
import secrets
import socket
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal, TypedDict, TypeVar, get_args, get_origin
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import jwt
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from openai import AsyncOpenAI
from openai.types.shared.reasoning_effort import ReasoningEffort
from pydantic import BaseModel
from pydantic_ai.exceptions import ModelHTTPError, UsageLimitExceeded
from pydantic_ai.models import Model
from pydantic_ai.providers.openai import OpenAIProvider

from backend.app.agents.subscription_proof import ProofResult, run_proof

from .ollama import ollama_configured, ollama_model
from .settings import AISettings, AISettingsStore
from .siwc import DIRECT_SCOPE, SIWCOAuthFlow, SubscriptionError, TokenManager
from .siwc_model import SIWCTransport, siwc_model
from .storage import CredentialStore

RunOutput = TypeVar("RunOutput")
_SDK_REASONING_LITERAL = next(
    value for value in get_args(ReasoningEffort) if get_origin(value) is Literal
)
SUPPORTED_REASONING_EFFORTS = frozenset(get_args(_SDK_REASONING_LITERAL))


class CatalogModel(TypedDict):
    id: str
    name: str
    reasoning_efforts: list[str]
    default_reasoning_effort: str | None


def normalize_model_catalog(raw_models: object) -> list[CatalogModel]:
    """Expose only SIWC-visible models and reasoning levels accepted by this transport."""
    if not isinstance(raw_models, list):
        return []
    result: list[CatalogModel] = []
    for item in raw_models:
        if not isinstance(item, dict) or item.get("visibility") != "list":
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        raw_efforts = item.get("supported_reasoning_levels")
        efforts = (
            [
                entry["effort"]
                for entry in raw_efforts
                if isinstance(entry, dict)
                and isinstance(entry.get("effort"), str)
                and entry["effort"] in SUPPORTED_REASONING_EFFORTS
            ]
            if isinstance(raw_efforts, list)
            else []
        )
        default = item.get("default_reasoning_level")
        if not isinstance(default, str) or default not in efforts:
            default = None
        result.append(
            {
                "id": slug,
                "name": item.get("display_name") or slug,
                "reasoning_efforts": efforts,
                "default_reasoning_effort": default,
            }
        )
    return result

OAuthFailureCategory = Literal[
    "provider",
    "identity",
    "registration",
    "network",
    "timeout",
    "storage",
    "response",
    "unexpected",
]


def _oauth_failure_category(error: Exception) -> OAuthFailureCategory:
    # Never return provider messages, arbitrary error codes, exception text or payloads.
    if isinstance(error, SubscriptionError):
        if error.code in {
            "invalid_issuer",
            "invalid_jwks_location",
            "invalid_signing_algorithm",
            "unknown_signing_key",
            "invalid_nonce",
            "subject_identity_mismatch",
        }:
            return "identity"
        if error.code in {"missing_issued_client_id", "client_identity_mismatch"}:
            return "registration"
        return "provider"
    if isinstance(error, jwt.PyJWTError):
        return "identity"
    if isinstance(error, (TimeoutError, httpx.TimeoutException, httpx2.TimeoutException)):
        return "timeout"
    if isinstance(error, (httpx.TransportError, httpx2.TransportError)):
        return "network"
    if isinstance(error, (httpx.HTTPStatusError, httpx2.HTTPStatusError)):
        return "provider"
    if isinstance(error, OSError):
        return "storage"
    if isinstance(error, (KeyError, ValueError, TypeError)):
        return "response"
    return "unexpected"


class ConnectionStatus(BaseModel):
    status: Literal["disconnected", "connecting", "connected", "reauth_required", "limit_reached"]
    route: str = "siwc"
    live_verified: bool = False
    error: str | None = None
    error_category: OAuthFailureCategory | None = None


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
        settings_store: AISettingsStore | None = None,
    ) -> None:
        self.mcp_url = mcp_url
        self.model_name = model_name
        self.settings_store = settings_store or AISettingsStore(data_dir, default_model=model_name)
        self.provider: OpenAIProvider | None = None
        self.http_client: httpx2.AsyncClient | None = None
        self.runs: set[asyncio.Task[Any]] = set()
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

    @property
    def available(self) -> bool:
        if self.settings_store.read().temporary_model:
            return ollama_configured()
        return self.state.status in {"connected", "limit_reached"}

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
                # A completed callback can leave TIME_WAIT connections on this fixed port.
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
                            f"Порт подключения AI {self.callback_port} занят. "
                            "Дождитесь завершения подключения другого пользователя и повторите."
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
        return await self._finish_callback(
            state=request.query_params.get("state", ""),
            code=request.query_params.get("code"),
            client_id=request.query_params.get("client_id"),
            denial=request.query_params.get("error"),
        )

    async def complete_from_url(self, callback_url: str) -> ConnectionStatus:
        flow = self.flow
        if flow is None:
            raise HTTPException(409, "Сначала начните подключение подписки.")
        callback_url = callback_url.strip()
        try:
            returned = urlsplit(callback_url)
            expected = urlsplit(flow.redirect_uri)
            if (
                any(ord(char) < 32 for char in callback_url)
                or "#" in callback_url
                or (returned.scheme, returned.netloc, returned.path)
                != (expected.scheme, expected.netloc, expected.path)
            ):
                raise ValueError("Invalid callback address")
            params = parse_qs(
                returned.query, keep_blank_values=True, strict_parsing=True, max_num_fields=20
            )
            if any(len(values) != 1 for values in params.values()):
                raise ValueError("Repeated callback parameter")
        except ValueError:
            raise HTTPException(
                400, "Вставьте полный адрес из строки браузера после входа в ChatGPT."
            ) from None
        response = await self._finish_callback(
            state=params.get("state", [""])[0],
            code=params.get("code", [None])[0],
            client_id=params.get("client_id", [None])[0],
            denial=params.get("error", [None])[0],
        )
        if response.status_code >= 400:
            raise HTTPException(response.status_code, bytes(response.body).decode())
        return self.state

    async def _finish_callback(
        self, *, state: str, code: str | None, client_id: str | None, denial: str | None
    ) -> HTMLResponse:
        async with self.lock:
            flow = self.flow
            if flow is None:
                return HTMLResponse("Подключение уже завершено или отменено.", status_code=409)
            if not secrets.compare_digest(state.encode(), flow.state.encode()):
                return HTMLResponse("Неверное состояние OAuth. Начните подключение заново.", 400)
            if denial:
                self.flow = None
                self.state = ConnectionStatus(status="disconnected", error="oauth_denied")
                asyncio.create_task(self.stop_after_response(self.server))
                return HTMLResponse("Подключение отменено. Вернитесь в Language Tutor.", 400)
            if not code:
                return HTMLResponse("Отсутствует код авторизации.", 400)
            self.flow = None
            if self.timeout_task:
                self.timeout_task.cancel()
            try:
                async with asyncio.timeout(30):
                    credentials = await flow.exchange_code(code, client_id=client_id)
                self.store = CredentialStore(self.data_dir / "credentials")
                await self.store.save(credentials)
            except Exception as error:
                self.state = ConnectionStatus(
                    status="reauth_required",
                    error="oauth_exchange_failed",
                    error_category=_oauth_failure_category(error),
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

    async def _catalog_client(self) -> httpx2.AsyncClient:
        async with asyncio.timeout(30):
            if self.state.status not in ("connected", "limit_reached"):
                raise HTTPException(409, "Сначала подключите подписку.")
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
            if client is None:
                raise HTTPException(503, "Подключение AI недоступно.")
            return client

    async def available_models(self) -> list[CatalogModel]:
        try:
            client = await self._catalog_client()
            response = await client.get("https://api.openai.com/v1/models")
            if response.is_error:
                if response.status_code in (401, 403):
                    raise SubscriptionError("model_catalog_unauthorized", response.status_code)
                raise HTTPException(503, "Не удалось получить модели подписки. Повторите запрос.")
            return normalize_model_catalog(response.json().get("models", []))
        except SubscriptionError as error:
            raise self.provider_failure(error) from None
        except (httpx.TransportError, httpx2.TransportError, TimeoutError):
            raise HTTPException(
                503, "Не удалось получить каталог моделей. Повторите запрос."
            ) from None
        except ValueError:
            raise HTTPException(
                409, "Подключение подписки недоступно. Подключите её заново."
            ) from None

    async def connected_model(self, settings: AISettings) -> Model:
        available = await self.available_models()
        selected = next((item for item in available if item["id"] == settings.model), None)
        if selected is None:
            raise HTTPException(409, "Выбранная модель недоступна подписке. Обновите настройки AI.")
        if (
            settings.reasoning_effort is not None
            and settings.reasoning_effort not in selected["reasoning_efforts"]
        ):
            raise HTTPException(
                409,
                "Выбранный уровень reasoning недоступен для этой модели. Обновите настройки AI.",
            )
        provider = self.provider
        if provider is None:
            raise HTTPException(503, "Подключение AI недоступно.")
        return siwc_model(settings.model, provider, settings.reasoning_effort)

    async def execute(
        self,
        operation: Callable[[Model], Awaitable[RunOutput]],
        *,
        settings: AISettings | None = None,
    ) -> RunOutput:
        """Run with the selected provider and a settings snapshot for the whole operation."""

        # Capture before awaiting credential setup or the model catalogue. A settings
        # change made while this run waits applies to the next run only.
        snapshot = (settings or self.settings_store.read()).effective()

        async def connected() -> RunOutput:
            async with asyncio.timeout(30):
                if snapshot.temporary_model:
                    async with ollama_model() as temporary:
                        return await operation(temporary)
                model = await self.connected_model(snapshot)
                return await operation(model)

        async with self.lock:
            if not snapshot.temporary_model and self.state.status not in (
                "connected", "limit_reached"
            ):
                raise HTTPException(409, "Сначала подключите подписку.")
            task = asyncio.create_task(connected())
            self.runs.add(task)
        try:
            result = await task
            if not snapshot.temporary_model and not self.store.enabled:
                raise HTTPException(409, "Подписка отключена.")
            if not snapshot.temporary_model:
                self.state = ConnectionStatus(status="connected", live_verified=True)
            return result
        except (SubscriptionError, ModelHTTPError) as error:
            if snapshot.temporary_model:
                status = error.status if isinstance(error, SubscriptionError) else error.status_code
                if status in (401, 403):
                    raise HTTPException(
                        403, "Ollama Cloud отклонила ключ или доступ к модели."
                    ) from None
                if status == 429:
                    raise HTTPException(
                        429, "Достигнут лимит Ollama Cloud. Повторите позже."
                    ) from None
                raise HTTPException(
                    503, "Ollama Cloud временно недоступна. Повторите запрос."
                ) from None
            raise self.provider_failure(error) from None
        except TimeoutError:
            raise HTTPException(504, "AI не ответил за 30 секунд. Повторите запрос.") from None
        except UsageLimitExceeded:
            raise HTTPException(
                422, "AI достиг ограничения числа раундов. Уточните вопрос и повторите запрос."
            ) from None
        except asyncio.CancelledError:
            parent = asyncio.current_task()
            if parent is not None and parent.cancelling():
                raise
            raise HTTPException(409, "Запрос AI отменён.") from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                503, "Не удалось завершить запрос AI и MCP. Повторите запрос."
            ) from None
        finally:
            self.runs.discard(task)

    async def proof(self) -> ProofResult:
        return await self.execute(lambda model: run_proof(model=model, mcp_url=self.mcp_url))

    def provider_failure(self, error: SubscriptionError | ModelHTTPError) -> HTTPException:
        subscription_error = isinstance(error, SubscriptionError)
        status = error.status if isinstance(error, SubscriptionError) else error.status_code
        if status == 429:
            self.state = ConnectionStatus(status="limit_reached", error="subscription_limit")
            return HTTPException(429, "Достигнут лимит подписки. Повторите позже.")
        if status == 403:
            return HTTPException(403, "Подписка или политика OpenAI не разрешает этот запрос.")
        # An OAuth refresh 400 invalidates authorization; an inference 400 does not.
        if status == 401 or (subscription_error and status == 400):
            self.state = ConnectionStatus(status="reauth_required", error="authorization_expired")
            return HTTPException(409, "Требуется повторное подключение подписки.")
        return HTTPException(
            503,
            "Подписка временно недоступна. Повторите позже."
            if subscription_error
            else "Провайдер AI временно недоступен. Повторите запрос.",
        )

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
