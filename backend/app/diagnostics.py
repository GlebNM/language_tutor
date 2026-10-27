"""Persistent adaptive placement; only a completed session changes familiarity."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from backend.app.ai_connection.service import ConnectionService
from backend.app.diagnostic_data import (
    DIAGNOSTIC_VERSION,
    MAX_QUESTIONS,
    MIN_QUESTIONS,
    frequency_group,
    frequency_ranks,
)
from backend.app.materials import MaterialError, MaterialLibrary

SCORES = {"known": 2, "unsure": 1, "unknown": 0, "skip": None}


class DiagnosticAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    item_id: str
    action: Literal["known", "unsure", "unknown", "skip"]


class DiagnosticsService:
    def __init__(self, library: MaterialLibrary):
        self.library = library

    @staticmethod
    def public_session(row: sqlite3.Row) -> dict[str, Any]:
        summary = json.loads(row["summary_json"]) if row["summary_json"] else None
        return {
            "id": row["id"],
            "status": row["status"],
            "format_version": row["pool_version"],
            "min_questions": MIN_QUESTIONS,
            "max_questions": MAX_QUESTIONS,
            "ready_to_complete": bool(summary and summary.get("ready_to_complete")),
            "items": [
                {
                    key: value
                    for key, value in item.items()
                    if key
                    in {
                        "id",
                        "kind",
                        "group",
                        "prompt",
                        "surface",
                        "start_offset",
                        "end_offset",
                    }
                }
                for item in json.loads(row["items_json"])
            ],
            "answers": json.loads(row["answers_json"]),
            "summary": summary if row["status"] == "completed" else None,
        }

    def current(self) -> dict[str, Any]:
        with self.library.connect() as db:
            row = db.execute(
                "SELECT * FROM diagnostic_sessions ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            return {
                "session": self.public_session(row) if row else None,
                "profile": self.library.get_profile(),
            }

    def get(self, session_id: str) -> dict[str, Any]:
        with self.library.connect() as db:
            return self.public_session(self.row(db, session_id))

    @staticmethod
    def row(db: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row: sqlite3.Row | None = db.execute(
            "SELECT * FROM diagnostic_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise KeyError("Диагностика не найдена.")
        return row

    @staticmethod
    def require_active(row: sqlite3.Row | dict[str, Any]) -> None:
        if row["status"] != "in_progress":
            raise HTTPException(409, "Эта диагностика уже завершена.")
        if row["pool_version"] != DIAGNOSTIC_VERSION:
            raise HTTPException(409, "Формат диагностики обновился. Начните новую.")

    def start(self) -> dict[str, Any]:
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT * FROM diagnostic_sessions WHERE status='in_progress' "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if active and active["pool_version"] == DIAGNOSTIC_VERSION:
                return self.public_session(active)
            db.execute("UPDATE diagnostic_sessions SET status='skipped' WHERE status='in_progress'")
            session_id = uuid.uuid4().hex
            generation = db.execute(
                "SELECT value FROM storage_metadata WHERE key='generation'"
            ).fetchone()[0]
            db.execute(
                "INSERT INTO diagnostic_sessions VALUES(?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    "in_progress",
                    DIAGNOSTIC_VERSION,
                    generation,
                    "[]",
                    "[]",
                    None,
                    datetime.now(UTC).isoformat(),
                ),
            )
            return self.public_session(self.row(db, session_id))

    def answer(self, session_id: str, answer: DiagnosticAnswer) -> dict[str, Any]:
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.row(db, session_id)
            self.require_active(row)
            items = json.loads(row["items_json"])
            answers = json.loads(row["answers_json"])
            if answer.item_id not in {item["id"] for item in items}:
                raise MaterialError("Задание не относится к этой диагностике.")
            previous = next((a for a in answers if a["item_id"] == answer.item_id), None)
            if previous is not None:
                if previous != answer.model_dump():
                    raise HTTPException(409, "Ответ уже использован для подбора следующего слова.")
                return self.public_session(row)
            if items[-1]["id"] != answer.item_id:
                raise HTTPException(409, "Ответьте на текущий вопрос.")
            answers.append(answer.model_dump())
            db.execute(
                "UPDATE diagnostic_sessions SET answers_json=? WHERE id=?",
                (json.dumps(answers, ensure_ascii=False), session_id),
            )
            return self.public_session(self.row(db, session_id))

    def skip(self, session_id: str) -> dict[str, Any]:
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.row(db, session_id)
            if row["status"] == "completed":
                raise HTTPException(409, "Эта диагностика уже завершена.")
            db.execute("UPDATE diagnostic_sessions SET status='skipped' WHERE id=?", (session_id,))
            return self.public_session(self.row(db, session_id))

    async def next_question(
        self, session_id: str, connection: ConnectionService | None, mcp_url: str
    ) -> dict[str, Any]:
        from backend.app.agents.diagnostics import question_offsets, run_diagnostic_step

        with self.library.connect() as db:
            snapshot = dict(self.row(db, session_id))
            self.require_active(snapshot)
            items = json.loads(snapshot["items_json"])
            answers = json.loads(snapshot["answers_json"])
            if len(items) > len(answers) or snapshot["summary_json"]:
                return self.public_session(self.row(db, session_id))
            recent = db.execute(
                "SELECT items_json FROM diagnostic_sessions WHERE id<>? "
                "ORDER BY created_at DESC LIMIT 3",
                (session_id,),
            ).fetchall()
        if len(answers) >= MAX_QUESTIONS:
            return self.save_step(
                snapshot,
                None,
                {
                    "ready_to_complete": True,
                    "description": "Сохранена ваша самооценка знакомости слов.",
                    "model": items[-1]["model"],
                    "reasoning_effort": items[-1]["reasoning_effort"],
                },
            )
        if connection is None:
            raise HTTPException(409, "Сначала подключите подписку.")
        excluded = list(
            dict.fromkeys(
                [item["lemma"] for item in items]
                + [
                    item["lemma"]
                    for row in recent
                    for item in json.loads(row["items_json"])
                    if "lemma" in item
                ]
            )
        )[:98]
        settings = connection.settings_store.read().effective()
        step = await connection.execute(
            lambda model: run_diagnostic_step(
                model=model,
                mcp_url=mcp_url,
                items=items,
                answers=answers,
                excluded=excluded,
                reasoning_effort=settings.reasoning_effort,
            ),
            settings=settings,
        )
        provenance = {"model": settings.model, "reasoning_effort": settings.reasoning_effort}
        if step.finish:
            return self.save_step(
                snapshot,
                None,
                {
                    "ready_to_complete": True,
                    "description": step.description,
                    **provenance,
                },
            )
        assert step.question is not None
        question = step.question
        start, end = question_offsets(question)
        item = {
            **question.model_dump(),
            **provenance,
            "id": uuid.uuid4().hex,
            "kind": "word",
            "group": frequency_group(question.lemma),
            "rank": frequency_ranks()[question.lemma],
            "start_offset": start,
            "end_offset": end,
        }
        return self.save_step(snapshot, item, None)

    def save_step(
        self, snapshot: dict[str, Any], item: dict[str, Any] | None, ready: dict[str, Any] | None
    ) -> dict[str, Any]:
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.row(db, snapshot["id"])
            self.require_active(row)
            generation = db.execute(
                "SELECT value FROM storage_metadata WHERE key='generation'"
            ).fetchone()[0]
            if generation != snapshot["generation"]:
                raise HTTPException(409, "Учебные данные изменились. Начните новую диагностику.")
            if (
                row["items_json"] != snapshot["items_json"]
                or row["answers_json"] != snapshot["answers_json"]
                or row["summary_json"] != snapshot["summary_json"]
            ):
                return self.public_session(row)
            items = json.loads(snapshot["items_json"])
            if item:
                items.append(item)
            db.execute(
                "UPDATE diagnostic_sessions SET items_json=?,summary_json=? WHERE id=?",
                (
                    json.dumps(items, ensure_ascii=False),
                    json.dumps(ready, ensure_ascii=False) if ready else None,
                    snapshot["id"],
                ),
            )
            return self.public_session(self.row(db, snapshot["id"]))

    def complete(self, session_id: str) -> dict[str, Any]:
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.row(db, session_id)
            if row["status"] == "completed":
                return self.public_session(row)
            self.require_active(row)
            items = json.loads(row["items_json"])
            answers = json.loads(row["answers_json"])
            ready = json.loads(row["summary_json"]) if row["summary_json"] else None
            if not ready or not ready.get("ready_to_complete") or len(items) != len(answers):
                raise MaterialError("Диагностика ещё не завершена. Продолжите отвечать на вопросы.")
            groups = []
            answers_by_id = {answer["item_id"]: answer["action"] for answer in answers}
            for group in (1, 2, 3):
                choices = [answers_by_id[item["id"]] for item in items if item["group"] == group]
                total = sum(choice != "skip" for choice in choices)
                known = choices.count("known")
                groups.append(
                    {
                        "group": group,
                        "known": known,
                        "total": total,
                        "unsure": choices.count("unsure"),
                        "unknown": choices.count("unknown"),
                        "inferred": known >= 4 and total > 0 and known / total >= 0.8,
                    }
                )
            summary = {
                **ready,
                "groups": groups,
                "basis": "self_report",
                "grades": [
                    {
                        "item_id": item["id"],
                        "word": item["surface"],
                        "prompt": item["prompt"],
                        "meaning": item["meaning"],
                        "model": item["model"],
                        "reasoning_effort": item["reasoning_effort"],
                        "action": answers_by_id[item["id"]],
                        "score": SCORES[answers_by_id[item["id"]]],
                    }
                    for item in items
                ],
                "notice": (
                    "Это самооценка знакомости на малой выборке, а не проверенный уровень CEFR."
                ),
            }
            version = (
                db.execute("SELECT diagnostic_version FROM learning_profile WHERE id=1").fetchone()[
                    0
                ]
                + 1
            )
            db.execute("DELETE FROM word_knowledge WHERE source='diagnostic'")
            db.execute("DELETE FROM diagnostic_rules")
            for group_result in groups:
                db.execute(
                    "INSERT INTO diagnostic_rules VALUES(?,?,?,?)",
                    ("en", group_result["group"], version, int(group_result["inferred"])),
                )
            for item in items:
                db.execute(
                    "INSERT OR IGNORE INTO lexemes VALUES(?,?,?,?)",
                    (uuid.uuid4().hex, "en", item["lemma"], item["part_of_speech"]),
                )
                lexeme_id = db.execute(
                    "SELECT id FROM lexemes WHERE language='en' AND lemma=? AND part_of_speech=?",
                    (item["lemma"], item["part_of_speech"]),
                ).fetchone()[0]
                db.execute(
                    "INSERT INTO word_knowledge VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(lexeme_id) DO NOTHING",
                    (
                        lexeme_id,
                        SCORES[answers_by_id[item["id"]]],
                        0,
                        "diagnostic",
                        version,
                        datetime.now(UTC).isoformat(),
                    ),
                )
            db.execute(
                "UPDATE learning_profile SET diagnostic_version=?,revision=revision+1 WHERE id=1",
                (version,),
            )
            db.execute(
                "UPDATE diagnostic_sessions SET status='completed',summary_json=? WHERE id=?",
                (json.dumps(summary, ensure_ascii=False), session_id),
            )
            from backend.app.progress import ProgressService

            ProgressService(self.library).capture(db, reason="diagnostic")
            return self.public_session(self.row(db, session_id))


def create_diagnostics_router(
    library: MaterialLibrary,
    *,
    service: ConnectionService | None = None,
    mcp_url: str = "http://mcp:8001/mcp",
) -> APIRouter:
    router = APIRouter(prefix="/api/diagnostics")
    diagnostics = DiagnosticsService(library)

    @router.get("")
    def current() -> dict[str, Any]:
        return diagnostics.current()

    @router.post("")
    def start() -> dict[str, Any]:
        return diagnostics.start()

    @router.post("/{session_id}/answers")
    def answer(session_id: str, answer: DiagnosticAnswer) -> dict[str, Any]:
        return diagnostics.answer(session_id, answer)

    @router.post("/{session_id}/next")
    async def next_question(session_id: str) -> dict[str, Any]:
        return await diagnostics.next_question(session_id, service, mcp_url)

    @router.post("/{session_id}/skip")
    def skip(session_id: str) -> dict[str, Any]:
        return diagnostics.skip(session_id)

    @router.post("/{session_id}/complete")
    def complete(session_id: str) -> dict[str, Any]:
        return diagnostics.complete(session_id)

    return router
