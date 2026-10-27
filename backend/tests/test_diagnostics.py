import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic_ai import models
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from backend.app.ai_connection.settings import AISettingsStore
from backend.app.diagnostic_data import frequency_ranks
from backend.app.diagnostics import DiagnosticsService, create_diagnostics_router
from backend.app.main import create_learner_app as create_app
from backend.app.materials import MaterialLibrary
from backend.app.tenant_context import current_mcp_token
from backend.tests.account_support import mcp_account
from backend.tests.test_knowledge import import_text, token_for


def diagnostic_client(path, **kwargs):
    app = create_app(
        path, seed_demo=False, include_ai=False, frontend_dist=path.parent / "missing-dist"
    )
    app.include_router(create_diagnostics_router(app.state.materials, **kwargs))
    # Routes defined by the test take precedence over the unconnected production router.
    count = 6
    app.router.routes[:] = app.router.routes[-count:] + app.router.routes[:-count]
    return TestClient(app, base_url="http://localhost:8765")


@pytest.fixture
def diagnostic_mcp(tmp_path):
    context, database = mcp_account(tmp_path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "backend.mcp_server"],
        env={
            **os.environ,
            "MCP_HOST": "127.0.0.1",
            "MCP_PORT": str(port),
            "DATA_DIR": str(tmp_path),
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        for _ in range(150):
            assert process.poll() is None
            try:
                httpx.get(url, timeout=0.1)
                break
            except httpx.TransportError:
                time.sleep(0.02)
        else:
            pytest.fail("MCP did not start")
        yield url
    finally:
        process.terminate()
        process.wait(timeout=5)
        current_mcp_token.reset(context)


# Fixed examples are test fixtures only. Production selection always uses the dictionary tool.
QUESTIONS = [
    ("water", "NOUN", "She mentioned the water during our meeting."),
    ("house", "NOUN", "They talked about the house again today."),
    ("work", "VERB", "We work together on most days."),
    ("friend", "NOUN", "I spoke to my friend before lunch."),
    ("improve", "VERB", "They want to improve the plan this week."),
    ("journey", "NOUN", "We discussed the journey after the meeting."),
    ("effort", "NOUN", "She mentioned the effort in her report."),
    ("advice", "NOUN", "He asked about the advice again today."),
    ("curious", "ADJ", "She seemed curious when we spoke today."),
    ("resist", "VERB", "They may resist the plan for a while."),
    ("rough", "ADJ", "She said the surface was rough today."),
    ("comfort", "NOUN", "They spoke about comfort at the meeting."),
] + [
    (word, "NOUN", f"They mentioned the {word} during our meeting.")
    for word in "book time day night world money river village bread garden prison courage "
    "justice reward shelter burden wisdom wealth chance road voice room reason "
    "window truth choice memory owner".split()
]


def adaptive_connection(
    settings_dir, *, reject_once=False, reject_all=False, failure=None, stop=True, capture=None
):
    reviews = 0

    def respond(messages, info):
        nonlocal reviews
        assert info.model_settings["openai_reasoning_effort"] == "low"
        payload = json.loads(
            next(p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart))
        )
        if "masked_prompt" in payload:
            reviews += 1
            assert payload["surface"] not in payload["masked_prompt"].split()
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        info.output_tools[0].name,
                        {
                            "acceptable": not (reject_all or (reject_once and reviews == 1)),
                            "reason": "Контекст раскрывает значение."
                            if reviews == 1
                            else "Контекст нейтрален.",
                        },
                    )
                ]
            )
        if capture is not None:
            capture.append(payload)
        if payload["can_finish"] and stop:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        info.output_tools[0].name,
                        {
                            "question": None,
                            "finish": True,
                            "description": "Предварительный профиль по самооценке знакомости.",
                        },
                    )
                ]
            )
        lemma, pos, prompt = next(q for q in QUESTIONS if q[0] not in payload["excluded"])
        returns = [
            p
            for m in messages
            for p in m.parts
            if isinstance(p, ToolReturnPart) and p.tool_name == "get_frequency_words"
        ]
        if not returns and failure != "no_dictionary":
            rank = frequency_ranks()[lemma]
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "get_frequency_words",
                        {
                            "min_rank": rank,
                            "max_rank": rank,
                            "limit": 1,
                        },
                    )
                ]
            )
        if returns:
            assert lemma in str(returns[-1].content)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "question": {
                            "lemma": "invented" if failure == "invented" else lemma,
                            "part_of_speech": "VERB" if failure == "wrong_pos" else pos,
                            "surface": lemma,
                            "prompt": prompt,
                            "meaning": "Скрытое значение для проверки.",
                        },
                        "finish": False,
                        "description": "Уточняем знакомость этой частотной группы.",
                    },
                )
            ]
        )

    async def execute(operation, *, settings=None):
        try:
            return await operation(FunctionModel(respond))
        except UnexpectedModelBehavior:
            raise HTTPException(503, "AI вернул недопустимый результат.") from None

    return SimpleNamespace(execute=execute, settings_store=AISettingsStore(settings_dir))


