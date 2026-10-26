"""Typed generation roles against the external model interface and public assessment."""

import json

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from backend.app.agents.text_generation import review_context
from backend.tests.generation_fixtures import context_output, evidence_keys, requirements_output
from backend.tests.test_text_generation_analysis import client_for, mark_vocabulary


def payload_from(messages):
    return json.loads(
        next(
            part.content
            for message in reversed(messages)
            for part in message.parts
            if isinstance(part, UserPromptPart)
        )
    )


def draft_analysis(tmp_path, *, repeats=1):
    with client_for(tmp_path / "learning.sqlite3") as client:
        mark_vocabulary(
            client,
            "We read books. We borrow books. We return books.",
            {
                "we": 3,
                "read": 2,
                "book": 2,
                "borrow": 1,
                "return": 2,
            },
        )
        result = client.post(
            "/api/text-generation/assess",
            json={
                "settings": {"length": "short"},
                "draft": {
                    "title": "Books",
                    "paragraphs": [
                        {"text": "We borrow books. " * repeats},
                        {"text": "We read books. " * 49 + "We return books."},
                    ],
                },
            },
        )
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
                checks.append(
                    {
                        "block_id": occurrence["block_id"],
                        "token_ids": occurrence["token_ids"],
                        "context_status": "clear",
                        "explanation": "Обещание вернуть книгу раскрывает временное пользование.",
                        "evidence": [
                            {"block_id": draft["paragraphs"][1]["id"], "quote": "We return books."}
                        ],
                    }
                )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    context_output(payload, checks),
                )
            ]
        )

    report = await review_context(model=FunctionModel(respond), analysis=analysis)
    assert report.passed is True
    assert [p["text"] for p in received[0]["draft"]["paragraphs"]] == [
        p["text"] for p in analysis["versioned_draft"]["paragraphs"]
    ]
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
            "block_id": occurrence["block_id"],
            "token_ids": occurrence["token_ids"],
            "context_status": "clear",
            "explanation": "Подсказка есть в тексте.",
            "evidence": [{"block_id": draft["paragraphs"][1]["id"], "quote": "We return books."}],
        }
        output = context_output(payload, [] if mistake == "omitted" else [check])
        if mistake == "stale":
            output["draft_hash"] = "previous-draft"
        if mistake == "foreign_evidence":
            output["context_checks"]["c1"]["evidence"] = ["This sentence was invented."]
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])

    with pytest.raises(UnexpectedModelBehavior):
        await review_context(model=FunctionModel(respond), analysis=analysis)


@pytest.mark.asyncio
async def test_weak_context_is_actionable_feedback_instead_of_success(tmp_path):
    analysis = draft_analysis(tmp_path)

    def respond(messages, info):
        payload = payload_from(messages)
        occurrence = payload["insufficient_vocabulary"][0]["occurrences"][0]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    context_output(payload, [
                            {
                                "block_id": occurrence["block_id"],
                                "token_ids": occurrence["token_ids"],
                                "context_status": "weak",
                                "explanation": "Уточните временное пользование.",
                                "repair_instructions": "Укажите, что книги нужно вернуть.",
                                "evidence": [
                                    {
                                        "block_id": occurrence["block_id"],
                                        "quote": "We borrow books.",
                                    }
                                ],
                            }
                    ]),
                )
            ]
        )

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
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    requirements_output(payload, [
                            {
                                "criterion_id": "coherence",
                                "status": "pass",
                                "explanation": "Текст последовательно говорит о книгах.",
                                "evidence": [
                                    {
                                        "block_id": draft["paragraphs"][0]["id"],
                                        "quote": "We borrow books.",
                                    }
                                ],
                            }
                    ]),
                )
            ]
        )

    report = await review_requirements(
        model=FunctionModel(respond),
        analysis=analysis,
        settings=GenerationRequest(length="short"),
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
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "kind": "draft",
                        "title": "An Evening",
                        "paragraphs": [{"text": "We read books."}],
                    },
                )
            ]
        )

    result = await write_text(
        model=FunctionModel(respond),
        settings=GenerationRequest(),
        profile={"known_examples": [{"lemma": "book", "score": 2}]},
    )
    assert isinstance(result, DraftText)
    assert set(result.model_dump()) == {"kind", "title", "paragraphs"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("sufficiency", "reason", "feedback"), [
    ("insufficient", "insufficient_profile", None),
    ("provisional", "conflicting_constraints", None),
    ("provisional", "insufficient_profile", {"deterministic_report": {
        "violations": [{"criterion_id": "new_ratio", "actual": 0.5, "maximum": 0.01}],
    }}),
])
async def test_writer_keeps_supported_or_unrelated_refusals(sufficiency, reason, feedback):
    from backend.app.agents.text_generation import write_text
    from backend.app.text_generation_analysis import CannotGenerate, GenerationRequest

    def respond(messages, info):
        return ModelResponse(parts=[ToolCallPart(info.output_tools[1].name, {
            "kind": "blocked", "reason_code": reason,
            "explanation": "Не удаётся соблюсти требования.",
            "conflicting_constraints": ["topic", "style"] if reason == "conflicting_constraints"
                                       else [],
        })])

    result = await write_text(
        model=FunctionModel(respond), settings=GenerationRequest(),
        profile={"profile_sufficiency": sufficiency}, feedback=feedback,
    )
    assert isinstance(result, CannotGenerate)
    assert result.reason_code == reason


