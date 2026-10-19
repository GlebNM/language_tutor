"""Controlled model, real HTTP MCP process. No subscription credentials or provider traffic."""

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator

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
        assert names == ["get_proof_context"]
        with pytest.raises(ToolError, match="mismatched"):
            await client.call_tool(
                "get_proof_context",
                {
                    "material_id": "another-material",
                    "cue_id": "proof-cue",
                    "token_id": "proof-token",
                },
            )