def next_question(client, session):
    response = client.post(f"/api/diagnostics/{session['id']}/next", json={})
    assert response.status_code == 200, response.text
    return response.json()


def answer_all(client, session, overrides=None, default="known"):
    while not session["ready_to_complete"]:
        session = next_question(client, session)
        if session["ready_to_complete"]:
            break
        item = session["items"][-1]
        word = item["surface"]
        response = client.post(
            f"/api/diagnostics/{session['id']}/answers",
            json={
                "item_id": item["id"],
                "action": (overrides or {}).get(word, default),
            },
        )
        assert response.status_code == 200, response.text
        session = response.json()
    return session


def test_progress_persists_and_skip_does_not_apply_assumptions(tmp_path, diagnostic_mcp):
    path = tmp_path / "api.sqlite3"
    provider = adaptive_connection(tmp_path)
    with diagnostic_client(path, service=provider, mcp_url=diagnostic_mcp) as client:
        assert client.get("/api/diagnostics").json()["session"] is None
        session = next_question(client, client.post("/api/diagnostics", json={}).json())
        item = session["items"][0]
        assert "meaning" not in item and "lemma" not in item
        assert item["prompt"][item["start_offset"] : item["end_offset"]] == "water"
        answer = {"item_id": item["id"], "action": "unsure"}
        assert client.post(f"/api/diagnostics/{session['id']}/answers", json=answer).json()[
            "answers"
        ] == [answer]
    with diagnostic_client(path, service=provider, mcp_url=diagnostic_mcp) as client:
        restored = client.get("/api/diagnostics").json()
        assert restored["session"]["id"] == session["id"]
        assert restored["profile"]["diagnostic_version"] == 0
        resumed = next_question(client, restored["session"])
        assert resumed["items"][-1]["surface"] != "water"
        assert client.post(f"/api/diagnostics/{session['id']}/skip", json={}).status_code == 200
        assert client.get("/api/profile").json()["diagnostic_version"] == 0


def test_completed_self_report_preserves_manual_and_separates_uncertainty(tmp_path, diagnostic_mcp):
    path = tmp_path / "api.sqlite3"
    captured = []
    with diagnostic_client(
        path, service=adaptive_connection(tmp_path, capture=captured), mcp_url=diagnostic_mcp
    ) as client:
        material = import_text(
            client,
            "The friend brought water. We work together. "
            "A work of art. I improve my advice. This cat is curious. Qxyz.",
        )
        water = token_for(material, "water")
        client.put(f"/api/lexemes/{water['lexeme_id']}/knowledge", json={"score": None})
        session = client.post("/api/diagnostics", json={}).json()
        answer_all(
            client,
            session,
            {
                "friend": "unknown",
                "advice": "unsure",
                "curious": "skip",
                "book": "unknown",
            },
        )
        result = client.post(f"/api/diagnostics/{session['id']}/complete", json={})
        assert result.status_code == 200, result.text
        summary = result.json()["summary"]
        assert summary["basis"] == "self_report"
        assert summary["model"] == "gpt-6-luna"
        assert summary["reasoning_effort"] == "low"
        assert all("correct" not in grade for grade in summary["grades"])
        assert not summary["groups"][0]["inferred"]  # Three of four no longer enables a prior.
        assert captured[2]["answers"][0]["action"] == "known"
        material = client.get(f"/api/materials/{material['id']}").json()
        assert token_for(material, "water")["knowledge"]["source"] == "manual"
        assert token_for(material, "water")["knowledge"]["score"] is None
        assert token_for(material, "friend")["knowledge"]["score"] == 0
        assert token_for(material, "advice")["knowledge"]["score"] == 1
        assert token_for(material, "curious")["knowledge"]["score"] is None
        assert token_for(material, "work")["knowledge"]["score"] == 2
        assert token_for(material, "The")["knowledge"]["score"] is None
        assert token_for(material, "Qxyz")["knowledge"]["source"] is None
        assert client.post(f"/api/diagnostics/{session['id']}/complete", json={}).status_code == 200
        assert client.get("/api/profile").json()["diagnostic_version"] == 1


