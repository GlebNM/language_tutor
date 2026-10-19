"""SIWC HTTP dialect adapter around Pydantic AI's standard Responses model."""

import json
from collections.abc import AsyncIterator

import httpx2
from pydantic_ai.exceptions import UnexpectedModelBehavior, UserError
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from .siwc import SubscriptionError, TokenManager

UNSUPPORTED = frozenset(
    (
        "background",
        "conversation",
        "previous_response_id",
        "max_output_tokens",
        "max_tool_calls",
        "metadata",
        "moderation",
        "multi_agent",
        "prompt",
        "prompt_cache_retention",
        "safety_identifier",
        "temperature",
        "top_logprobs",
        "top_p",
        "truncation",
        "user",
    )
)


class CompletedStream(httpx2.AsyncByteStream):
    def __init__(self, source: httpx2.AsyncByteStream) -> None:
        self.source = source

    async def __aiter__(self) -> AsyncIterator[bytes]:
        buffer = b""
        data: list[bytes] = []
        completed = False
        async for chunk in self.source:
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.rstrip(b"\r")
                if line.startswith(b"data:"):
                    data.append(line[5:].strip())
                elif not line and data:
                    raw = b"\n".join(data)
                    data = []
                    if raw == b"[DONE]":
                        continue
                    event = json.loads(raw)
                    kind = event.get("type")
                    if kind == "response.completed":
                        if event.get("response", {}).get("status") != "completed":
                            raise UnexpectedModelBehavior("SIWC completion status is not completed")
                        completed = True
                    elif kind in ("response.failed", "error"):
                        error = event.get("response", {}).get("error") or event
                        code = error.get("code", "inference_failed")
                        status = 429 if code == "subscription_sharing_usage_limit_exceeded" else 503
                        raise SubscriptionError(code, status)
                    elif kind == "response.incomplete":
                        raise UnexpectedModelBehavior("SIWC response incomplete")
            yield chunk
        if not completed:
            raise UnexpectedModelBehavior("SIWC stream ended without response.completed")

    async def aclose(self) -> None:
        await self.source.aclose()


class SIWCTransport(httpx2.AsyncBaseTransport):
    def __init__(
        self,
        manager: TokenManager,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.manager = manager
        self.transport = transport or httpx2.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.host != "api.openai.com" or request.url.scheme != "https":
            raise ValueError("SIWC requests must use the official API origin")
        if request.url.path not in ("/v1/responses", "/v1/models"):
            raise ValueError("Unsupported SIWC endpoint")
        headers = dict(request.headers)
        headers["authorization"] = f"Bearer {await self.manager.access_token()}"
        headers.pop("content-length", None)
        content = await request.aread()
        if request.url.path == "/v1/responses":
            body = json.loads(content)
            body["store"] = False
            body["stream"] = True
            for field in UNSUPPORTED:
                body.pop(field, None)
            tools = body.pop("tools", [])
            if any(tool.get("type") not in ("function", "custom") for tool in tools):
                raise ValueError("SIWC supports only local function/custom tools here")
            if tools:
                body["input"].insert(
                    0, {"type": "additional_tools", "role": "developer", "tools": tools}
                )
            for item in body["input"]:
                if item.get("role") == "system":
                    item["role"] = "developer"
            content = json.dumps(body).encode()
        forwarded = httpx2.Request(
            request.method,
            request.url,
            headers=headers,
            content=content,
            extensions=request.extensions,
        )
        response = await self.transport.handle_async_request(forwarded)
        if request.url.path == "/v1/responses" and response.status_code < 400:
            assert isinstance(response.stream, httpx2.AsyncByteStream)
            return httpx2.Response(
                response.status_code,
                headers=response.headers,
                stream=CompletedStream(response.stream),
                extensions=response.extensions,
            )
        return response

    async def aclose(self) -> None:
        await self.transport.aclose()


class SIWCResponsesModel(OpenAIResponsesModel):
    def prepare_request(
        self,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> tuple[ModelSettings | None, ModelRequestParameters]:
        if model_settings and any(
            key in model_settings
            for key in (
                "openai_previous_response_id",
                "openai_conversation_id",
                "openai_background",
            )
        ):
            raise UserError("SIWC requires complete input history without server-side continuation")
        return super().prepare_request(model_settings, model_request_parameters)


def siwc_model(model_name: str, provider: OpenAIProvider) -> OpenAIResponsesModel:
    return OpenAIResponsesModel(
        model_name,
        provider=provider,
        profile=OpenAIModelProfile(
            openai_responses_requires_streaming=True,
            openai_responses_requires_store_false=True,
            openai_system_prompt_role="developer",
            openai_supports_input_token_counting=False,
            openai_supports_prompt_cache_breakpoints=False,
            supports_cache=False,
            openai_unsupported_model_settings=("max_tokens", "temperature", "top_p"),
        ),
    )
