"""Background text generation through public job operations with controlled AI."""

import json
import time

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from backend.app.main import create_learner_app
from backend.tests.test_learning import learning_mcp as learning_mcp
from backend.tests.test_text_generation_analysis import mark_vocabulary


def test_generation_settings_are_available_without_connecting_ai(tmp_path):
    app = create_learner_app(tmp_path / "learning.sqlite3", seed_demo=False, include_ai=False)
    with TestClient(app, base_url="http://localhost") as client:
        settings = client.get("/api/text-generation/settings").json()
        assert settings["available"] is False
        assert settings["difficulty_limits"]["gentle"]["new_or_unassessed_ratio_max"] == 0.01
        assert settings["difficulty_limits"]["normal"]["new_or_unassessed_ratio_max"] == 0.03
        assert settings["difficulty_limits"]["rich"]["new_or_unassessed_ratio_max"] == 0.05
        response = client.post("/api/text-generation/jobs", json={})
        assert response.status_code == 409
        assert client.get("/api/text-generation/jobs").json() == {"jobs": []}


def job_client(database, mcp_url, model):
    app = create_learner_app(database, seed_demo=False, include_ai=False)
    service = app.state.text_generation
    service.model_factory = lambda: model
    service.mcp_url = mcp_url
    return TestClient(app, base_url="http://localhost")


def wait_job(client, identity):
    for _ in range(500):
        response = client.get(f"/api/text-generation/jobs/{identity}")
        assert response.status_code == 200, response.text
        result = response.json()
        if result["status"] not in {
            "queued",
            "preparing",
            "writing",
            "checking",
            "revising",
            "saving",
        }:
            return result
        time.sleep(0.02)
    pytest.fail("Generation did not complete within the controlled-model test budget")


def controlled_writer(*, weak_once=False, weak_always=False, lookup=False, too_many_unknown=False):
    writer_inputs = []
    tool_results = []
    context_runs = []

    def respond(messages, info):
        payload = json.loads(
            next(
                part.content
                for message in reversed(messages)
                for part in message.parts
                if isinstance(part, UserPromptPart)
            )
        )
        if "profile" in payload:
            returns = [
                part
                for message in messages
                for part in message.parts
                if isinstance(part, ToolReturnPart)
            ]
            if lookup and not any(part.tool_name == "find_texts_with_word" for part in returns):
                return ModelResponse(parts=[ToolCallPart("find_texts_with_word", {"word": "read"})])
            if lookup and not any(part.tool_name == "get_text" for part in returns):
                found = next(part for part in returns if part.tool_name == "find_texts_with_word")
                data = (
                    json.loads(found.content) if isinstance(found.content, str) else found.content
                )
                tool_results.append(data)
                material_id = data["texts"][0]["material_id"]
                return ModelResponse(parts=[ToolCallPart("get_text", {"material_id": material_id})])
            tool_results.extend(part.content for part in returns if part.tool_name == "get_text")
            writer_inputs.append(payload)
            changing = weak_once and len(writer_inputs) > 1
            text = "We read books. " * 49 + ("We read books." if changing else "We borrow books.")
            if too_many_unknown:
                text = "We read books. " * 46 + "We borrow books. " * 4
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        info.output_tools[0].name,
                        {
                            "kind": "draft",
                            "title": "Books",
                            "paragraphs": [{"text": text}],
                        },
                    )
                ]
            )
        draft = payload["draft"]
        identity = {
            key: draft[key] for key in ("draft_id", "revision", "draft_hash", "snapshot_id")
        }
        if "insufficient_vocabulary" in payload:
            context_runs.append(payload)
            weak = weak_always or weak_once and len(context_runs) == 1
            checks = []
            for word in payload["insufficient_vocabulary"]:
                for occurrence in word["occurrences"]:
                    checks.append(
                        {
                            "block_id": occurrence["block_id"],
                            "token_ids": occurrence["token_ids"],
                            "context_status": "weak" if weak else "clear",
                            "explanation": "Контекст нужно раскрыть."
                            if weak
                            else "Контекст понятен.",
                            "repair_instructions": "Замените фразу или добавьте подсказку."
                            if weak
                            else None,
                            "evidence": [
                                {"block_id": occurrence["block_id"], "quote": "We borrow books."}
                            ],
                        }
                    )
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        info.output_tools[0].name,
                        {
                            **identity,
                            "context_checks": checks,
                        },
                    )
                ]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        **identity,
                        "checks": [
                            {
                                "criterion_id": criterion,
                                "status": "pass",
                                "explanation": "Соответствует.",
                                "evidence": [
                                    {
                                        "block_id": draft["paragraphs"][0]["id"],
                                        "quote": "We read books.",
                                    }
                                ],
                            }
                            for criterion in payload["required_criteria"]
                        ],
                    },
                )
            ]
        )

    return FunctionModel(respond), writer_inputs, tool_results