def test_group_prior_and_repeated_session_use_new_words_preserving_manual(tmp_path, diagnostic_mcp):
    path = tmp_path / "api.sqlite3"
    with diagnostic_client(
        path, service=adaptive_connection(tmp_path), mcp_url=diagnostic_mcp
    ) as client:
        material = import_text(client, "The friend brought water. This cat is curious.")
        session = client.post("/api/diagnostics", json={}).json()
        first = answer_all(client, session)
        result = client.post(f"/api/diagnostics/{session['id']}/complete", json={})
        assert result.status_code == 200
        assert all(g["inferred"] for g in result.json()["summary"]["groups"])
        restored = client.get(f"/api/materials/{material['id']}").json()
        assert token_for(restored, "The")["knowledge"]["source"] == "prior"
        friend = token_for(restored, "friend")
        client.put(f"/api/lexemes/{friend['lexeme_id']}/knowledge", json={"score": 3})
        repeated = client.post("/api/diagnostics", json={}).json()
        second = answer_all(client, repeated, default="unknown")
        assert {i["surface"] for i in first["items"]}.isdisjoint(
            i["surface"] for i in second["items"]
        )
        assert (
            client.post(f"/api/diagnostics/{repeated['id']}/complete", json={}).status_code == 200
        )
        restored = client.get(f"/api/materials/{material['id']}").json()
        assert token_for(restored, "friend")["knowledge"]["score"] == 3
        assert token_for(restored, "The")["knowledge"]["score"] is None
        assert client.get("/api/profile").json()["diagnostic_version"] == 2


@pytest.mark.parametrize("failure", ["invented", "wrong_pos", "no_dictionary"])
def test_unverified_questions_never_reach_user_or_change_profile(tmp_path, diagnostic_mcp, failure):
    with diagnostic_client(
        tmp_path / "api.sqlite3",
        service=adaptive_connection(tmp_path, failure=failure),
        mcp_url=diagnostic_mcp,
    ) as client:
        before = client.get("/api/profile").json()
        session = client.post("/api/diagnostics", json={}).json()
        assert client.post(f"/api/diagnostics/{session['id']}/next", json={}).status_code == 503
        restored = client.get("/api/diagnostics").json()["session"]
        assert restored["items"] == []
        assert client.get("/api/profile").json() == before


def test_context_reviewer_rejects_and_regenerates_question(tmp_path, diagnostic_mcp):
    with diagnostic_client(
        tmp_path / "api.sqlite3",
        service=adaptive_connection(tmp_path, reject_once=True),
        mcp_url=diagnostic_mcp,
    ) as client:
        session = next_question(client, client.post("/api/diagnostics", json={}).json())
        assert session["items"][0]["surface"] == "house"
        assert len(session["items"]) == 1


