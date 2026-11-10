import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior

from backend.app.ai_connection import cloudflare, create_ai_router
from backend.app.ai_connection.service import ConnectionService
from backend.app.ai_connection.settings import AISettings, AISettingsStore
from backend.app.ai_connection.siwc import SIWCOAuthFlow


def test_temporary_model_preserves_subscription_choice_and_requires_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    store = AISettingsStore(tmp_path)
    store.write(AISettings("gpt-6.1-sol", "high"))
    app = FastAPI()
    app.include_router(create_ai_router(data_dir=tmp_path, mcp_url="http://mcp:8001/mcp"))
    with TestClient(app) as client:
        missing = client.put("/api/ai/temporary-model", json={"enabled": True})
        assert missing.status_code == 409
        assert "CLOUDFLARE_API_TOKEN" in missing.json()["detail"]
        assert not store.read().temporary_model
        monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a" * 32)
        monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "synthetic-cloudflare-secret")
        enabled = client.put("/api/ai/temporary-model", json={"enabled": True})
        assert enabled.status_code == 200
        assert enabled.json() == {
            "model": "gpt-6.1-sol", "reasoning_effort": "high",
            "temporary_model": True, "temporary_model_available": True,
        }
        assert "synthetic-cloudflare-secret" not in enabled.text
        assert store.read().effective() == AISettings("@cf/zai-org/glm-4.7-flash", None, True)
        # A different learner uses the same server key with independent settings.
        assert not AISettingsStore(tmp_path / "other-learner").read().temporary_model
        monkeypatch.delenv("CLOUDFLARE_API_TOKEN")
        assert client.put("/api/ai/temporary-model", json={"enabled": False}).status_code == 200
        assert store.read().effective() == AISettings("gpt-6.1-sol", "high")
    assert "synthetic-cloudflare-secret" not in store.path.read_text()


async def test_cloudflare_provider_sends_authenticated_cloud_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a" * 32)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "synthetic-cloudflare-secret")
    requests = []

    async def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert str(request.url) == (
            "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32
            + "/ai/v1/chat/completions"
        )
        assert request.headers["authorization"] == "Bearer synthetic-cloudflare-secret"
        payload = json.loads(request.content)
        assert payload["model"] == "@cf/zai-org/glm-4.7-flash"
        assert "reasoning_effort" not in payload
        assert payload["chat_template_kwargs"]["enable_thinking"] is True
        assert payload["max_completion_tokens"] == 8192
        return httpx2.Response(200, json={
            "id": "cloudflare-test", "object": "chat.completion", "created": 1,
            "model": "@cf/zai-org/glm-4.7-flash",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello!"},
                         "finish_reason": "stop"}],
        })

    transport = cloudflare.CloudflareTransport
    monkeypatch.setattr(
        cloudflare, "CloudflareTransport", lambda: transport(httpx2.MockTransport(respond))
    )
    async with cloudflare.cloudflare_model() as model:
        client = model._provider.client
        result = await Agent(model, name="cloudflare_connection_test").run("Say hello.")
        assert result.output == "Hello!"
    assert client.is_closed()
    assert len(requests) == 1


def connection(tmp_path: Path) -> ConnectionService:
    return ConnectionService(
        data_dir=tmp_path, callback_host="127.0.0.1", callback_port=0,
        oauth_timeout=5, flow_factory=SIWCOAuthFlow,
        mcp_url="http://mcp:8001/mcp", model_name="gpt-6-luna",
    )


async def test_temporary_execution_without_subscription_keeps_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a" * 32)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "synthetic-cloudflare-secret")
    service = connection(tmp_path)
    original = AISettings("gpt-6.1-sol", "high")
    service.settings_store.write(AISettings(original.model, original.reasoning_effort, True))
    assert service.available
    started, resume = asyncio.Event(), asyncio.Event()

    async def operation(model):
        assert model.model_name == "@cf/zai-org/glm-4.7-flash"
        started.set()
        await resume.wait()
        return "ok"

    task = asyncio.create_task(service.execute(operation))
    await asyncio.wait_for(started.wait(), timeout=5)
    service.settings_store.write(original)
    resume.set()
    assert await task == "ok"
    assert service.state.status == "disconnected"
    assert not service.available
    assert service.settings_store.read().effective() == original
    with pytest.raises(HTTPException) as error:
        await service.execute(operation)
    assert error.value.status_code == 409
    await service.close()


async def test_unexpected_model_failure_logs_type_without_private_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a" * 32)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "synthetic-cloudflare-secret")
    service = connection(tmp_path)
    service.settings_store.write(AISettings("gpt-6-luna", "low", True))

    async def failing(model):
        raise UnexpectedModelBehavior("synthetic-private-prompt")

    with pytest.raises(HTTPException) as error:
        await service.execute(failing)
    assert error.value.status_code == 502
    assert "error_type=UnexpectedModelBehavior" in caplog.text
    assert "synthetic-" not in caplog.text + error.value.detail
    await service.close()