def prepare_known_words(client, borrow_score=1):
    mark_vocabulary(
        client,
        "We read books. We borrow books.",
        {
            "we": 3,
            "read": 2,
            "book": 2,
            "borrow": borrow_score,
        },
    )


def test_real_mcp_job_saves_once_and_writer_reads_existing_texts(learning_mcp):
    mcp_url, database = learning_mcp
    model, writer_inputs, tools = controlled_writer(lookup=True)
    with job_client(database, mcp_url, model) as client:
        prepare_known_words(client)
        original = client.post(
            "/api/materials/text",
            json={
                "title": "An Evening",
                "content": "We read books.",
            },
        )
        assert original.status_code == 201, original.text
        before = len(client.get("/api/materials").json())
        request = {"length": "short", "difficulty": "gentle"}
        started = client.post(
            "/api/text-generation/jobs", json=request, headers={"Idempotency-Key": "same-request"}
        )
        assert started.status_code == 202, started.text
        job = wait_job(client, started.json()["id"])
        assert job["status"] == "completed", job
        assert client.get("/api/text-generation/jobs").json()["jobs"][0]["id"] == job["id"]
        material = client.get(f"/api/materials/{job['result_material_id']}").json()
        assert material["kind"] == "text" and material["source"] == "generated"
        assert material["youtube_video_id"] is None
        assert len(client.get("/api/materials").json()) == before + 1
        repeated = client.post(
            "/api/text-generation/jobs", json=request, headers={"Idempotency-Key": "same-request"}
        )
        assert repeated.json()["id"] == job["id"]
        assert repeated.json()["result_material_id"] == material["id"]
        assert len(client.get("/api/materials").json()) == before + 1
        assert writer_inputs[0]["profile"]["snapshot_id"] == job["analysis"]["snapshot_id"]
        assert tools and original.json()["id"] in str(tools)


def test_multiround_writer_outlives_a_single_request_deadline(learning_mcp, monkeypatch):
    import asyncio

    mcp_url, database = learning_mcp
    base_model, _, _ = controlled_writer(lookup=True)
    assert base_model.function is not None
    original_timeout = asyncio.timeout

    def shortened_request_timeout(delay):
        return original_timeout(0.3 if delay == 30 else delay)

    monkeypatch.setattr(asyncio, "timeout", shortened_request_timeout)

    async def respond(messages, info):
        payload = json.loads(next(part.content for message in reversed(messages)
                                  for part in message.parts if isinstance(part, UserPromptPart)))
        if "profile" in payload:
            await asyncio.sleep(0.18)
        return base_model.function(messages, info)

    with job_client(database, mcp_url, FunctionModel(respond)) as client:
        prepare_known_words(client)
        original = client.post("/api/materials/text", json={
            "title": "An Evening", "content": "We read books.",
        })
        assert original.status_code == 201
        started = client.post("/api/text-generation/jobs", json={"length": "short"})
        assert started.status_code == 202
        result = wait_job(client, started.json()["id"])
        assert result["status"] == "completed", json.dumps(result, ensure_ascii=False)
        assert client.get(f"/api/materials/{result['result_material_id']}").json()["kind"] == "text"


