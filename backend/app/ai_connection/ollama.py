"""Shared server-side Ollama Cloud credentials; no local Ollama installation required."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import HTTPException
from openai import AsyncOpenAI
from pydantic_ai import ModelProfile
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
        # Cloud does not enforce native JSON schemas. Put the schema in the prompt
        # so Gemma sees the field names even when it answers without an output tool.
        yield OpenAIChatModel(
            "gemma4:31b",
            provider=OllamaProvider(openai_client=client),
            profile=ModelProfile(
                default_structured_output_mode="prompted",
                supports_json_schema_output=False,
            ),
        )
