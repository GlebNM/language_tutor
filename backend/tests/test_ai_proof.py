"""Controlled model, real HTTP MCP process. No subscription credentials or provider traffic."""

import asyncio
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from pydantic_ai import models
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from backend.app.agents.subscription_proof import run_proof


@pytest.fixture(autouse=True)
def forbid_live_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)


@pytest.fixture
def mcp_url() -> Iterator[str]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {**os.environ, "MCP_HOST": "127.0.0.1", "MCP_PORT": str(port)}
    process = subprocess.Popen(
        [sys.executable, "-m", "backend.mcp_server"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        for _ in range(100):
            assert process.poll() is None, "MCP process stopped before readiness"
            try:
                httpx.get(url, timeout=0.1)
                break
            except httpx.TransportError:
                time.sleep(0.02)
        else:
            pytest.fail("MCP did not become ready")
        yield url
    finally:
        process.terminate()
        process.wait(timeout=5)


async def test_agent_selects_real_mcp_tool_and_validates_contextual_card(mcp_url: str) -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returned = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == "get_proof_context"
        ]
        if not returned:
            assert "get_proof_context" in [tool.name for tool in info.function_tools]
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "get_proof_context",
                        {
                            "material_id": "subscription-proof",
                            "cue_id": "proof-cue",
                            "token_id": "proof-token",
                        },
                    )
                ]
            )
        assert "She gave up smoking." in str(returned[0].content)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "translation": "бросила",
                        "explanation": "Здесь give up означает прекратить привычку.",
                        "expression": "give up",
                        "example": "He gave up smoking last year.",
                    },
                )
            ]
        )

    result = await run_proof(model=FunctionModel(respond), mcp_url=mcp_url)
    assert result.card.translation == "бросила"
    assert result.card.expression == "give up"
    assert result.tools[0].name == "get_proof_context"
    assert result.tools[0].success is True
    assert result.model_requests == 2
    assert result.model == "function:respond:"


async def test_invalid_card_is_retried_through_the_output_schema(mcp_url: str) -> None:
    attempts = 0

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "get_proof_context",
                        {
                            "material_id": "subscription-proof",
                            "cue_id": "proof-cue",
                            "token_id": "proof-token",
                        },
                    )
                ]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "translation": "" if attempts == 2 else "бросила",
                        "explanation": "Прекратила привычку.",
                        "expression": "give up",
                        "example": "She gave up smoking.",
                    },
                )
            ]
        )

    result = await run_proof(model=FunctionModel(respond), mcp_url=mcp_url)
    assert result.card.translation == "бросила"
    assert result.model_requests == 3


async def test_mcp_rejects_selection_from_another_material(mcp_url: str) -> None:
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    async with Client(mcp_url) as client:
        names = [tool.name for tool in await client.list_tools()]
        assert "get_proof_context" in names
        with pytest.raises(ToolError, match="mismatched"):
            await client.call_tool(
                "get_proof_context",
                {
                    "material_id": "another-material",
                    "cue_id": "proof-cue",
                    "token_id": "proof-token",
                },
            )


async def test_siwc_sdk_exchanges_real_mcp_context_before_structured_output(
    mcp_url: str,
    tmp_path: Path,
) -> None:
    import json
    from pathlib import Path

    import httpx2
    from openai import AsyncOpenAI
    from pydantic_ai.providers.openai import OpenAIProvider

    from backend.app.ai_connection.siwc import TokenManager
    from backend.app.ai_connection.siwc_credentials import SIWCCredentials
    from backend.app.ai_connection.siwc_model import SIWCTransport, siwc_model
    from backend.app.ai_connection.storage import CredentialStore

    store = CredentialStore(Path(tmp_path) / "credentials")
    await store.save(
        SIWCCredentials(
            access_token="test-access",
            refresh_token="test-refresh",
            id_token="test-id",
            client_id="oaiapp_test",
            subject="learner",
            scopes=["chatgpt.tokens.use.direct"],
            expires_at=time.time() + 3600,
        )
    )
    turns = 0

    async def remote(request: httpx2.Request) -> httpx2.Response:
        nonlocal turns
        turns += 1
        body = json.loads(request.content)
        tools = body["input"][0]["tools"]
        if turns == 1:
            assert "get_proof_context" in [tool["name"] for tool in tools]
            name = "get_proof_context"
            arguments = {
                "material_id": "subscription-proof",
                "cue_id": "proof-cue",
                "token_id": "proof-token",
            }
        else:
            outputs = [item for item in body["input"] if item.get("type") == "function_call_output"]
            assert outputs and "She gave up smoking." in str(outputs[0]["output"])
            name = next(
                tool["name"]
                for tool in tools
                if "translation" in tool["parameters"].get("properties", {})
            )
            arguments = {
                "translation": "бросила",
                "explanation": "Прекратила привычку.",
                "expression": "give up",
                "example": "He gave up smoking.",
            }
        item = {
            "id": f"fc_{turns}",
            "type": "function_call",
            "call_id": f"call_{turns}",
            "name": name,
            "arguments": json.dumps(arguments),
            "status": "completed",
        }
        response = {
            "id": f"resp_{turns}",
            "object": "response",
            "created_at": time.time(),
            "model": "test-siwc",
            "status": "completed",
            "output": [item],
        }
        events = [
            {
                "type": "response.output_item.added",
                "item": {**item, "arguments": ""},
                "output_index": 0,
                "sequence_number": 0,
            },
            {
                "type": "response.output_item.done",
                "item": item,
                "output_index": 0,
                "sequence_number": 1,
            },
            {"type": "response.completed", "response": response, "sequence_number": 2},
        ]
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(f"data: {json.dumps(event)}\n\n" for event in events).encode(),
        )

    async with httpx2.AsyncClient(
        transport=SIWCTransport(
            TokenManager(store),
            transport=httpx2.MockTransport(remote),
        )
    ) as client:
        provider = OpenAIProvider(
            openai_client=AsyncOpenAI(
                api_key="subscription-oauth",
                http_client=client,
                max_retries=0,
            )
        )
        # Only the controlled SDK transport is enabled; real MCP still uses local TCP.
        with models.override_allow_model_requests(True):
            result = await run_proof(model=siwc_model("test-siwc", provider), mcp_url=mcp_url)
    assert result.card.translation == "бросила"
    assert result.tools[0].success is True
    assert turns == 2