def test_dictionary_search_leaves_budget_for_context_revision_and_publication(learning_mcp):
    mcp_url, database = learning_mcp
    base_model, writer_inputs, _ = controlled_writer(weak_once=True)
    assert base_model.function is not None
    dictionary_pages = []

    def respond(messages, info):
        payload = json.loads(next(part.content for message in reversed(messages)
                                  for part in message.parts if isinstance(part, UserPromptPart)))
        if "profile" in payload:
            dictionary_pages.extend(
                json.loads(part.content)
                for message in messages for part in message.parts
                if isinstance(part, ToolReturnPart)
                and part.tool_name == "list_generation_vocabulary"
            )
            if any(tool.name == "list_generation_vocabulary" for tool in info.function_tools):
                return ModelResponse(parts=[ToolCallPart("list_generation_vocabulary", {
                    "job_id": "untrusted-job", "category": "known", "limit": 2,
                })])
        return base_model.function(messages, info)

    with job_client(database, mcp_url, FunctionModel(respond)) as client:
        prepare_known_words(client)
        started = client.post("/api/text-generation/jobs", json={"length": "short"})
        assert started.status_code == 202
        result = wait_job(client, started.json()["id"])
        assert result["status"] == "completed", {
            "status": result["status"], "error": result["error"], "usage": result["usage"],
        }
        assert result["revision"] == 2
        assert writer_inputs[1]["feedback"]["blocking_issues"]
        assert dictionary_pages and dictionary_pages[0]["items"]
        assert result["usage"]["model_requests"] <= 18
        assert client.get(f"/api/materials/{result['result_material_id']}").json()["kind"] == "text"


def test_job_deadline_still_stops_a_writer_with_a_longer_operation_budget(learning_mcp):
    import asyncio
    from threading import Event

    entered = Event()
    mcp_url, database = learning_mcp

    async def respond(messages, info):
        entered.set()
        await asyncio.sleep(5)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            "kind": "draft", "title": "Books",
            "paragraphs": [{"text": "We read books. " * 50}],
        })])

    client = job_client(database, mcp_url, FunctionModel(respond))
    client.app.state.text_generation.timeout_seconds = 2
    with client:
        prepare_known_words(client)
        before = len(client.get("/api/materials").json())
        started = client.post("/api/text-generation/jobs", json={"length": "short"})
        assert started.status_code == 202
        assert entered.wait(3)
        result = wait_job(client, started.json()["id"])
        assert result["status"] == "needs_changes", result
        assert "Истекло время" in result["error"]
        assert len(client.get("/api/materials").json()) == before


