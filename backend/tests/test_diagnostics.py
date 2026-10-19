import os
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic_ai import models
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from backend.app.diagnostic_data import pool
from backend.app.diagnostics import create_diagnostics_router
from backend.app.knowledge import create_knowledge_router
from backend.app.main import create_app
from backend.tests.test_knowledge import import_text, token_for


def diagnostic_client(path, **kwargs):
    app = create_app(
        path, seed_demo=False, include_ai=False, frontend_dist=path.parent / "missing-dist"
    )
    app.include_router(create_diagnostics_router(app.state.materials, **kwargs))
    app.router.routes.insert(0, app.router.routes.pop())
    return TestClient(app, base_url="http://localhost:8765")


def test_diagnostic_progress_persists_and_skip_does_not_apply_assumptions(tmp_path):
    path = tmp_path / "learning.sqlite3"
    with diagnostic_client(path) as client:
        assert client.get("/api/diagnostics").json()["session"] is None
        session = client.post("/api/diagnostics", json={}).json()
        assert len(session["items"]) == 14
        assert all("meanings" not in item for item in session["items"])
        answer = {"item_id": session["items"][0]["id"], "action": "answer", "answer": "вода"}
        assert client.post(f"/api/diagnostics/{session['id']}/answers", json=answer).json()[
            "answers"
        ] == [answer]
    with diagnostic_client(path) as client:
        restored = client.get("/api/diagnostics").json()
        assert restored["session"]["id"] == session["id"]
        assert restored["profile"]["diagnostic_version"] == 0
        assert (
            client.post(f"/api/diagnostics/{session['id']}/skip", json={}).json()["status"]
            == "skipped"
        )
        assert client.get("/api/profile").json()["diagnostic_version"] == 0


@pytest.fixture
def diagnostic_mcp(tmp_path):
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


def graded_connection(correct_ids, bad_ids=False):
    def respond(messages, info):
        returned = [
            p
            for m in messages
            for p in m.parts
            if isinstance(p, ToolReturnPart) and p.tool_name == "get_diagnostic_items"
        ]
        if not returned:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "get_diagnostic_items",
                        {"language": "en", "kind": "word", "group": group, "limit": 4},
                    )
                    for group in [1, 2, 3]
                ]
                + [
                    ToolCallPart(
                        "get_diagnostic_items", {"language": "en", "kind": "sentence", "limit": 2}
                    )
                ]
            )
        assert "вода" in str(returned)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "description": "Предварительно: уверенно узнаёте частые слова.",
                        "grades": [
                            {
                                "item_id": "invented" if bad_ids else item["id"],
                                "correct": item["id"] in correct_ids,
                                "explanation": "Ответ проверен по допустимым значениям.",
                            }
                            for item in pool()
                        ],
                    },
                )
            ]
        )

    async def execute(operation):
        return await operation(FunctionModel(respond))

    return SimpleNamespace(execute=execute)


def answer_all(client, session, overrides=None):
    for item in session["items"]:
        response = client.post(
            f"/api/diagnostics/{session['id']}/answers",
            json={
                "item_id": item["id"],
                "answer": "значение",
                "action": "answer",
                **(overrides or {}).get(item["id"], {}),
            },
        )
        assert response.status_code == 200, response.text


