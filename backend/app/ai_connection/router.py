from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI
from pydantic_ai.providers.openai_codex import OpenAICodexOAuthFlow

from backend.app.agents.subscription_proof import ProofResult

from .service import ConnectionService, ConnectionStatus


def create_ai_router(
    *,
    data_dir: Path,
    mcp_url: str,
    callback_host: str = "0.0.0.0",
    callback_port: int = 1455,
    oauth_timeout: float = 300,
    model_name: str = "gpt-5.6-luna",
    flow_factory: Callable[[], OpenAICodexOAuthFlow] = OpenAICodexOAuthFlow,
) -> APIRouter:
    service = ConnectionService(
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
        return service.state

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