@pytest.mark.parametrize(
    ("source", "status", "response_status", "connection_status"),
    [
        ("oauth", 400, 409, "reauth_required"),
        ("inference", 400, 503, "connected"),
        ("inference", 401, 409, "reauth_required"),
        ("inference", 403, 403, "connected"),
        ("inference", 429, 429, "limit_reached"),
        ("inference", 503, 503, "connected"),
    ],
)
async def test_provider_errors_preserve_connection_unless_authorization_expired(
    tmp_path: Path,
    mcp_url: str,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    status: int,
    response_status: int,
    connection_status: str,
) -> None:
    import httpx2
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.app.ai_connection import create_ai_router
    from backend.app.ai_connection.siwc_credentials import SIWCCredentials
    from backend.app.ai_connection.storage import CredentialStore

    store = CredentialStore(tmp_path / "credentials")
    await store.save(
        SIWCCredentials(
            access_token="test-access",
            refresh_token="test-refresh",
            id_token="test-id",
            client_id="oaiapp_test",
            subject="learner",
            scopes=["chatgpt.tokens.use.direct"],
            expires_at=0 if source == "oauth" else time.time() + 3600,
        )
    )

    async def remote(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/models":
            return httpx2.Response(
                200, json={"models": [{"slug": "test-siwc", "visibility": "list"}]}
            )
        assert request.url.path == "/v1/responses"
        return httpx2.Response(status, json={"error": {"message": "Controlled failure"}})

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", lambda **kwargs: httpx2.MockTransport(remote))
    if source == "oauth":

        async def oauth_remote(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/accounts/oauth/token"
            return httpx.Response(400, json={"error": "invalid_grant"})

        oauth_client = httpx.AsyncClient(transport=httpx.MockTransport(oauth_remote))
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: oauth_client)

    app = FastAPI()
    app.include_router(create_ai_router(data_dir=tmp_path, mcp_url=mcp_url))
    with TestClient(app) as client, models.override_allow_model_requests(True):
        response = client.post("/api/ai/proof")
        assert response.status_code == response_status, response.text
        connection = client.get("/api/ai/connection").json()
        assert connection["status"] == connection_status
        if connection_status == "reauth_required":
            assert connection["error"] == "authorization_expired"
        elif connection_status == "limit_reached":
            assert connection["error"] == "subscription_limit"


async def test_concurrent_refresh_uses_rotated_token_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from backend.app.ai_connection.siwc import TokenManager
    from backend.app.ai_connection.siwc_credentials import SIWCCredentials
    from backend.app.ai_connection.storage import CredentialStore

    store = CredentialStore(tmp_path / "credentials")
    await store.save(
        SIWCCredentials(
            access_token="expired-access",
            refresh_token="original-refresh",
            id_token="test-id",
            client_id="oaiapp_test",
            subject="learner",
            scopes=["chatgpt.tokens.use.direct"],
            expires_at=0,
        )
    )
    refresh_calls = 0

    async def remote(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_calls
        assert request.url.path == "/api/accounts/oauth/token"
        assert request.content.decode().find("refresh_token=original-refresh") >= 0
        refresh_calls += 1
        await asyncio.sleep(0.02)
        return httpx.Response(
            200,
            json={
                "token_type": "Bearer",
                "access_token": "rotated-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
                "scope": "chatgpt.tokens.use.direct",
            },
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(remote))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: mock_client)
    manager = TokenManager(store)

    tokens = await asyncio.gather(*(manager.access_token() for _ in range(8)))

    assert tokens == ["rotated-access"] * 8
    assert refresh_calls == 1
    saved = await CredentialStore(tmp_path / "credentials").load()
    assert saved.access_token == "rotated-access"
    assert saved.refresh_token == "rotated-refresh"
    await mock_client.aclose()
