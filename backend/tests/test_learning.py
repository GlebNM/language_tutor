"""Public learning API with controlled models and a real Streamable HTTP MCP server."""

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
from fastapi.testclient import TestClient
from pydantic_ai import models
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from backend.app.main import create_app


@pytest.fixture(autouse=True)
def forbid_live_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)


@pytest.fixture
def learning_mcp(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {**os.environ, "MCP_HOST": "127.0.0.1", "MCP_PORT": str(port), "DATA_DIR": str(tmp_path)}
    process = subprocess.Popen(
        [sys.executable, "-m", "backend.mcp_server"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        for _ in range(100):
            assert process.poll() is None
            try:
                httpx.get(url, timeout=0.1)
                break
            except httpx.TransportError:
                time.sleep(0.02)
        else:
            pytest.fail("MCP did not become ready")
        yield url, tmp_path / "learning.sqlite3"
    finally:
        process.terminate()
        process.wait(timeout=5)


def import_selection(client: TestClient, *, text: str = "She gave up smoking.") -> dict[str, str]:
    response = client.post(
        "/api/materials",
        json={
            "youtube_url": "https://youtu.be/TLkA0RELQ1g",
            "title": "Context example",
            "content": f"1\n00:00:01,000 --> 00:00:04,000\n{text}\n",
            "format": "srt",
        },
    )
    assert response.status_code == 201, response.text
    material = response.json()
    cue = material["cues"][0]
    token = cue["tokens"][1]
    return {"material_id": material["id"], "cue_id": cue["id"], "token_id": token["id"]}


def test_real_context_card_reuses_context_cache_and_rejects_mismatched_ids(
    learning_mcp: tuple[str, Path],
) -> None:
    from backend.app.ai_connection.settings import AISettings, AISettingsStore
    from backend.app.learning import create_learning_router

    mcp_url, database = learning_mcp
    selected: dict[str, str] = {}
    calls = 0

    effort = "low"

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        assert "She gave up smoking." in str(messages)
        assert info.model_settings and info.model_settings["openai_reasoning_effort"] == effort
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "translation": "бросила",
                        "explanation": "Give up означает прекратить привычку.",
                        "expression": "give up",
                        "example": "He gave up smoking last year.",
                    },
                )
            ]
        )

    app = create_app(
        database_path=database,
        seed_demo=False,
        include_ai=False,
        frontend_dist=database.parent / "no-frontend",
    )

    class ControlledConnection:
        settings_store = AISettingsStore(database.parent)

        async def execute(self, operation, *, settings=None):
            return await operation(FunctionModel(respond))

    app.include_router(
        create_learning_router(
            library=app.state.materials,
            connection=ControlledConnection(),
            mcp_url=mcp_url,
        )
    )
    with TestClient(app, base_url="http://localhost") as client:
        selected.update(import_selection(client))
        response = client.post("/api/cards", json=selected)
        assert response.status_code == 200, response.text
        assert response.json()["card"]["translation"] == "бросила"
        assert response.json()["reasoning_effort"] == "low"
        assert response.json()["tools"][0]["name"] == "get_subtitle_context"
        again = client.post("/api/cards", json=selected)
        assert again.json()["cached"] is True
        assert calls == 1
        effort = "medium"
        ControlledConnection.settings_store.write(AISettings("gpt-6-luna", effort))
        updated = client.post("/api/cards", json=selected)
        assert updated.status_code == 200, updated.text
        assert updated.json()["cached"] is False
        assert updated.json()["reasoning_effort"] == "medium"
        assert calls == 2
        refreshed = client.post("/api/cards", json={**selected, "force_refresh": True})
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["cached"] is False
        assert calls == 3
        wrong = client.post("/api/cards", json={**selected, "cue_id": "wrong"})
        assert wrong.status_code == 404


async def test_queued_chat_keeps_settings_captured_before_conversation_lock(
    learning_mcp: tuple[str, Path],
    tmp_path: Path,
) -> None:
    from backend.app.agents.learning import ChatAnswer
    from backend.app.ai_connection.settings import AISettings, AISettingsStore
    from backend.app.learning import ChatInput, LearningService, SelectionInput

    mcp_url, _ = learning_mcp
    database = tmp_path / "learning.sqlite3"
    app = create_app(database_path=database, seed_demo=False, include_ai=False)
    with TestClient(app, base_url="http://localhost") as client:
        selection = import_selection(client)

    class ControlledConnection:
        settings_store = AISettingsStore(tmp_path)

        def __init__(self) -> None:
            self.used_settings: list[AISettings] = []

        async def execute(self, operation, *, settings=None):
            self.used_settings.append(settings)
            return ChatAnswer(answer="Ответ готов."), 0, settings.model

    connection = ControlledConnection()
    connection.settings_store.write(AISettings("gpt-6-luna", "low"))
    service = LearningService(
        library=app.state.materials,
        connection=connection,
        mcp_url=mcp_url,
    )
    conversation = service.create_conversation(SelectionInput(**selection))
    lock = service.locks.setdefault("conversation:" + conversation["id"], asyncio.Lock())
    await lock.acquire()
    task = asyncio.create_task(
        service.message(
            conversation["id"], ChatInput(message="Поясни это", request_id="queued-turn-1")
        )
    )
    await asyncio.sleep(0)
    assert not task.done()
    connection.settings_store.write(AISettings("gpt-6-sol", "high"))
    lock.release()
    response = await task
    assert connection.used_settings == [AISettings("gpt-6-luna", "low")]
    assert response["model"] == "gpt-6-luna"
    assert response["reasoning_effort"] == "low"


