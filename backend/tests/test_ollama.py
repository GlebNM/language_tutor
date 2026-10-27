import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior

from backend.app.ai_connection import create_ai_router, ollama
from backend.app.ai_connection.service import ConnectionService
from backend.app.ai_connection.settings import AISettings, AISettingsStore
from backend.app.ai_connection.siwc import SIWCOAuthFlow


def test_temporary_model_preserves_subscription_choice_and_requires_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    store = AISettingsStore(tmp_path)
    store.write(AISettings("gpt-6.1-sol", "high"))
    app = FastAPI()
    app.include_router(create_ai_router(data_dir=tmp_path, mcp_url="http://mcp:8001/mcp"))
    with TestClient(app) as client:
        missing = client.put("/api/ai/temporary-model", json={"enabled": True})
        assert missing.status_code == 409
        assert "OLLAMA_API_KEY" in missing.json()["detail"]
        assert not store.read().temporary_model
        monkeypatch.setenv("OLLAMA_API_KEY", "synthetic-ollama-secret")
        enabled = client.put("/api/ai/temporary-model", json={"enabled": True})
        assert enabled.status_code == 200
        assert enabled.json() == {
            "model": "gpt-6.1-sol", "reasoning_effort": "high",
            "temporary_model": True, "temporary_model_available": True,
        }
        assert "synthetic-ollama-secret" not in enabled.text
        assert store.read().effective() == AISettings("gemma4:31b", None, True)
        # A different learner uses the same server key with independent settings.
        assert not AISettingsStore(tmp_path / "other-learner").read().temporary_model
        monkeypatch.delenv("OLLAMA_API_KEY")
        assert client.put("/api/ai/temporary-model", json={"enabled": False}).status_code == 200
        assert store.read().effective() == AISettings("gpt-6.1-sol", "high")
    assert "synthetic-ollama-secret" not in store.path.read_text()


async def test_ollama_provider_sends_authenticated_cloud_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_API_KEY", "synthetic-ollama-secret")
    requests = []

    async def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert str(request.url) == "https://ollama.com/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer synthetic-ollama-secret"
        payload = json.loads(request.content)
        assert payload["model"] == "gemma4:31b"
        assert "reasoning_effort" not in payload
        return httpx2.Response(200, json={
            "id": "ollama-test", "object": "chat.completion", "created": 1,
            "model": "gemma4:31b",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello!"},
                         "finish_reason": "stop"}],
        })

    def controlled_client(**kwargs: object) -> AsyncOpenAI:
        return AsyncOpenAI(
            **kwargs, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
        )

    monkeypatch.setattr(ollama, "AsyncOpenAI", controlled_client)
    async with ollama.ollama_model() as model:
        client = model._provider.client
        result = await Agent(model, name="ollama_connection_test").run("Say hello.")
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
    monkeypatch.setenv("OLLAMA_API_KEY", "synthetic-ollama-secret")
    service = connection(tmp_path)
    original = AISettings("gpt-6.1-sol", "high")
    service.settings_store.write(AISettings(original.model, original.reasoning_effort, True))
    assert service.available
    started, resume = asyncio.Event(), asyncio.Event()

    async def operation(model):
        assert model.model_name == "gemma4:31b"
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
    monkeypatch.setenv("OLLAMA_API_KEY", "synthetic-ollama-secret")
    service = connection(tmp_path)
    service.settings_store.write(AISettings("gpt-6-luna", "low", True))

    async def failing(model):
        raise UnexpectedModelBehavior("synthetic-private-prompt")

    with pytest.raises(HTTPException) as error:
        await service.execute(failing)
    assert error.value.status_code == 503
    assert "error_type=UnexpectedModelBehavior" in caplog.text
    assert "synthetic-" not in caplog.text + error.value.detail
    await service.close()


@pytest.mark.parametrize(
    "provider_status,expected", [(401, 403), (403, 403), (429, 429), (500, 503)]
)
async def test_ollama_errors_preserve_subscription_state_and_hide_provider_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_status: int, expected: int,
) -> None:
    monkeypatch.setenv("OLLAMA_API_KEY", "synthetic-ollama-secret")
    service = connection(tmp_path)
    service.settings_store.write(AISettings("gpt-6-luna", "low", True))

    async def failing(model):
        raise ModelHTTPError(provider_status, model.model_name, body="synthetic-private-detail")

    with pytest.raises(HTTPException) as error:
        await service.execute(failing)
    assert error.value.status_code == expected
    assert "Ollama Cloud" in error.value.detail
    assert "synthetic-" not in error.value.detail
    assert service.state.status == "disconnected"
    await service.close()
