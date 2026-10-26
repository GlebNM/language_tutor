"""Bounded MCP access to one generation job's frozen personal dictionary."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from backend.app.learning_context import MAX_TOOL_CHARACTERS, bounded_json
from backend.app.materials import MaterialLibrary
from backend.app.text_generation_analysis import (
    DraftText,
    GenerationRequest,
    assess_draft,
    build_writing_profile,
    vocabulary_page,
)


def _fits(value: dict[str, Any]) -> bool:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"))) <= MAX_TOOL_CHARACTERS


def _profile(library: MaterialLibrary, job_id: str) -> dict[str, Any]:
    from backend.app.text_generation_jobs import get_job_snapshot

    profile = build_writing_profile(get_job_snapshot(library, job_id))
    names = ("known_examples", "learning_examples", "expression_examples")
    profile["examples_truncated"] = False
    while not _fits(profile):
        name = max(names, key=lambda key: len(profile.get(key, [])))
        if not profile.get(name):
            raise ValueError("Сводка словаря превышает допустимый размер.")
        profile[name].pop()
        profile["examples_truncated"] = True
    return profile


def _vocabulary(
    library: MaterialLibrary, job_id: str, category: Literal["known", "learning"],
    part_of_speech: str | None, offset: int, limit: int,
) -> dict[str, Any]:
    from backend.app.text_generation_jobs import get_job_snapshot

    page = vocabulary_page(get_job_snapshot(library, job_id), category,
                           part_of_speech=part_of_speech, offset=offset, limit=limit)
    trimmed = False
    while not _fits(page):
        if len(page["items"]) <= 1:
            raise ValueError("Словарная запись превышает допустимый размер.")
        page["items"].pop()
        trimmed = True
    if trimmed:
        page["next_offset"] = offset + len(page["items"])
    return page


def _issue_page(analysis: dict[str, Any], offset: int, limit: int) -> dict[str, Any]:
    if not 0 <= offset <= 100000 or not 1 <= limit <= 30:
        raise ValueError("Некорректная страница замечаний.")
    occurrences = [
        {**{key: value for key, value in item.items() if key != "occurrences"},
         "occurrences": [occurrence],
         "occurrences_complete": item["occurrence_count"] == 1}
        for item in analysis["insufficient_vocabulary"] for occurrence in item["occurrences"]
    ]
    page = {
        key: analysis[key]
        for key in ("draft_id", "revision", "draft_hash", "snapshot_id")
    }
    items = occurrences[offset:offset + limit]
    page.update(items=items, total_occurrences=len(occurrences), next_offset=None)
    while not _fits(page):
        if len(items) <= 1:
            raise ValueError("Запись замечания превышает допустимый размер.")
        items.pop()
    page["next_offset"] = offset + len(items) if offset + len(items) < len(occurrences) else None
    return page


def register_generation_tools(
    server: FastMCP, library_factory: Callable[[], MaterialLibrary]
) -> None:
    """Register read/assessment tools; identity comes from authenticated MCP context."""

    @server.tool()
    def get_generation_profile(job_id: str) -> str:
        """Read this job's frozen familiarity profile. Examples are a sample, not a word list.
        Use list_generation_vocabulary for more known/learning vocabulary.
        """
        return bounded_json(_profile(library_factory(), job_id))

    @server.tool()
    def list_generation_vocabulary(
        job_id: str, category: Literal["known", "learning"],
        part_of_speech: str | None = None,
        offset: int = Field(default=0, ge=0, le=100000),
        limit: int = Field(default=20, ge=1, le=80),
    ) -> str:
        """Read a complete paginated category from the job dictionary; follow next_offset."""
        return bounded_json(_vocabulary(library_factory(), job_id, category,
                                        part_of_speech, offset, limit))

    @server.tool()
    def get_generation_word_knowledge(job_id: str, words: list[dict[str, str]]) -> str:
        """Check up to 30 lemma/part_of_speech pairs against the SAME frozen job profile."""
        from backend.app.text_generation_jobs import get_job_snapshot

        if not 1 <= len(words) <= 30:
            raise ValueError("Проверяйте от одного до тридцати слов.")
        snapshot = get_job_snapshot(library_factory(), job_id)
        result = []
        for word in words:
            lemma, pos = word.get("lemma", ""), word.get("part_of_speech", "")
            if not 1 <= len(lemma) <= 100 or not 1 <= len(pos) <= 20:
                raise ValueError("Нужны базовая форма и часть речи слова.")
            result.append({"lemma": lemma, "part_of_speech": pos,
                           "knowledge": snapshot.knowledge(lemma, pos)})
        return bounded_json({"snapshot_id": snapshot.snapshot_id, "words": result})

    @server.tool()
    def check_text_difficulty(
        job_id: str, title: str, paragraphs: list[str],
        offset: int = Field(default=0, ge=0, le=100000),
        limit: int = Field(default=10, ge=1, le=30),
    ) -> str:
        """Assess a draft under fixed job settings. Ratios cover ALL text, items page occurrences.
        Re-call with the same title/paragraphs and next_offset to obtain remaining occurrences.
        This assessment does not publish a material or replace mandatory reviewer checks.
        """
        from backend.app.text_generation_jobs import get_job, get_job_snapshot

        library = library_factory()
        settings = GenerationRequest.model_validate(get_job(library, job_id)["request"])
        draft = DraftText.model_validate({"title": title,
                                         "paragraphs": [{"text": text} for text in paragraphs]})
        analysis = assess_draft(get_job_snapshot(library, job_id), draft, settings)
        result = _issue_page(analysis, offset, limit)
        result.update(status=analysis["status"], metrics=analysis["metrics"],
                      violations=analysis["violations"],
                      semantic_checks_pending=analysis["semantic_checks_pending"])
        while not _fits(result) and len(result["items"]) > 1:
            result["items"].pop()
            result["next_offset"] = offset + len(result["items"])
        return bounded_json(result)

    @server.tool()
    def list_draft_vocabulary_issues(
        job_id: str, draft_id: str,
        offset: int = Field(default=0, ge=0, le=100000),
        limit: int = Field(default=10, ge=1, le=30),
    ) -> str:
        """Read all insufficiently familiar occurrences in the job's stored current draft.
        The result repeats a word across pages if it has multiple occurrences.
        """
        from backend.app.text_generation_jobs import get_job

        analysis = get_job(library_factory(), job_id).get("analysis")
        if analysis is None or analysis["draft_id"] != draft_id:
            raise ValueError("Этот черновик не является текущим черновиком задания.")
        return bounded_json(_issue_page(analysis, offset, limit))