def test_chat_persists_retries_and_builds_verified_citations_after_track_replacement(
    learning_mcp: tuple[str, Path],
) -> None:
    import json

    from backend.app.learning import create_learning_router

    mcp_url, database = learning_mcp
    selected: dict[str, str] = {}
    attempts = 0
    forge_once = True

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal attempts, forge_once
        attempts += 1
        if attempts > 3:
            assert any(
                isinstance(part, TextPart) and "He gave up smoking." in part.content
                for message in messages
                for part in message.parts
            )
        returned = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == "find_word_occurrences"
        ]
        if not returned:
            assert "get_proof_context" not in [tool.name for tool in info.function_tools]
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "find_word_occurrences",
                        {
                            "language": "en",
                            "lemma": "give",
                            "part_of_speech": "VERB",
                            "limit": 5,
                        },
                    )
                ]
            )
        sources = json.loads(str(returned[-1].content))["occurrences"]
        assert len(sources) == 1
        citation = "fabricated:cue" if forge_once else sources[0]["citation_id"]
        forge_once = False
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "answer": "В вашем материале это слово входит в выражение give up.",
                        "educational_example": "He gave up smoking.",
                        "citation_ids": [citation],
                    },
                )
            ]
        )

    app = create_app(
        database_path=database,
        seed_demo=False,
        include_ai=False,
        frontend_dist=database.parent / "no-frontend",
    )
    app.include_router(
        create_learning_router(
            library=app.state.materials,
            connection=None,
            mcp_url=mcp_url,
            model_factory=lambda: FunctionModel(respond),
        )
    )
    with TestClient(app, base_url="http://localhost") as client:
        selected.update(import_selection(client))
        conversation = client.post("/api/conversations", json=selected).json()
        path = f"/api/conversations/{conversation['id']}/messages"
        incoming = {
            "message": "Где это слово встречалось в моих материалах?",
            "request_id": "citation-test-1",
        }
        response = client.post(path, json=incoming)
        assert response.status_code == 200, response.text
        assert response.json()["reasoning_effort"] == "low"
        assert response.json()["model"]
        quote = response.json()["citations"][0]
        assert quote["text"] == "She gave up smoking."
        assert quote["start_ms"] == 1000
        assert quote["youtube_url"] == "https://www.youtube.com/watch?v=TLkA0RELQ1g&t=1"
        assert quote["material_id"] == selected["material_id"]
        assert attempts == 3  # The fabricated citation was refused and corrected.
        again = client.post(path, json=incoming)
        assert again.json() == response.json()
        assert attempts == 3
        assert client.post(path, json={**incoming, "message": "Другой вопрос"}).status_code == 409
        assert client.post(path, json={"message": "x" * 4001}).status_code == 422
        replaced = client.put(
            f"/api/materials/{selected['material_id']}/track",
            json={
                "youtube_url": "https://youtu.be/TLkA0RELQ1g",
                "title": "New version",
                "content": "1\n00:00:02,000 --> 00:00:05,000\nShe is still smoking.\n",
                "format": "srt",
            },
        )
        assert replaced.status_code == 200
        assert replaced.json()["id"] != selected["material_id"]
        reopened = client.get(f"/api/conversations/{conversation['id']}").json()
        assert len(reopened["messages"]) == 1
        assert reopened["messages"][0]["response"]["citations"][0] == quote
        old = client.get(f"/api/materials/{selected['material_id']}").json()
        assert old["cues"][0]["text"] == quote["text"]
        assert (
            client.get(
                "/api/conversations",
                params={
                    "material_id": selected["material_id"],
                },
            ).json()[0]["id"]
            == conversation["id"]
        )

        followup = client.post(
            path, json={"message": "Объясни предыдущий пример", "request_id": "citation-test-2"}
        )
        assert followup.status_code == 200, followup.text


