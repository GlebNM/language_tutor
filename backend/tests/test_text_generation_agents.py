"""Typed generation roles against the external model interface and public assessment."""

import json

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from backend.app.agents.text_generation import review_context
from backend.tests.test_text_generation_analysis import client_for, mark_vocabulary


def payload_from(messages):
    return json.loads(next(
        part.content for message in reversed(messages) for part in message.parts
        if isinstance(part, UserPromptPart)
    ))


def draft_analysis(tmp_path):
    with client_for(tmp_path / "learning.sqlite3") as client:
        mark_vocabulary(client, "We read books. We borrow books. We return books.", {
            "we": 3, "read": 2, "book": 2, "borrow": 1, "return": 2,
        })
        result = client.post("/api/text-generation/assess", json={
            "settings": {"length": "short"},
            "draft": {"title": "Books", "paragraphs": [
                {"text": "We borrow books."},
                {"text": "We read books. " * 49 + "We return books."},
            ]},
        })
        assert result.status_code == 200, result.text
        return result.json()


@pytest.mark.asyncio
async def test_context_review_sees_all_words_and_can_use_another_paragraph(tmp_path):
    analysis = draft_analysis(tmp_path)
    received = []

    def respond(messages, info):
        payload = payload_from(messages)
        received.append(payload)
        draft = payload["draft"]
        checks = []
        for item in payload["insufficient_vocabulary"]:
            for occurrence in item["occurrences"]:
                checks.append({
                    "block_id": occurrence["block_id"], "token_ids": occurrence["token_ids"],
                    "context_status": "clear",
                    "explanation": "Обещание вернуть книгу раскрывает временное пользование.",
                    "evidence": [{"block_id": draft["paragraphs"][1]["id"],
                                  "quote": "We return books."}],
                })
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            **{key: draft[key] for key in ("draft_id", "revision", "draft_hash", "snapshot_id")},
            "context_checks": checks,
        })])

    report = await review_context(model=FunctionModel(respond), analysis=analysis)
    assert report.passed is True
    assert received[0]["draft"]["paragraphs"] == analysis["versioned_draft"]["paragraphs"]
    assert received[0]["insufficient_vocabulary"] == analysis["insufficient_vocabulary"]
    assert report.context_checks[0].evidence[0].block_id.endswith(":p2")


@pytest.mark.asyncio
@pytest.mark.parametrize("mistake", ["omitted", "stale", "foreign_evidence"])
async def test_context_review_cannot_pass_with_missing_or_fabricated_findings(tmp_path, mistake):
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    analysis = draft_analysis(tmp_path)

    def respond(messages, info):
        payload = payload_from(messages)
        draft = payload["draft"]
        occurrence = payload["insufficient_vocabulary"][0]["occurrences"][0]
        check = {
            "block_id": occurrence["block_id"], "token_ids": occurrence["token_ids"],
            "context_status": "clear", "explanation": "Подсказка есть в тексте.",
            "evidence": [{"block_id": draft["paragraphs"][1]["id"],
                          "quote": "We return books."}],
        }
        output = {
            **{key: draft[key] for key in ("draft_id", "revision", "draft_hash", "snapshot_id")},
            "context_checks": [] if mistake == "omitted" else [check],
        }
        if mistake == "stale":
            output["draft_hash"] = "previous-draft"
        if mistake == "foreign_evidence":
            check["evidence"][0]["quote"] = "This sentence was invented."
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])

    with pytest.raises(UnexpectedModelBehavior):
        await review_context(model=FunctionModel(respond), analysis=analysis)


@pytest.mark.asyncio
async def test_weak_context_is_actionable_feedback_instead_of_success(tmp_path):
    analysis = draft_analysis(tmp_path)

    def respond(messages, info):
        payload = payload_from(messages)
        draft = payload["draft"]
        occurrence = payload["insufficient_vocabulary"][0]["occurrences"][0]
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            **{key: draft[key] for key in ("draft_id", "revision", "draft_hash", "snapshot_id")},
            "context_checks": [{
                "block_id": occurrence["block_id"], "token_ids": occurrence["token_ids"],
                "context_status": "weak", "explanation": "Уточните временное пользование.",
                "repair_instructions": "Добавьте, что книги нужно вернуть владельцу.",
                "evidence": [{"block_id": occurrence["block_id"], "quote": "We borrow books."}],
            }],
        })])

    report = await review_context(model=FunctionModel(respond), analysis=analysis)
    assert report.passed is False
    assert report.as_report()["blocking_issues"][0]["repair_instructions"]


@pytest.mark.asyncio
async def test_requirements_review_preserves_requested_criteria_and_numeric_failure(tmp_path):
    from backend.app.agents.text_generation import review_requirements
    from backend.app.text_generation_analysis import GenerationRequest

    analysis = draft_analysis(tmp_path)
    analysis["status"] = "fail"
    analysis["violations"] = [{"criterion_id": "new_ratio", "actual": 0.2, "maximum": 0.01}]
    received = []

    def respond(messages, info):
        payload = payload_from(messages)
        received.append(payload)
        draft = payload["draft"]
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            **{key: draft[key] for key in ("draft_id", "revision", "draft_hash", "snapshot_id")},
            "checks": [{"criterion_id": "coherence", "status": "pass",
                        "explanation": "Текст последовательно говорит о книгах.",
                        "evidence": [{"block_id": draft["paragraphs"][0]["id"],
                                      "quote": "We borrow books."}]}],
        })])

    report = await review_requirements(
        model=FunctionModel(respond), analysis=analysis, settings=GenerationRequest(length="short"),
    )
    assert report.passed is True
    assert received[0]["required_criteria"] == ["coherence"]
    assert received[0]["deterministic_report"]["status"] == "fail"
    assert analysis["status"] == "fail"


@pytest.mark.asyncio
async def test_writer_returns_free_text_without_target_fields(tmp_path):
    from backend.app.agents.text_generation import write_text
    from backend.app.text_generation_analysis import DraftText, GenerationRequest

    def respond(messages, info):
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            "kind": "draft", "title": "An Evening", "paragraphs": [{"text": "We read books."}],
        })])

    result = await write_text(
        model=FunctionModel(respond), settings=GenerationRequest(),
        profile={"known_examples": [{"lemma": "book", "score": 2}]},
    )
    assert isinstance(result, DraftText)
    assert set(result.model_dump()) == {"kind", "title", "paragraphs"}