@pytest.mark.parametrize(("failure", "expected_status"), [
    ("timeout", "needs_changes"), ("requests", "needs_changes"), ("provider", "failed"),
])
def test_subscription_budget_failure_keeps_a_retriable_draft(
    learning_mcp, monkeypatch, failure, expected_status,
):
    import asyncio

    import httpx2
    from openai import AsyncOpenAI
    from pydantic_ai import models
    from pydantic_ai.providers.openai import OpenAIProvider

    from backend.app.ai_connection.service import ConnectionStatus
    from backend.app.ai_connection.siwc import TokenManager
    from backend.app.ai_connection.siwc_credentials import SIWCCredentials
    from backend.app.ai_connection.siwc_model import SIWCTransport

    mcp_url, database = learning_mcp
    app = create_learner_app(database, seed_demo=False, include_ai=True)
    service = app.state.text_generation
    service.mcp_url = mcp_url
    if failure == "requests":
        service.model_request_limit = 1
    connection = app.state.connection
    asyncio.run(connection.store.save(SIWCCredentials(
        access_token="test-access", refresh_token="test-refresh", id_token="test-id",
        client_id="oaiapp_test", subject="learner", scopes=["chatgpt.tokens.use.direct"],
        expires_at=time.time() + 3600,
    )))
    connection.state = ConnectionStatus(status="connected")
    original_timeout = asyncio.timeout

    def shortened_operation_timeout(delay):
        return original_timeout(0.2 if delay == 30 else delay)

    monkeypatch.setattr(asyncio, "timeout", shortened_operation_timeout)

    async def remote(request):
        if request.url.path == "/v1/models":
            return httpx2.Response(200, json={"models": [{
                "slug": "gpt-6-luna", "visibility": "list",
                "supported_reasoning_levels": [{"effort": "low"}],
            }]})
        body = json.loads(request.content)
        tools = body["input"][0]["tools"]
        writer = next((tool for tool in tools
                       if {"kind", "paragraphs"} <= set(
                           tool["parameters"].get("properties", {}))), None)
        if writer is None:
            if failure == "provider":
                return httpx2.Response(422, json={"error": {
                    "message": "Synthetic provider validation error",
                    "type": "invalid_request_error", "code": "invalid_payload",
                }})
            await asyncio.sleep(0.5)
            raise AssertionError("The subscription operation should have timed out")
        item = {"id": "fc_draft", "type": "function_call", "call_id": "call_draft",
                "name": writer["name"], "status": "completed", "arguments": json.dumps({
                    "kind": "draft", "title": "Books",
                    "paragraphs": [{"text": "We read books. " * 49 + "We borrow books."}],
                })}
        response = {"id": "resp_draft", "object": "response", "created_at": time.time(),
                    "model": "gpt-6-luna", "status": "completed", "output": [item]}
        events = [
            {"type": "response.output_item.added", "item": {**item, "arguments": ""},
             "output_index": 0, "sequence_number": 0},
            {"type": "response.output_item.done", "item": item,
             "output_index": 0, "sequence_number": 1},
            {"type": "response.completed", "response": response, "sequence_number": 2},
        ]
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"},
            content="".join(f"data: {json.dumps(e)}\n\n" for e in events).encode(),
        )

    sdk_client = httpx2.AsyncClient(transport=SIWCTransport(
        TokenManager(connection.store), transport=httpx2.MockTransport(remote),
    ))
    connection.http_client = sdk_client
    connection.provider = OpenAIProvider(openai_client=AsyncOpenAI(
        api_key="subscription-oauth", http_client=sdk_client, max_retries=0,
    ))
    with (
        TestClient(app, base_url="http://localhost") as client,
        models.override_allow_model_requests(True),
    ):
        prepare_known_words(client)
        before = len(client.get("/api/materials").json())
        started = client.post("/api/text-generation/jobs", json={"length": "short"})
        assert started.status_code == 202
        result = wait_job(client, started.json()["id"])
        assert result["status"] == expected_status, {
            "status": result["status"], "error": result["error"], "usage": result["usage"],
        }
        assert result["draft"]["title"] == "Books"
        assert result["analysis"]["metrics"]["word_count"] == 151
        assert result["result_material_id"] is None
        if failure == "timeout":
            assert "не ответил" in result["error"]
        elif failure == "requests":
            assert "ограничения числа раундов" in result["error"]
        assert len(client.get("/api/materials").json()) == before