async def test_mcp_exact_language_pos_matching_bounds_and_neighbor_validation(
    learning_mcp: tuple[str, Path],
) -> None:
    import json

    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    mcp_url, database = learning_mcp
    app = create_app(
        database_path=database,
        seed_demo=False,
        include_ai=False,
        frontend_dist=database.parent / "no-frontend",
    )
    with TestClient(app, base_url="http://localhost") as client:
        selected = import_selection(client)
    async with Client(mcp_url) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert {
            "get_learning_profile",
            "get_word_knowledge",
            "get_subtitle_context",
            "find_word_occurrences",
            "get_diagnostic_items",
        } <= names
        args = {"language": "en", "lemma": "give", "part_of_speech": "VERB", "limit": 5}
        result = await client.call_tool("find_word_occurrences", args)
        payload = json.loads(result.structured_content["result"])
        assert len(payload["occurrences"]) == 1
        assert payload["occurrences"][0]["text"] == "She gave up smoking."
        assert len(result.structured_content["result"]) <= 6000
        for mismatch in [
            {"lemma": "gave"},
            {"lemma": "Give"},
            {"language": "ru"},
            {"part_of_speech": "NOUN"},
            {"lemma": "zzzz-no-match"},
        ]:
            result = await client.call_tool("find_word_occurrences", {**args, **mismatch})
            assert json.loads(result.structured_content["result"])["occurrences"] == []
        with pytest.raises(ToolError):
            await client.call_tool("find_word_occurrences", {**args, "limit": 6})
        with pytest.raises(ToolError):
            await client.call_tool(
                "get_subtitle_context",
                {
                    "material_id": selected["material_id"],
                    "cue_id": selected["cue_id"],
                    "neighbors": 3,
                },
            )
        with pytest.raises(ToolError):
            await client.call_tool(
                "get_subtitle_context",
                {
                    "material_id": "wrong-material",
                    "cue_id": selected["cue_id"],
                },
            )


async def test_data_reset_during_model_response_cannot_restore_card_or_chat(
    learning_mcp: tuple[str, Path],
) -> None:
    import asyncio

    from backend.app.learning import create_learning_router

    mcp_url, database = learning_mcp
    ready, release = asyncio.Event(), asyncio.Event()
    selected: dict[str, str] = {}

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert "She gave up smoking." in str(messages)
        ready.set()
        await release.wait()
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "translation": "бросила",
                        "explanation": "Это выражение give up.",
                        "expression": "give up",
                        "example": "He gave up smoking.",
                    },
                )
            ]
        )

    app = create_app(
        database_path=database,
        seed_demo=False,
        include_ai=False,
        frontend_dist=database.parent / "no-frontend",
    )
    app.include_router(
        create_learning_router(
            library=app.state.materials,
            connection=None,
            mcp_url=mcp_url,
            model_factory=lambda: FunctionModel(respond),
        )
    )
    with TestClient(app, base_url="http://localhost") as client:
        selected.update(import_selection(client))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        pending = asyncio.create_task(client.post("/api/cards", json=selected))
        await asyncio.wait_for(ready.wait(), timeout=10)
        reset = await client.post("/api/data/reset", json={"confirmation": "Удалить мои данные"})
        assert reset.status_code == 200
        release.set()
        stale = await pending
        assert stale.status_code == 409
        assert (await client.get("/api/materials")).json() == []
        assert (await client.post("/api/cards", json=selected)).status_code == 404