def test_completed_diagnosis_applies_three_of_four_and_preserves_manual_null(
    diagnostic_mcp, tmp_path, monkeypatch
):
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    path = tmp_path / "api.sqlite3"
    app = create_app(path, seed_demo=False, include_ai=False, frontend_dist=tmp_path / "missing")
    correct_ids = {"en-v1-water", "en-v1-house", "en-v1-work", "en-v1-improve", "en-v1-journey"}
    app.include_router(create_knowledge_router(app.state.materials))
    app.include_router(
        create_diagnostics_router(
            app.state.materials, service=graded_connection(correct_ids), mcp_url=diagnostic_mcp
        )
    )
    app.router.routes.insert(0, app.router.routes.pop())
    with TestClient(app, base_url="http://localhost:8765") as client:
        material = import_text(
            client,
            "The friend brought water. A house needs work. "
            "I improve my advice. This cat is curious. Qxyz.",
        )
        water = token_for(material, "water")
        client.put(f"/api/lexemes/{water['lexeme_id']}/knowledge", json={"score": None})
        session = client.post("/api/diagnostics", json={}).json()
        answer_all(
            client,
            session,
            {"en-v1-curious": {"action": "skip"}, "en-v1-friend": {"action": "dont_know"}},
        )
        result = client.post(f"/api/diagnostics/{session['id']}/complete", json={})
        assert result.status_code == 200, result.text
        assert [(g["correct"], g["inferred"]) for g in result.json()["summary"]["groups"]] == [
            (3, True),
            (2, False),
            (0, False),
        ]
        material = client.get(f"/api/materials/{material['id']}").json()
        assert token_for(material, "water")["knowledge"]["score"] is None
        assert token_for(material, "water")["knowledge"]["source"] == "manual"
        assert token_for(material, "friend")["knowledge"]["score"] == 0
        assert token_for(material, "curious")["knowledge"]["score"] is None
        assert token_for(material, "The")["knowledge"]["source"] == "prior"
        assert token_for(material, "The")["knowledge"]["score"] == 2
        assert token_for(material, "Qxyz")["knowledge"]["source"] is None
        assert material["difficulty"]["provisional"] is True
        assert (
            client.get("/api/dictionary", params={"search": "the"}).json()[0]["knowledge"][
                "provisional"
            ]
            is True
        )
        # A repeated completed request is safe and does not create a new profile version.
        assert client.post(f"/api/diagnostics/{session['id']}/complete", json={}).status_code == 200
        assert client.get("/api/profile").json()["diagnostic_version"] == 1

        # Repeating diagnosis replaces provisional rules, retaining every manual decision.
        friend = token_for(material, "friend")
        client.put(f"/api/lexemes/{friend['lexeme_id']}/knowledge", json={"score": 3})
        correct_ids.clear()
        repeated = client.post("/api/diagnostics", json={}).json()
        answer_all(client, repeated)
        response = client.post(f"/api/diagnostics/{repeated['id']}/complete", json={})
        assert response.status_code == 200, response.text
        material = client.get(f"/api/materials/{material['id']}").json()
        assert token_for(material, "water")["knowledge"]["score"] is None
        assert token_for(material, "friend")["knowledge"]["score"] == 3
        assert token_for(material, "The")["knowledge"]["score"] is None
        assert client.get("/api/profile").json()["diagnostic_version"] == 2


def test_agent_rejects_unexpected_grading_ids_without_changing_profile(
    diagnostic_mcp, tmp_path, monkeypatch
):
    from fastapi import HTTPException
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    provider = graded_connection(set(), bad_ids=True)
    original = provider.execute

    async def execute(operation):
        try:
            return await original(operation)
        except UnexpectedModelBehavior:
            raise HTTPException(503, "AI вернул недопустимый результат.") from None

    provider.execute = execute
    with diagnostic_client(
        tmp_path / "api.sqlite3", service=provider, mcp_url=diagnostic_mcp
    ) as client:
        before = client.get("/api/profile").json()
        session = client.post("/api/diagnostics", json={}).json()
        answer_all(client, session)
        response = client.post(f"/api/diagnostics/{session['id']}/complete", json={})
        assert response.status_code == 503
        assert client.get("/api/profile").json() == before
        assert client.get("/api/diagnostics").json()["session"]["status"] == "in_progress"


def test_incomplete_diagnosis_and_foreign_item_do_not_change_profile(tmp_path):
    with diagnostic_client(tmp_path / "learning.sqlite3") as client:
        session = client.post("/api/diagnostics", json={}).json()
        response = client.post(
            f"/api/diagnostics/{session['id']}/answers",
            json={"item_id": "another-session", "action": "skip"},
        )
        assert response.status_code == 422
        response = client.post(f"/api/diagnostics/{session['id']}/complete", json={})
        assert response.status_code == 409
        assert client.get("/api/profile").json()["diagnostic_version"] == 0