def test_writer_reads_all_expression_meanings_from_its_snapshot_through_real_mcp(learning_mcp):
    import asyncio
    from threading import Event

    from backend.tests.test_expressions import analyze

    mcp_url, database = learning_mcp
    entered, release = Event(), Event()
    pages, profiles = [], []
    base_model, _, _ = controlled_writer()
    assert base_model.function is not None

    async def respond(messages, info):
        payload = json.loads(next(part.content for message in reversed(messages)
                                  for part in message.parts if isinstance(part, UserPromptPart)))
        if "profile" in payload:
            if not entered.is_set():
                profiles.append(payload["profile"])
                entered.set()
                while not release.is_set():
                    await asyncio.sleep(0.01)
            if any(tool.name == "list_generation_expressions" for tool in info.function_tools):
                returns = [part for message in messages for part in message.parts
                           if isinstance(part, ToolReturnPart)
                           and part.tool_name == "list_generation_expressions"]
                latest = None
                if returns:
                    latest = json.loads(returns[-1].content)
                    pages.append(latest)
                if latest is None or latest["next_offset"] is not None:
                    return ModelResponse(parts=[ToolCallPart("list_generation_expressions", {
                        "job_id": "forged-other-job", "limit": 30,
                        "offset": latest["next_offset"] if latest else 0,
                    })])
        return base_model.function(messages, info)

    def extract(messages, info):
        payload = json.loads(next(part.content for message in reversed(messages)
                                  for part in message.parts if isinstance(part, UserPromptPart)))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"expressions": [
            {"canonical": " ".join(token["text"].lower() for token in cue["tokens"][1:]),
             "kind": "phrasal_verb", "sense_key": "saved-meaning",
             "translation": "Значение выражения. " * 15,
             "explanation": "PRIVATE SOURCE DETAIL", "example": "PRIVATE SOURCE EXAMPLE",
             "token_ids": [token["id"] for token in cue["tokens"][1:]]}
            for cue in payload["cues"]
        ]})])

    phrases = ("give up", "turn off", "take off", "put on", "look after", "look for",
               "look up", "go on", "come back", "get up", "wake up", "sit down",
               "stand up", "put down", "pick up", "find out", "work out", "run out",
               "carry on", "bring back", "come in", "go out", "break down", "call off", "set up")
    with job_client(database, mcp_url, FunctionModel(respond)) as client:
        prepare_known_words(client)
        client.app.state.expression_analysis.model_factory = lambda: FunctionModel(extract)
        material = client.post("/api/materials/text", json={
            "title": "Expressions", "content": "\n\n".join(f"We {phrase}." for phrase in phrases),
        }).json()
        analyzed = analyze(client, material)
        assert analyzed["job"]["status"] == "completed", analyzed
        entries = {entry["id"]: entry for entry in analyzed["occurrences"]}
        assert len(entries) == 25
        for identity in entries:
            assert client.put(f"/api/expressions/{identity}/knowledge",
                              json={"score": 1}).status_code == 200
        started = client.post("/api/text-generation/jobs", json={"length": "short"}).json()
        assert entered.wait(3)
        changed = next(iter(entries))
        assert client.put(f"/api/expressions/{changed}/knowledge",
                          json={"score": 3}).status_code == 200
        release.set()
        job = wait_job(client, started["id"])
        assert job["status"] == "completed", job
        assert pages, "The generation writer must be able to retrieve expression meanings"
        meanings = [item for page in pages for item in page["items"]]
        assert {item["id"] for item in meanings} == set(entries)
        assert len(meanings) == 25
        assert len(profiles[0]["expression_examples"]) < 25
        assert all(page["snapshot_id"] == profiles[0]["snapshot_id"] for page in pages)
        assert all(item["familiarity"]["score"] == 1 for item in meanings)
        assert all(set(item) == {"id", "canonical", "sense_key", "translation", "familiarity"}
                   for item in meanings)
        assert all(len(json.dumps(page, ensure_ascii=False, separators=(",", ":"))) <= 6000
                   for page in pages)
        assert "PRIVATE SOURCE" not in json.dumps([profiles, pages])
        assert client.get(f"/api/expressions/{changed}").json()["knowledge"]["score"] == 3

        from fastmcp import Client
        from fastmcp.exceptions import ToolError

        from backend.app.accounts import AccountInput, AccountStore
        from backend.app.tenant_context import current_mcp_token

        accounts = AccountStore(database.parents[2])
        other = accounts.register(
            AccountInput(username="other-reader", password="test-password-123")
        )
        with accounts.connect() as db:
            other_token = db.execute("SELECT mcp_token FROM accounts WHERE id=?",
                                     (other["id"],)).fetchone()[0]

        async def check_scope():
            async with Client(mcp_url, auth=current_mcp_token.get()) as own:
                with pytest.raises(ToolError):
                    await own.call_tool("list_generation_expressions", {
                        "job_id": started["id"], "limit": 31,
                    })
            async with Client(mcp_url, auth=other_token) as foreign:
                with pytest.raises(ToolError):
                    await foreign.call_tool("list_generation_expressions", {
                        "job_id": started["id"],
                    })

        asyncio.run(check_scope())


def test_context_feedback_rewrites_without_fixed_vocabulary(learning_mcp):
    mcp_url, database = learning_mcp
    model, inputs, _ = controlled_writer(weak_once=True)
    with job_client(database, mcp_url, model) as client:
        prepare_known_words(client)
        started = client.post("/api/text-generation/jobs", json={"length": "short"}).json()
        job = wait_job(client, started["id"])
        assert job["status"] == "completed", job
        assert job["revision"] == 2
        assert inputs[-1]["feedback"]["blocking_issues"][0]["criterion_id"] == "context_support"
        assert "borrow" not in job["draft"]["paragraphs"][0]["text"]
        assert job["analysis"]["insufficient_vocabulary"] == []


