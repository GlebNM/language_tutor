"""Familiarity, dictionary and lexical coverage shared by both interfaces."""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from backend.app.materials import MaterialError, MaterialLibrary

STATUSES = {
    None: "Пока не оценено",
    0: "Не знаю",
    1: "Учу",
    2: "Узнаю в контексте",
    3: "Уверенно знаю",
}


def knowledge_value(
    score: int | None, ignored: bool = False, source: str | None = None
) -> dict[str, Any]:
    return {
        "score": score,
        "ignored": ignored,
        "source": source,
        "provisional": source in {"diagnostic", "prior"},
        "status": "Исключено" if ignored else STATUSES[score],
    }


class KnowledgeService:
    def __init__(self, library: MaterialLibrary):
        self.library = library

    def get(self, lexeme_id: str, *, db: sqlite3.Connection | None = None) -> dict[str, Any]:
        if db is None:
            with self.library.connect() as connection:
                return self.get(lexeme_id, db=connection)
        lexeme = db.execute("SELECT * FROM lexemes WHERE id=?", (lexeme_id,)).fetchone()
        if lexeme is None:
            raise KeyError("Слово не найдено.")
        row = db.execute("SELECT * FROM word_knowledge WHERE lexeme_id=?", (lexeme_id,)).fetchone()
        if row:
            return knowledge_value(row["score"], bool(row["ignored"]), row["source"])
        from backend.app.diagnostic_data import frequency_group

        group = frequency_group(lexeme["lemma"], lexeme["language"])
        rule = (
            db.execute(
                "SELECT enabled FROM diagnostic_rules WHERE language=? AND frequency_group=?",
                (lexeme["language"], group),
            ).fetchone()
            if group
            else None
        )
        if rule and rule["enabled"]:
            return knowledge_value(2, source="prior")
        return knowledge_value(None)

    def set_manual(
        self, lexeme_id: str, score: int | None, ignored: bool | None = None
    ) -> dict[str, Any]:
        if score is not None and (type(score) is not int or score not in {0, 1, 2, 3}):
            raise MaterialError("Знакомость должна быть от 0 до 3 или null.")
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self.get(lexeme_id, db=db)
            db.execute(
                "INSERT INTO word_knowledge VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(lexeme_id) DO UPDATE SET score=excluded.score,"
                "ignored=excluded.ignored,"
                "source=excluded.source,assessment_version=NULL,updated_at=excluded.updated_at",
                (
                    lexeme_id,
                    score,
                    int(previous["ignored"] if ignored is None else ignored),
                    "manual",
                    None,
                    datetime.now(UTC).isoformat(),
                ),
            )
            db.execute("UPDATE learning_profile SET revision=revision+1 WHERE id=1")
            return self.get(lexeme_id, db=db)

    def dictionary(
        self, search: str = "", score: str | None = None, ignored: bool | None = None
    ) -> list[dict[str, Any]]:
        with self.library.connect() as db:
            entries = []
            for row in db.execute("SELECT * FROM lexemes ORDER BY lemma,part_of_speech"):
                knowledge = self.get(row["id"], db=db)
                if search.casefold() not in row["lemma"]:
                    continue
                if score is not None and knowledge["score"] != (
                    None if score == "unassessed" else int(score)
                ):
                    continue
                if ignored is not None and knowledge["ignored"] != ignored:
                    continue
                # Dictionary contains saved and provisional words, rather than every unseen token.
                if knowledge["source"] is None:
                    continue
                entry = dict(row)
                entry["lexeme_id"] = entry.pop("id")
                entry["knowledge"] = knowledge
                entries.append(entry)
            return entries

    def correct_lexeme(
        self, material_id: str, cue_id: str, token_id: str, lemma: str, part_of_speech: str
    ) -> dict[str, Any]:
        selected = self.library.selection(material_id, cue_id, token_id)
        lemma = lemma.strip().casefold()
        if not lemma or len(lemma) > 100 or any(char.isspace() for char in lemma):
            raise MaterialError("Укажите одну базовую форму слова, до 100 символов.")
        if part_of_speech not in {
            "ADJ",
            "ADP",
            "ADV",
            "AUX",
            "CCONJ",
            "DET",
            "INTJ",
            "NOUN",
            "NUM",
            "PART",
            "PRON",
            "PROPN",
            "SCONJ",
            "VERB",
            "X",
        }:
            raise MaterialError("Укажите допустимую часть речи.")
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            still_selected = db.execute(
                "SELECT id FROM occurrences WHERE id=? AND cue_id=? AND material_id=?",
                (token_id, cue_id, material_id),
            ).fetchone()
            if still_selected is None:
                raise KeyError("Выбранное слово больше не существует.")
            language = selected["token"]["language"]
            db.execute(
                "INSERT OR IGNORE INTO lexemes VALUES(?,?,?,?)",
                (uuid.uuid4().hex, language, lemma, part_of_speech),
            )
            row = db.execute(
                "SELECT id FROM lexemes WHERE language=? AND lemma=? AND part_of_speech=?",
                (language, lemma, part_of_speech),
            ).fetchone()
            db.execute(
                "UPDATE occurrences SET lexeme_id=?,uncertain=0 "
                "WHERE id=? AND cue_id=? AND material_id=?",
                (row["id"], token_id, cue_id, material_id),
            )
            db.execute("UPDATE learning_profile SET revision=revision+1 WHERE id=1")
        return self.library.selection(material_id, cue_id, token_id)


