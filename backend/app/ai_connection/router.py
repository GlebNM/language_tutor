from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from openai.types.shared.reasoning_effort import ReasoningEffort
from pydantic import BaseModel, ConfigDict, Field

from backend.app.agents.subscription_proof import ProofResult

from .ollama import ollama_configured
from .service import CatalogModel, ConnectionService, ConnectionStatus
from .settings import AISettings
from .siwc import SIWCOAuthFlow


class AISettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str = Field(min_length=1, max_length=200)
    reasoning_effort: ReasoningEffort | None = None


class TemporaryModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


def create_ai_router(
    *,
    data_dir: Path,
    mcp_url: str,
    callback_host: str = "0.0.0.0",
    callback_port: int = 1456,
    oauth_timeout: float = 300,
    model_name: str = "gpt-6-luna",
    flow_factory: Callable[..., SIWCOAuthFlow] = SIWCOAuthFlow,
    service: ConnectionService | None = None,
) -> APIRouter:
    service = service or ConnectionService(
        data_dir=data_dir,
        callback_host=callback_host,
        callback_port=callback_port,
        oauth_timeout=oauth_timeout,
        flow_factory=flow_factory,
        mcp_url=mcp_url,
        model_name=model_name,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await service.close()

    router = APIRouter(prefix="/api/ai", tags=["AI connection"], lifespan=lifespan)

    @router.get("/connection")
    async def status() -> ConnectionStatus:
        return await service.inspect()

    @router.get("/settings")
    async def get_settings() -> dict[str, str | bool | None]:
        current = service.settings_store.read()
        return {
            "model": current.model,
            "reasoning_effort": current.reasoning_effort,
            "temporary_model": current.temporary_model,
            "temporary_model_available": ollama_configured(),
        }

    @router.put("/temporary-model")
    async def temporary_model(settings: TemporaryModelInput) -> dict[str, str | bool | None]:
        if settings.enabled and not ollama_configured():
            raise HTTPException(409, "Добавьте OLLAMA_API_KEY в .env и перезапустите приложение.")
        service.settings_store.write(
            replace(service.settings_store.read(), temporary_model=settings.enabled)
        )
        return await get_settings()

    @router.put("/settings")
    async def put_settings(settings: AISettingsInput) -> dict[str, str | None]:
        models = await service.available_models()
        selected = next((item for item in models if item["id"] == settings.model), None)
        if selected is None:
            raise HTTPException(422, "Модель недоступна этой подписке.")
        efforts = selected["reasoning_efforts"]
        if settings.reasoning_effort is not None and settings.reasoning_effort not in efforts:
            raise HTTPException(422, "Этот уровень reasoning недоступен для выбранной модели.")
        saved = service.settings_store.write(
            AISettings(
                model=settings.model,
                reasoning_effort=settings.reasoning_effort,
                temporary_model=service.settings_store.read().temporary_model,
            )
        )
        return {"model": saved.model, "reasoning_effort": saved.reasoning_effort}

    @router.get("/models")
    async def models_catalog() -> dict[str, list[CatalogModel]]:
        return {"models": await service.available_models()}

    @router.post("/connection/start")
    async def start() -> dict[str, object]:
        return await service.start()

    @router.post("/connection/cancel")
    async def cancel() -> ConnectionStatus:
        return await service.cancel()

    @router.delete("/connection")
    async def disconnect() -> ConnectionStatus:
        return await service.disconnect()

    @router.post("/proof")
    async def proof() -> ProofResult:
        return await service.proof()

    return router