def test_numeric_failure_cannot_be_overridden_by_successful_reviewers(learning_mcp):
    mcp_url, database = learning_mcp
    model, _, _ = controlled_writer(too_many_unknown=True)
    with job_client(database, mcp_url, model) as client:
        prepare_known_words(client, borrow_score=0)
        before = len(client.get("/api/materials").json())
        started = client.post("/api/text-generation/jobs", json={"length": "short"}).json()
        job = wait_job(client, started["id"])
        assert job["status"] == "needs_changes", job
        assert job["revision"] == 3
        assert job["result_material_id"] is None
        assert job["feedback"]["blocking_issues"][0]["criterion_id"] == "new_ratio"
        assert len(client.get("/api/materials").json()) == before


def test_cancelled_writer_cannot_publish_a_late_text_and_job_survives_restart(learning_mcp):
    import asyncio
    from threading import Event

    mcp_url, database = learning_mcp
    entered = Event()

    async def respond(messages, info):
        entered.set()
        await asyncio.sleep(60)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "kind": "draft",
                        "title": "Books",
                        "paragraphs": [{"text": "We read books. " * 50}],
                    },
                )
            ]
        )

    with job_client(database, mcp_url, FunctionModel(respond)) as client:
        prepare_known_words(client)
        before = len(client.get("/api/materials").json())
        started = client.post("/api/text-generation/jobs", json={"length": "short"})
        assert started.status_code == 202, started.text
        identity = started.json()["id"]
        assert entered.wait(3)
        stopped = client.delete(f"/api/text-generation/jobs/{identity}")
        assert stopped.json()["status"] == "cancelled"
        time.sleep(0.03)
        assert client.get(f"/api/text-generation/jobs/{identity}").json()["status"] == "cancelled"
        assert len(client.get("/api/materials").json()) == before
    model, _, _ = controlled_writer()
    with job_client(database, mcp_url, model) as restarted:
        restored = restarted.get(f"/api/text-generation/jobs/{identity}").json()
        assert restored["status"] == "cancelled"
        retry = restarted.post(f"/api/text-generation/jobs/{identity}/retry", json={})
        assert retry.status_code == 202
        assert retry.json()["id"] != identity
        result = wait_job(restarted, retry.json()["id"])
        assert result["status"] == "completed", result


def test_profile_change_is_rechecked_before_a_text_can_be_saved(learning_mcp):
    import asyncio
    from threading import Event

    mcp_url, database = learning_mcp
    model, _, _ = controlled_writer(too_many_unknown=True)
    entered, release = Event(), Event()
    respond = model.function
    assert respond is not None

    async def paused(messages, info):
        payload = json.loads(
            next(
                part.content
                for message in reversed(messages)
                for part in message.parts
                if isinstance(part, UserPromptPart)
            )
        )
        if "insufficient_vocabulary" in payload and not entered.is_set():
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
        return respond(messages, info)

    with job_client(database, mcp_url, FunctionModel(paused)) as client:
        prepare_known_words(client, borrow_score=1)
        borrow = next(
            entry for entry in client.get("/api/dictionary").json() if entry["lemma"] == "borrow"
        )
        started = client.post("/api/text-generation/jobs", json={"length": "short"}).json()
        assert entered.wait(3)
        changed = client.put(f"/api/lexemes/{borrow['lexeme_id']}/knowledge", json={"score": 0})
        assert changed.status_code == 200
        revision = client.get("/api/profile").json()["revision"]
        release.set()
        job = wait_job(client, started["id"])
        assert job["status"] == "needs_changes", job
        assert job["result_material_id"] is None
        assert job["analysis"]["profile_revision"] == revision
        assert job["analysis"]["metrics"]["explicit_unknown_tokens"] == 4
        assert not any(
            item["source"] == "generated" for item in client.get("/api/materials").json()
        )