def calculate_difficulty(material: dict[str, Any]) -> dict[str, Any]:
    tokens: list[dict[str, Any]] = []
    for cue in material["cues"]:
        markers = [
            (match.start(), match.end()) for match in re.finditer(r"\[[^\]]*\]", cue["text"])
        ]
        tokens.extend(
            token
            for token in cue["tokens"]
            if not token["knowledge"]["ignored"]
            and not any(start <= token["start_offset"] < end for start, end in markers)
        )
    known = sum(t["knowledge"]["score"] in {2, 3} for t in tokens)
    unfamiliar = sum(t["knowledge"]["score"] in {0, 1} for t in tokens)
    unassessed = len(tokens) - known - unfamiliar
    provisional = sum(
        t["knowledge"]["provisional"] and t["knowledge"]["score"] is not None for t in tokens
    )
    provisional_known = sum(
        t["knowledge"]["provisional"] and t["knowledge"]["score"] in {2, 3} for t in tokens
    )
    total = len(tokens)
    reasons = []
    if total == 0:
        reasons.append("В дорожке нет учитываемых слов.")
    elif total < 50:
        reasons.append("Короткая дорожка: для оценки нужно хотя бы 50 слов.")
    if not material["is_complete"]:
        reasons.append("Дорожка неполная: оценка всего видео недоступна.")
    if total and unassessed / total > 0.20:
        reasons.append(
            "Больше 20% слов пока не оценены. Пройдите диагностику или уточните словарь."
        )
    label = None
    if not reasons:
        label = (
            "Мало новой лексики"
            if known / total >= 0.95
            else "Понадобятся подсказки"
            if known / total >= 0.90
            else "Много новой лексики"
        )
    return {
        "total": total,
        "known": known,
        "unfamiliar": unfamiliar,
        "unassessed": unassessed,
        "unique_unfamiliar": len(
            {t["lexeme_id"] for t in tokens if t["knowledge"]["score"] in {0, 1}}
        ),
        "known_percent": 100 * known / total if total else None,
        "unfamiliar_percent": 100 * unfamiliar / total if total else None,
        "unassessed_percent": 100 * unassessed / total if total else None,
        "provisional_count": provisional,
        "provisional_percent": 100 * provisional / total if total else None,
        "provisional_known_percent": 100 * provisional_known / total if total else None,
        "coverage_interval": [100 * known / total, 100 * (known + unassessed) / total]
        if total and unassessed
        else None,
        "provisional": bool(total and provisional / total > 0.20),
        "label": label,
        "limitations": reasons,
        "explanation": (
            "Оценка касается лексики; она не измеряет акцент, темп, грамматику "
            "и общее понимание. Диапазон покрытия условен и не является "
            "статистическим доверительным интервалом."
        ),
    }


class KnowledgeChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    score: int | None = Field(ge=0, le=3)
    ignored: bool | None = None


class LexemeCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    cue_id: str
    lemma: str = Field(min_length=1, max_length=100)
    part_of_speech: str


def create_knowledge_router(library: MaterialLibrary) -> APIRouter:
    router = APIRouter(prefix="/api")
    service = KnowledgeService(library)

    @router.put("/lexemes/{lexeme_id}/knowledge")
    def update(lexeme_id: str, change: KnowledgeChange) -> dict[str, Any]:
        return service.set_manual(lexeme_id, change.score, change.ignored)

    @router.get("/dictionary")
    def dictionary(
        search: str = Query(default="", max_length=100),
        score: Literal["unassessed", "0", "1", "2", "3"] | None = None,
        ignored: bool | None = None,
    ) -> list[dict[str, Any]]:
        return service.dictionary(search, score, ignored)

    @router.put("/materials/{material_id}/occurrences/{token_id}/lexeme")
    def correct(material_id: str, token_id: str, change: LexemeCorrection) -> dict[str, Any]:
        return service.correct_lexeme(
            material_id, change.cue_id, token_id, change.lemma, change.part_of_speech
        )

    @router.get("/materials/{material_id}/difficulty")
    def difficulty(material_id: str) -> dict[str, Any]:
        return library.get_material(material_id)["difficulty"]  # type: ignore[no-any-return]

    return router
