"""Persistent skippable diagnosis; only completed validated results change familiarity."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.app.ai_connection.service import ConnectionService
from backend.app.diagnostic_data import POOL_VERSION, pool
from backend.app.materials import MaterialError, MaterialLibrary


class DiagnosticAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    item_id: str
    answer: str = Field(default="", max_length=2000)
    action: Literal["answer", "dont_know", "skip"]


class DiagnosticsService:
    def __init__(self, library: MaterialLibrary):
        self.library = library

    @staticmethod
    def public_session(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "status": row["status"],
            "pool_version": row["pool_version"],
            "items": [
                {key: item[key] for key in ("id", "kind", "group", "prompt")}
                for item in json.loads(row["items_json"])
            ],
            "answers": json.loads(row["answers_json"]),
            "summary": json.loads(row["summary_json"]) if row["summary_json"] else None,
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

    def start(self) -> dict[str, Any]:
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT * FROM diagnostic_sessions WHERE status='in_progress' "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if active:
                return self.public_session(active)
            session_id = uuid.uuid4().hex
            generation = db.execute(
                "SELECT value FROM storage_metadata WHERE key='generation'"
            ).fetchone()[0]
            db.execute(
                "INSERT INTO diagnostic_sessions VALUES(?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    "in_progress",
                    POOL_VERSION,
                    generation,
                    json.dumps(pool(), ensure_ascii=False),
                    "[]",
                    None,
                    datetime.now(UTC).isoformat(),
                ),
            )
            return self.public_session(self.row(db, session_id))

    def answer(self, session_id: str, answer: DiagnosticAnswer) -> dict[str, Any]:
        if answer.action == "answer" and not answer.answer.strip():
            raise MaterialError("Введите ответ или выберите «Не знаю» / «Пропустить».")
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.row(db, session_id)
            if row["status"] != "in_progress":
                raise HTTPException(409, "Эта диагностика уже завершена.")
            if answer.item_id not in {item["id"] for item in json.loads(row["items_json"])}:
                raise MaterialError("Задание не относится к этой диагностике.")
            answers = [
                item
                for item in json.loads(row["answers_json"])
                if item["item_id"] != answer.item_id
            ]
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

    async def complete(
        self, session_id: str, connection: ConnectionService, mcp_url: str
    ) -> dict[str, Any]:
        from backend.app.agents.diagnostics import run_diagnosis

        with self.library.connect() as db:
            snapshot = dict(self.row(db, session_id))
        if snapshot["status"] == "completed":
            return self.get(session_id)
        if snapshot["status"] != "in_progress":
            raise HTTPException(409, "Диагностика прервана. Начните новую.")
        items = json.loads(snapshot["items_json"])
        answers = json.loads(snapshot["answers_json"])
        if {item["id"] for item in items} != {answer["item_id"] for answer in answers}:
            raise MaterialError("Ответьте на все задания или отметьте их как пропущенные.")
        result = await connection.execute(
            lambda model: run_diagnosis(
                model=model,
                mcp_url=mcp_url,
                items=items,
                answers=answers,
            )
        )
        grades = {grade.item_id: grade.model_dump() for grade in result.grades}
        # Model output is checked again at the application boundary before any writes.
        if len(grades) != len(items) or set(grades) != {item["id"] for item in items}:
            raise MaterialError("AI вернул оценки для неизвестных или повторных заданий.")
        answers_by_id = {answer["item_id"]: answer for answer in answers}
        for item_id, grade in grades.items():
            action = answers_by_id[item_id]["action"]
            if action != "answer":
                grade["correct"] = False if action == "dont_know" else None
        groups = [
            {
                "group": group,
                "correct": sum(
                    grades[item["id"]]["correct"] is True
                    for item in items
                    if item["group"] == group
                ),
                "total": 4,
            }
            for group in (1, 2, 3)
        ]
        for group in groups:
            group["inferred"] = group["correct"] >= 3
        summary = {
            "description": result.description,
            "grades": list(grades.values()),
            "groups": groups,
            "notice": (
                "Это предварительная эвристика по малой выборке, "
                "а не сертифицированный уровень CEFR."
            ),
        }
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.row(db, session_id)
            if row["status"] == "completed":
                return self.public_session(row)
            generation = db.execute(
                "SELECT value FROM storage_metadata WHERE key='generation'"
            ).fetchone()[0]
            if (
                row["status"] != "in_progress"
                or generation != snapshot["generation"]
                or row["answers_json"] != snapshot["answers_json"]
            ):
                raise HTTPException(
                    409, "Диагностика изменилась во время запроса. Повторите завершение."
                )
            version = (
                db.execute("SELECT diagnostic_version FROM learning_profile WHERE id=1").fetchone()[
                    0
                ]
                + 1
            )
            db.execute("DELETE FROM word_knowledge WHERE source='diagnostic'")
            db.execute("DELETE FROM diagnostic_rules")
            for group in groups:
                db.execute(
                    "INSERT INTO diagnostic_rules VALUES(?,?,?,?)",
                    ("en", group["group"], version, int(group["inferred"])),
                )
            for item in items:
                if item["kind"] != "word":
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO lexemes VALUES(?,?,?,?)",
                    (uuid.uuid4().hex, "en", item["lemma"], item["part_of_speech"]),
                )
                lexeme_id = db.execute(
                    "SELECT id FROM lexemes WHERE language='en' AND lemma=? AND part_of_speech=?",
                    (item["lemma"], item["part_of_speech"]),
                ).fetchone()[0]
                correct = grades[item["id"]]["correct"]
                db.execute(
                    "INSERT INTO word_knowledge VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(lexeme_id) DO NOTHING",
                    (
                        lexeme_id,
                        None if correct is None else 2 if correct else 0,
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

    @router.post("/{session_id}/skip")
    def skip(session_id: str) -> dict[str, Any]:
        return diagnostics.skip(session_id)

    @router.post("/{session_id}/complete")
    async def complete(session_id: str) -> dict[str, Any]:
        if service is None:
            raise HTTPException(409, "Сначала подключите подписку.")
        return await diagnostics.complete(session_id, service, mcp_url)

    return router
