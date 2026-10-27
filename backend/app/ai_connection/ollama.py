"""Shared server-side Ollama Cloud credentials; no local Ollama installation required."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import HTTPException
from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.ollama import OllamaProvider


def ollama_configured() -> bool:
    return bool(os.getenv("OLLAMA_API_KEY", "").strip())


@asynccontextmanager
async def ollama_model() -> AsyncIterator[OpenAIChatModel]:
    key = os.getenv("OLLAMA_API_KEY", "").strip()
    if not key:
        raise HTTPException(409, "Добавьте OLLAMA_API_KEY в .env и перезапустите приложение.")
    async with AsyncOpenAI(
        api_key=key, base_url="https://ollama.com/v1", max_retries=0, timeout=30
    ) as client:
        yield OpenAIChatModel("gemma4:31b", provider=OllamaProvider(openai_client=client))