@pytest.mark.parametrize(
    "provider_status,expected", [(401, 403), (402, 402), (403, 403), (429, 429), (500, 503)]
)
async def test_cloudflare_errors_preserve_subscription_state_and_hide_provider_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_status: int, expected: int,
) -> None:
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a" * 32)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "synthetic-cloudflare-secret")
    service = connection(tmp_path)
    service.settings_store.write(AISettings("gpt-6-luna", "low", True))

    async def failing(model):
        raise ModelHTTPError(provider_status, model.model_name, body="synthetic-private-detail")

    with pytest.raises(HTTPException) as error:
        await service.execute(failing)
    assert error.value.status_code == expected
    assert "Cloudflare" in error.value.detail
    if provider_status == 402:
        assert "оплату" in error.value.detail
    if provider_status == 429:
        assert "квота" in error.value.detail
    assert "synthetic-" not in error.value.detail
    assert service.state.status == "disconnected"
    await service.close()


async def test_tool_round_trip_normalizes_text_parts_and_null_assistant_content(monkeypatch):
    """Exercise the message shapes that Workers AI rejected in the live SDK probe."""
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a" * 32)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "synthetic-cloudflare-secret")
    calls = []
    requests = []

    class Answer(BaseModel):
        translation: str

    async def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert all(isinstance(message["content"], str) for message in payload["messages"])
        if len(requests) == 1:
            name, arguments = "lookup_word", {"word": "left"}
        else:
            assert any(m.get("tool_calls") and m["content"] == ""
                       for m in payload["messages"])
            assert any(m.get("reasoning_content") == "synthetic reasoning"
                       for m in payload["messages"])
            assert any(m["role"] == "tool" and "оставил" in m["content"]
                       for m in payload["messages"])
            name, arguments = "final_result", {"translation": "оставил"}
        return httpx2.Response(200, json={
            "id": "cloudflare-tools", "object": "chat.completion", "created": 1,
            "model": cloudflare.MODEL,
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None,
                "reasoning_content": "synthetic reasoning", "tool_calls": [{
                    "id": "call-" + str(len(requests)), "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }],
            }}],
        })

    transport = cloudflare.CloudflareTransport
    monkeypatch.setattr(
        cloudflare, "CloudflareTransport", lambda: transport(httpx2.MockTransport(respond))
    )
    async with cloudflare.cloudflare_model() as model:
        agent = Agent(model, name="cloudflare_tool_test", output_type=Answer)

        @agent.tool_plain
        def lookup_word(word: str) -> str:
            calls.append(word)
            return "оставил"

        result = await agent.run("Explain left in I left my bag on the bus.")
    assert result.output.translation == "оставил"
    assert calls == ["left"]
    assert len(requests) == 2


@pytest.mark.parametrize("request_limit", [4, 5])
async def test_cloudflare_writer_uses_strict_output_tools_with_function_tools(
    monkeypatch, request_limit
):
    """Keep response_format out of mixed tool turns after live Harmony/JSON failures."""
    from pydantic_ai.toolsets import FunctionToolset
    from pydantic_ai.usage import UsageLimits

    from backend.app.agents.text_generation import write_text
    from backend.app.text_generation_analysis import GenerationRequest

    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a" * 32)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "synthetic-cloudflare-secret")
    payloads = []
    checked = []
    tools = FunctionToolset()

    @tools.tool_plain
    def check_text_difficulty(title: str, paragraphs: list[str]) -> str:
        checked.append((title, paragraphs))
        return '{"status":"pass"}'

    async def respond(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        assert "response_format" not in payload
        if len(payloads) <= 4:
            assert any(t["function"]["name"] == "check_text_difficulty" for t in payload["tools"])
            name, arguments = "check_text_difficulty", {
                "title": "A Bag", "paragraphs": ["We found a bag."]
            }
        else:
            output = next(t["function"] for t in payload["tools"]
                          if "kind" in t["function"]["parameters"].get("properties", {}))
            assert output["strict"] is True
            name = output["name"]
            arguments = {"kind": "draft", "title": "A Bag",
                         "paragraphs": [{"text": "We found a bag."}]}
        return httpx2.Response(200, json={
            "id": "writer-roundtrip", "object": "chat.completion", "created": 1,
            "model": cloudflare.MODEL,
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": f"writer-{len(payloads)}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }],
            }}],
        })

    transport = cloudflare.CloudflareTransport
    monkeypatch.setattr(
        cloudflare, "CloudflareTransport", lambda: transport(httpx2.MockTransport(respond))
    )
    async with cloudflare.cloudflare_model() as model:
        async def run():
            return await write_text(
                model=model, settings=GenerationRequest(),
                profile={"profile_sufficiency": "provisional"}, toolsets=[tools],
                usage_limits=UsageLimits(request_limit=request_limit),
            )

        if request_limit == 4:
            from pydantic_ai.exceptions import UsageLimitExceeded

            with pytest.raises(UsageLimitExceeded):
                await run()
        else:
            assert (await run()).kind == "draft"
    assert checked == [("A Bag", ["We found a bag."])] * 4
    assert len(payloads) == request_limit