async def test_concurrent_chat_retries_are_idempotent_and_no_matches_are_explicit(
    learning_mcp: tuple[str, Path],
) -> None:
    import asyncio

    from backend.app.learning import create_learning_router

    mcp_url, database = learning_mcp
    model_turns = 0

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal model_turns
        model_turns += 1
        await asyncio.sleep(0.01)
        returned = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == "find_word_occurrences"
        ]
        if not returned:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "find_word_occurrences",
                        {
                            "language": "en",
                            "lemma": "zzzz-no-match",
                            "part_of_speech": "NOUN",
                            "limit": 5,
                        },
                    )
                ]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "answer": "Для этого слова сохранённого контекста нет.",
                        "citation_ids": [],
                    },
                )
            ]
        )

    app = create_app(
        database_path=database,
        seed_demo=False,
        include_ai=False,
        frontend_dist=database.parent / "no-frontend",
    )
    app.include_router(
        create_learning_router(
            library=app.state.materials,
            connection=None,
            mcp_url=mcp_url,
            model_factory=lambda: FunctionModel(respond),
        )
    )
    with TestClient(app, base_url="http://localhost") as client:
        selected = import_selection(client)
        first = client.post("/api/conversations", json=selected).json()["id"]
        second = client.post("/api/conversations", json=selected).json()["id"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        incoming = {"message": "Где встречалось zzzz-no-match?", "request_id": "retry-test-123"}
        replies = await asyncio.gather(
            client.post(f"/api/conversations/{first}/messages", json=incoming),
            client.post(f"/api/conversations/{first}/messages", json=incoming),
            client.post(f"/api/conversations/{second}/messages", json=incoming),
        )
        assert all(reply.status_code == 200 for reply in replies)
        assert replies[0].json() == replies[1].json()
        assert replies[0].json()["conversation_id"] != replies[2].json()["conversation_id"]
        assert replies[0].json()["citations"] == []
        assert "точных совпадений не найдено" in replies[0].json()["answer"]
        assert model_turns == 4
        for conversation_id in [first, second]:
            reopened = (await client.get(f"/api/conversations/{conversation_id}")).json()
            assert len(reopened["messages"]) == 1


def test_chat_stops_after_three_sequential_tool_rounds(
    learning_mcp: tuple[str, Path],
) -> None:
    from backend.app.learning import create_learning_router

    mcp_url, database = learning_mcp
    turns = 0

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal turns
        turns += 1
        return ModelResponse(parts=[ToolCallPart("get_learning_profile", {"language": "en"})])

    app = create_app(
        database_path=database,
        seed_demo=False,
        include_ai=False,
        frontend_dist=database.parent / "no-frontend",
    )
    app.include_router(
        create_learning_router(
            library=app.state.materials,
            connection=None,
            mcp_url=mcp_url,
            model_factory=lambda: FunctionModel(respond),
        )
    )
    with TestClient(app, base_url="http://localhost") as client:
        selected = import_selection(client)
        conversation = client.post("/api/conversations", json=selected).json()["id"]
        response = client.post(
            f"/api/conversations/{conversation}/messages",
            json={
                "message": "Объясни слово",
                "request_id": "budget-test-123",
            },
        )
        assert response.status_code == 422
        assert "трёх раундов" in response.json()["detail"]
        assert turns == 4
        reopened = client.get(f"/api/conversations/{conversation}").json()
        assert len(reopened["messages"]) == 1
        assert reopened["messages"][0]["response"] is None


async def test_card_prefetch_uses_one_siwc_inference_with_low_reasoning(
    learning_mcp: tuple[str, Path],
    tmp_path: Path,
) -> None:
    import json

    import httpx2
    from openai import AsyncOpenAI
    from pydantic_ai.providers.openai import OpenAIProvider

    from backend.app.ai_connection.siwc import TokenManager
    from backend.app.ai_connection.siwc_credentials import SIWCCredentials
    from backend.app.ai_connection.siwc_model import SIWCTransport, siwc_model
    from backend.app.ai_connection.storage import CredentialStore
    from backend.app.learning import create_learning_router

    mcp_url, database = learning_mcp
    store = CredentialStore(tmp_path / "credentials")
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
    calls = 0

    async def remote(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        assert body["reasoning"]["effort"] == "low"
        assert "She gave up smoking." in str(body["input"])
        tools = body["input"][0]["tools"]
        assert "get_subtitle_context" not in [tool["name"] for tool in tools]
        output_name = next(
            tool["name"]
            for tool in tools
            if "translation" in tool["parameters"].get("properties", {})
        )
        item = {
            "id": "fc_card",
            "type": "function_call",
            "call_id": "call_card",
            "name": output_name,
            "status": "completed",
            "arguments": json.dumps(
                {
                    "translation": "бросила",
                    "explanation": "Здесь give up — бросить привычку.",
                    "expression": "give up",
                    "example": "He gave up smoking.",
                }
            ),
        }
        response = {
            "id": "resp_card",
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
            content="".join(f"data: {json.dumps(e)}\n\n" for e in events).encode(),
        )

    async with httpx2.AsyncClient(
        transport=SIWCTransport(
            TokenManager(store),
            transport=httpx2.MockTransport(remote),
        )
    ) as sdk_client:
        provider = OpenAIProvider(
            openai_client=AsyncOpenAI(
                api_key="subscription-oauth",
                http_client=sdk_client,
                max_retries=0,
            )
        )
        app = create_app(
            database_path=database,
            seed_demo=False,
            include_ai=False,
            frontend_dist=database.parent / "no-frontend",
        )
        app.include_router(
            create_learning_router(
                library=app.state.materials,
                connection=None,
                mcp_url=mcp_url,
                model_factory=lambda: siwc_model("test-siwc", provider),
            )
        )
        with TestClient(app, base_url="http://localhost") as client:
            selected = import_selection(client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            with models.override_allow_model_requests(True):
                result = await client.post("/api/cards", json=selected)
        assert result.status_code == 200, result.text
        assert result.json()["card"]["translation"] == "бросила"
        assert result.json()["model_requests"] == 1
        assert result.json()["tools"][0]["name"] == "get_subtitle_context"
        assert calls == 1