def test_ollama_question_regeneration_keeps_schema_within_round_budget(
    tmp_path, diagnostic_mcp, monkeypatch
):
    """Replay the format-repair loop seen before the first placement question."""
    import httpx2
    from openai import AsyncOpenAI

    from backend.app.ai_connection import ollama
    from backend.app.ai_connection.service import ConnectionService
    from backend.app.ai_connection.settings import AISettings
    from backend.app.ai_connection.siwc import SIWCOAuthFlow

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    monkeypatch.setenv("OLLAMA_API_KEY", "synthetic-ollama-secret")
    requests = []
    reviews = 0

    def question(index):
        lemma, pos, prompt = QUESTIONS[index]
        return {
            "question": {
                "lemma": lemma, "part_of_speech": pos, "surface": lemma,
                "prompt": prompt, "meaning": "вода" if index == 0 else "дом",
            },
            "finish": False, "description": "Уточняем знакомость слова.",
        }

    def call(name, arguments):
        return {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": f"call_{len(requests)}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }],
        }

    def dictionary(index):
        rank = frequency_ranks()[QUESTIONS[index][0]]
        return call("get_frequency_words", {"min_rank": rank, "max_rank": rank, "limit": 1})

    async def respond(request):
        nonlocal reviews
        payload = json.loads(request.content)
        requests.append(payload)
        messages = payload["messages"]
        instructions = " ".join(
            m["content"] for m in messages if m["role"] in ("system", "developer")
        )
        if '"properties"' in instructions:
            # A prompted schema lets the model use the exact field names immediately.
            assert payload["response_format"] == {"type": "json_object"}
            if "masked_prompt" in messages[-1]["content"]:
                reviews += 1
                output = {"acceptable": reviews > 1, "reason": "Проверка контекста."}
                message = {"role": "assistant", "content": json.dumps(output)}
            elif messages[-1]["role"] == "tool":
                message = {"role": "assistant", "content": json.dumps(question(reviews))}
            else:
                message = dictionary(reviews)
        else:
            # Captured pattern: dictionary, three format repairs, rejected context,
            # dictionary again, then a fresh format error consumes the shared budget.
            broken = {**question(0), "question": QUESTIONS[0][2]}
            wrong_fields = question(0)
            wrong_fields["question"]["pos"] = "noun"
            wrong_fields["question"]["sentence"] = wrong_fields["question"].pop("prompt")
            del wrong_fields["question"]["part_of_speech"]
            wrong_pos = question(0)
            wrong_pos["question"]["part_of_speech"] = "noun"
            replay = [
                lambda: dictionary(0),
                lambda: {"role": "assistant", "content": json.dumps(broken)},
                lambda: call("final_result", wrong_fields),
                lambda: call("final_result", wrong_pos),
                lambda: call("final_result", question(0)),
                lambda: call("final_result", {"acceptable": False, "reason": "Подсказка."}),
                lambda: dictionary(1),
                lambda: {"role": "assistant", "content": json.dumps(broken)},
            ]
            message = replay[len(requests) - 1]()
        return httpx2.Response(200, json={
            "id": "placement-test", "object": "chat.completion", "created": 1,
            "model": "gemma4:31b",
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        })

    def controlled_client(**kwargs):
        return AsyncOpenAI(
            **kwargs, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
        )

    monkeypatch.setattr(ollama, "AsyncOpenAI", controlled_client)
    service = ConnectionService(
        data_dir=tmp_path, callback_host="127.0.0.1", callback_port=0,
        oauth_timeout=5, flow_factory=SIWCOAuthFlow,
        mcp_url=diagnostic_mcp, model_name="gpt-6-luna",
    )
    service.settings_store.write(AISettings("gpt-6-luna", "low", True))
    with diagnostic_client(
        tmp_path / "api.sqlite3", service=service, mcp_url=diagnostic_mcp
    ) as client:
        session = next_question(client, client.post("/api/diagnostics", json={}).json())
        assert session["items"][0]["surface"] == "house"
        assert len(session["items"]) == 1
        assert client.get("/api/profile").json()["diagnostic_version"] == 0
    assert reviews == 2
    assert len(requests) == 6


def test_hard_cap_and_skips_never_enable_group_priors(tmp_path, diagnostic_mcp):
    with diagnostic_client(
        tmp_path / "api.sqlite3",
        service=adaptive_connection(tmp_path, stop=False),
        mcp_url=diagnostic_mcp,
    ) as client:
        session = client.post("/api/diagnostics", json={}).json()
        ready = answer_all(client, session, default="skip")
        assert len(ready["items"]) == 18
        assert ready["ready_to_complete"]
        result = client.post(f"/api/diagnostics/{session['id']}/complete", json={})
        assert result.status_code == 200
        assert all(
            g["total"] == 0 and not g["inferred"] for g in result.json()["summary"]["groups"]
        )
        assert all(g["score"] is None for g in result.json()["summary"]["grades"])


def test_foreign_items_incomplete_and_duplicate_answers(tmp_path, diagnostic_mcp):
    with diagnostic_client(
        tmp_path / "api.sqlite3", service=adaptive_connection(tmp_path), mcp_url=diagnostic_mcp
    ) as client:
        session = next_question(client, client.post("/api/diagnostics", json={}).json())
        url = f"/api/diagnostics/{session['id']}"
        assert (
            client.post(url + "/answers", json={"item_id": "foreign", "action": "skip"}).status_code
            == 422
        )
        assert client.post(url + "/complete", json={}).status_code == 422
        answer = {"item_id": session["items"][0]["id"], "action": "known"}
        assert client.post(url + "/answers", json=answer).status_code == 200
        assert len(client.post(url + "/answers", json=answer).json()["answers"]) == 1
        assert (
            client.post(url + "/answers", json={**answer, "action": "unknown"}).status_code == 409
        )
        resumed = next_question(client, session)
        assert len(resumed["items"]) == 2
        assert next_question(client, session) == resumed
        assert client.get("/api/profile").json()["diagnostic_version"] == 0


