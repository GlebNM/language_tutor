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