@pytest.mark.asyncio
async def test_writer_does_not_end_with_untested_profile_refusal():
    from pydantic_ai.messages import RetryPromptPart

    from backend.app.agents.text_generation import write_text
    from backend.app.text_generation_analysis import DraftText, GenerationRequest

    def respond(messages, info):
        if any(isinstance(part, RetryPromptPart) for message in messages for part in message.parts):
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
                "kind": "draft", "title": "Books",
                "paragraphs": [{"text": "We read books."}],
            })])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[1].name, {
            "kind": "blocked", "reason_code": "insufficient_profile",
            "explanation": "The known examples are truncated, so other words are unassessed.",
            "conflicting_constraints": [],
        })])

    result = await write_text(
        model=FunctionModel(respond), settings=GenerationRequest(),
        profile={"profile_sufficiency": "provisional", "examples_truncated": True,
                 "known_examples": [{"lemma": "book", "score": 2}]},
    )
    assert isinstance(result, DraftText)

@pytest.mark.asyncio
async def test_context_report_binds_required_short_keys_to_every_canonical_occurrence(tmp_path):
    analysis = draft_analysis(tmp_path, repeats=2)
    received = []

    def respond(messages, info):
        payload = payload_from(messages)
        received.append(payload)
        checks = {
            key: {
                'context_status': 'clear',
                'explanation': 'Обещание вернуть книгу раскрывает значение.',
                'evidence': evidence_keys(payload, [{'quote': 'We return books.'}]),
                'repair_instructions': None,
            }
            for key in payload['review_keys']
        }
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            'context_checks': checks,
        })])

    report = await review_context(model=FunctionModel(respond), analysis=analysis)
    assert report.passed
    assert report.draft_id == analysis['draft_id']
    assert report.snapshot_id == analysis['snapshot_id']
    expected = analysis['insufficient_vocabulary'][0]['occurrences'][0]['token_ids']
    assert report.context_checks[0].token_ids == expected
    assert report.context_checks[0].evidence[0].block_id.endswith(':p2')
    assert set(received[0]['review_keys']) == {'c1', 'c2'}
    assert len(report.context_checks) == 2
    assert report.context_checks[0].token_ids != report.context_checks[1].token_ids

@pytest.mark.asyncio
async def test_omitted_repeated_occurrence_is_retried_without_copying_source_ids(tmp_path):
    analysis = draft_analysis(tmp_path, repeats=2)
    attempts = []

    def respond(messages, info):
        payload = payload_from(messages)
        attempts.append(payload)
        keys = ['c1'] if len(attempts) == 1 else ['c1', 'c2']
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            'context_checks': {key: {
                'context_status': 'clear',
                'explanation': 'Возврат книги раскрывает временное пользование.',
                'evidence': evidence_keys(payload, [{'quote': 'We return books.'}]),
                'repair_instructions': None,
            } for key in keys},
        })])

    report = await review_context(model=FunctionModel(respond), analysis=analysis)
    assert len(attempts) == 2
    assert len(report.context_checks) == 2
    assert report.passed
    assert report.context_checks[0].block_id == report.context_checks[1].block_id
    assert report.context_checks[0].token_ids != report.context_checks[1].token_ids

@pytest.mark.asyncio
@pytest.mark.parametrize('evidence_count', [1, 6])
async def test_requirements_bind_quote_to_source_without_model_ids(tmp_path, evidence_count):
    from backend.app.agents.text_generation import review_requirements
    from backend.app.text_generation_analysis import GenerationRequest

    analysis = draft_analysis(tmp_path)
    def respond(messages, info):
        payload = payload_from(messages)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            'checks': {name: {
                'status': 'pass', 'explanation': 'Текст соответствует требованию.',
                'evidence': (evidence_keys(payload, [{'quote': 'We return books.'}])
                             if evidence_count == 1 else [key for key, source in
                             payload['evidence_sources'].items()
                             if source['block_id'].endswith(':p2')][:evidence_count]),
                'repair_instructions': None,
            } for name in payload['required_criteria']},
        })])
    report = await review_requirements(
        model=FunctionModel(respond), analysis=analysis,
        settings=GenerationRequest(topic='Books', style='Simple narrative'),
    )
    assert report.passed
    assert all(len(check.evidence) == evidence_count for check in report.checks)
    assert report.draft_hash == analysis['draft_hash']
    assert {check.criterion_id for check in report.checks} == {'topic', 'style', 'coherence'}
    assert all(check.evidence[0].block_id.endswith(':p2') for check in report.checks)