async def test_frequency_dictionary_mcp_ranks_bounds_and_exclusion(diagnostic_mcp):
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    async with Client(diagnostic_mcp, auth=current_mcp_token.get()) as client:
        assert "get_frequency_words" in {t.name for t in await client.list_tools()}
        result = await client.call_tool("get_frequency_words", {"group": 2, "limit": 20})
        encoded = result.structured_content["result"]
        payload = json.loads(encoded)
        assert len(encoded) <= 6000 and len(payload["words"]) == 20
        for word in payload["words"]:
            assert frequency_ranks()[word["lemma"]] == word["rank"]
            assert 1001 <= word["rank"] <= 3000 and word["group"] == 2
        rank = frequency_ranks()["water"]
        result = await client.call_tool(
            "get_frequency_words",
            {
                "min_rank": rank,
                "max_rank": rank,
                "exclude": ["water"],
            },
        )
        assert json.loads(result.structured_content["result"])["words"] == []
        for args in [
            {"limit": 21},
            {"min_rank": 100, "max_rank": 1},
            {"language": "ru"},
            {"exclude": ["x"] * 101},
        ]:
            with pytest.raises(ToolError):
                await client.call_tool("get_frequency_words", args)


async def test_parallel_generation_and_skip_during_generation(tmp_path, diagnostic_mcp):
    service = DiagnosticsService(MaterialLibrary(tmp_path / "api.sqlite3"))
    provider = adaptive_connection(tmp_path)
    session = service.start()
    first, second = await asyncio.gather(
        *[service.next_question(session["id"], provider, diagnostic_mcp) for _ in range(2)]
    )
    assert first == second and len(first["items"]) == 1
    service.skip(session["id"])
    new_session = service.start()
    original = provider.execute

    async def skip_while_generating(operation, *, settings=None):
        result = await original(operation, settings=settings)
        service.skip(new_session["id"])
        return result

    provider.execute = skip_while_generating
    with pytest.raises(HTTPException) as error:
        await service.next_question(new_session["id"], provider, diagnostic_mcp)
    assert error.value.status_code == 409
    assert service.get(new_session["id"])["items"] == []
    assert service.library.get_profile()["diagnostic_version"] == 0


def test_old_session_can_be_replaced_without_rewriting_completed_profile(tmp_path):
    service = DiagnosticsService(MaterialLibrary(tmp_path / "api.sqlite3"))
    old = service.start()
    with service.library.connect() as db:
        db.execute("UPDATE diagnostic_sessions SET pool_version=1 WHERE id=?", (old["id"],))
    new = service.start()
    assert new["id"] != old["id"]
    assert service.get(old["id"])["status"] == "skipped"
    assert new["format_version"] == 2
    assert service.library.get_profile()["diagnostic_version"] == 0


@pytest.fixture(autouse=True)
def disable_live_models(monkeypatch):
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)


def test_failed_next_keeps_saved_answer_and_can_be_retried(tmp_path, diagnostic_mcp):
    provider = adaptive_connection(tmp_path)
    original = provider.execute
    with diagnostic_client(
        tmp_path / "api.sqlite3", service=provider, mcp_url=diagnostic_mcp
    ) as client:
        session = next_question(client, client.post("/api/diagnostics", json={}).json())
        url = f"/api/diagnostics/{session['id']}"
        answer = {"item_id": session["items"][0]["id"], "action": "unknown"}
        assert client.post(url + "/answers", json=answer).status_code == 200

        async def unavailable(operation, *, settings=None):
            raise HTTPException(504, "AI не ответил за 30 секунд.")

        provider.execute = unavailable
        assert client.post(url + "/next", json={}).status_code == 504
        restored = client.get("/api/diagnostics").json()["session"]
        assert restored["answers"] == [answer]
        assert len(restored["items"]) == 1
        assert client.get("/api/profile").json()["diagnostic_version"] == 0
        provider.execute = original
        assert len(next_question(client, restored)["items"]) == 2


def test_two_rejected_contexts_leave_session_ready_for_retry(tmp_path, diagnostic_mcp):
    with diagnostic_client(
        tmp_path / "api.sqlite3",
        service=adaptive_connection(tmp_path, reject_all=True),
        mcp_url=diagnostic_mcp,
    ) as client:
        session = client.post("/api/diagnostics", json={}).json()
        assert client.post(f"/api/diagnostics/{session['id']}/next", json={}).status_code == 503
        assert client.get("/api/diagnostics").json()["session"]["items"] == []
        assert client.get("/api/profile").json()["diagnostic_version"] == 0
