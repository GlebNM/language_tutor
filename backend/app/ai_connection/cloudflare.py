"""Server-side Workers AI access for the shared free model."""

import json
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from fastapi import HTTPException
from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

MODEL = "@cf/openai/gpt-oss-120b"


def cloudflare_configured() -> bool:
    return bool(
        re.fullmatch(r"[a-fA-F0-9]{32}", os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip())
        and os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
    )


class CloudflareTransport(httpx2.AsyncBaseTransport):
    """Adapt SDK text messages to Workers AI's string-only message schema."""

    def __init__(self, inner: httpx2.AsyncBaseTransport | None = None) -> None:
        self.inner = inner or httpx2.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(await request.aread())
        for message in payload.get("messages", []):
            content = message.get("content")
            if isinstance(content, list):
                if any(part.get("type") != "text" for part in content):
                    raise ValueError("Учебная модель Cloudflare принимает текстовые сообщения.")
                message["content"] = "\n".join(part["text"] for part in content)
            elif content is None:
                message["content"] = ""
        headers = dict(request.headers)
        headers.pop("content-length", None)
        adapted = httpx2.Request(
            request.method, request.url, headers=headers, json=payload,
            extensions=request.extensions,
        )
        return await self.inner.handle_async_request(adapted)

    async def aclose(self) -> None:
        await self.inner.aclose()


@asynccontextmanager
async def cloudflare_model() -> AsyncIterator[OpenAIChatModel]:
    if not cloudflare_configured():
        raise HTTPException(
            409, "Укажите CLOUDFLARE_ACCOUNT_ID и CLOUDFLARE_API_TOKEN и перезапустите приложение."
        )
    account = os.environ["CLOUDFLARE_ACCOUNT_ID"].strip()
    async with AsyncOpenAI(
        api_key=os.environ["CLOUDFLARE_API_TOKEN"].strip(),
        base_url=f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=CloudflareTransport(), timeout=30),
    ) as client:
        yield OpenAIChatModel(
            MODEL,
            provider=OpenAIProvider(openai_client=client),
            profile=OpenAIModelProfile(
                default_structured_output_mode="tool",
                supports_json_schema_output=True,
                openai_chat_send_back_thinking_parts=False,
            ),
            # Workers AI defaults to 256 output tokens, which truncates cards,
            # writer drafts and review JSON after reasoning consumes part of it.
            settings=OpenAIChatModelSettings(openai_reasoning_effort="low", max_tokens=8192),
        )
