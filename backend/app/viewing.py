"""Collect viewing evidence and apply a reviewed vocabulary batch explicitly."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.app.activity import ActivityService
from backend.app.knowledge import KnowledgeService, knowledge_value
from backend.app.materials import MaterialLibrary
from backend.app.vocabulary import VocabularyPolicy


class LearningSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


class ListenedCuesInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    cue_ids: list[str] = Field(max_length=100)


class ViewingChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    lexeme_id: str = Field(min_length=1, max_length=100)
    score: int = Field(ge=0, le=3)
    included: bool = True


class ViewingService:
    def __init__(self, library: MaterialLibrary):
        self.library = library

    def settings(self, db: sqlite3.Connection | None = None) -> dict[str, Any]:
        if db is None:
            with self.library.connect() as connection:
                return self.settings(connection)
        row = db.execute("SELECT * FROM learning_settings WHERE id=1").fetchone()
        return {"enabled": bool(row["enabled"]), "epoch": row["epoch"]}

    def set_settings(self, enabled: bool) -> dict[str, Any]:
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.settings(db)["enabled"] != enabled:
                db.execute(
                    "UPDATE learning_settings SET enabled=?,epoch=epoch+1 WHERE id=1", (enabled,)
                )
                if not enabled:
                    db.execute("DELETE FROM viewings WHERE result_json IS NULL")
            return self.settings(db)

    def _pending(self, db: sqlite3.Connection, viewing_id: str) -> sqlite3.Row:
        row: sqlite3.Row | None = db.execute(
            "SELECT * FROM viewings WHERE id=?", (viewing_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(409, "Результаты просмотра сброшены. Обновите данные.")
        settings = self.settings(db)
        if row["result_json"] is not None:
            raise HTTPException(409, "Этот просмотр уже сохранён. Начните новый просмотр.")
        if not settings["enabled"] or settings["epoch"] != row["epoch"]:
            raise HTTPException(409, "Пополнение словаря выключено. Обновите настройки.")
        return row

    def resume(self, material_id: str) -> dict[str, Any]:
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            material = db.execute(
                "SELECT kind FROM materials WHERE id=?", (material_id,)
            ).fetchone()
            if material is None:
                raise KeyError("Материал не найден.")
            if material["kind"] != "video":
                raise HTTPException(422, "Прослушивание доступно только для видео.")
            settings = self.settings(db)
            if not settings["enabled"]:
                return {"viewing_id": None, **settings}
            row = db.execute(
                "SELECT id FROM viewings WHERE material_id=? AND result_json IS NULL",
                (material_id,),
            ).fetchone()
            viewing_id = row["id"] if row else uuid.uuid4().hex
            if row is None:
                db.execute(
                    "INSERT INTO viewings(id,material_id,epoch,created_at) VALUES(?,?,?,?)",
                    (viewing_id, material_id, settings["epoch"], datetime.now(UTC).isoformat()),
                )
            return {"viewing_id": viewing_id, **settings}

    def listen(self, viewing_id: str, cue_ids: list[str]) -> dict[str, int]:
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            viewing = self._pending(db, viewing_id)
            for cue_id in dict.fromkeys(cue_ids):
                if (
                    db.execute(
                        "SELECT id FROM cues WHERE id=? AND material_id=?",
                        (cue_id, viewing["material_id"]),
                    ).fetchone()
                    is None
                ):
                    raise KeyError("Реплика не принадлежит этому просмотру.")
                inserted = db.execute(
                    "INSERT OR IGNORE INTO viewing_cues VALUES(?,?)", (viewing_id, cue_id)
                ).rowcount
                if inserted:
                    ActivityService(self.library).record(
                        db,
                        "cue_listened",
                        material_id=viewing["material_id"],
                        cue_id=cue_id,
                        data={"viewing_id": viewing_id},
                    )
            return {
                "listened_cues": db.execute(
                    "SELECT COUNT(*) FROM viewing_cues WHERE viewing_id=?", (viewing_id,)
                ).fetchone()[0]
            }

    def record_click(
        self, db: sqlite3.Connection, viewing_id: str, selected: dict[str, Any]
    ) -> bool:
        # A stale player must not revive a draft after disabling the setting or saving it.
        row = db.execute("SELECT * FROM viewings WHERE id=?", (viewing_id,)).fetchone()
        if row is None:
            return False
        if row["material_id"] != selected["material_id"]:
            raise HTTPException(409, "Слово относится к другому просмотру.")
        settings = self.settings(db)
        if row["result_json"] is not None or not settings["enabled"]:
            return False
        if settings["epoch"] != row["epoch"]:
            return False
        db.execute(
            "INSERT OR IGNORE INTO viewing_clicks VALUES(?,?)",
            (viewing_id, selected["token"]["id"]),
        )
        return True

    def _entries(self, db: sqlite3.Connection, viewing: sqlite3.Row) -> list[dict[str, Any]]:
        VocabularyPolicy(self.library).ensure_material(db, viewing["material_id"])
        rows = db.execute(
            "SELECT l.*,o.id AS token_id,o.cue_id,o.start_offset,c.text,c.position,"
            "v.reason AS vocabulary_reason,"
            "CASE WHEN vc.token_id IS NULL THEN 0 ELSE 1 END AS clicked "
            "FROM occurrences o JOIN lexemes l ON l.id=o.lexeme_id "
            "JOIN cues c ON c.id=o.cue_id "
            "JOIN occurrence_vocabulary v ON v.token_id=o.id "
            "LEFT JOIN viewing_clicks vc ON vc.token_id=o.id AND vc.viewing_id=? "
            "LEFT JOIN viewing_cues vs ON vs.cue_id=o.cue_id AND vs.viewing_id=? "
            "WHERE o.material_id=? AND (vc.token_id IS NOT NULL OR vs.cue_id IS NOT NULL) "
            "ORDER BY clicked DESC,c.position,o.start_offset",
            (viewing["id"], viewing["id"], viewing["material_id"]),
        ).fetchall()
        choices = {
            row["lexeme_id"]: row
            for row in db.execute(
                "SELECT * FROM viewing_choices WHERE viewing_id=?", (viewing["id"],)
            )
        }
        entries: dict[str, dict[str, Any]] = {}
        knowledge = KnowledgeService(self.library)
        for row in rows:
            lexeme_id = row["id"]
            if row["vocabulary_reason"] is not None:
                continue
            if lexeme_id in entries:
                continue
            # Keep soundtrack labels out of a vocabulary assessment, as in difficulty.
            if not row["clicked"] and any(
                start <= row["start_offset"] < end
                for start, end in (
                    match.span() for match in re.finditer(r"\[[^\]]*\]", row["text"])
                )
            ):
                continue
            current = knowledge.get(lexeme_id, db=db)
            if current["source"] is not None or current["score"] is not None or current["ignored"]:
                continue
            choice = choices.get(lexeme_id)
            suggested = 0 if row["clicked"] else 2
            entries[lexeme_id] = {
                "lexeme_id": lexeme_id,
                "lemma": row["lemma"],
                "part_of_speech": row["part_of_speech"],
                "suggested_score": suggested,
                "score": choice["score"] if choice else suggested,
                "included": bool(choice["included"]) if choice else True,
                "preparation": "click" if row["clicked"] else "listening",
                "cue_id": row["cue_id"],
                "token_id": row["token_id"],
            }
        return sorted(entries.values(), key=lambda entry: (entry["score"], entry["lemma"]))

    def summary(self, viewing_id: str) -> dict[str, Any]:
        with self.library.connect() as db:
            viewing = self._pending(db, viewing_id)
            return {"viewing_id": viewing_id, "entries": self._entries(db, viewing)}

    def choose(self, viewing_id: str, choice: ViewingChoice) -> dict[str, Any]:
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            viewing = self._pending(db, viewing_id)
            if not any(
                entry["lexeme_id"] == choice.lexeme_id for entry in self._entries(db, viewing)
            ):
                raise HTTPException(409, "Слово уже оценено или не входит в сводку. Обновите её.")
            db.execute(
                "INSERT INTO viewing_choices VALUES(?,?,?,?) "
                "ON CONFLICT(viewing_id,lexeme_id) DO UPDATE SET "
                "score=excluded.score,included=excluded.included",
                (viewing_id, choice.lexeme_id, choice.score, choice.included),
            )
            return {"viewing_id": viewing_id, "entries": self._entries(db, viewing)}

    def confirm(self, viewing_id: str) -> dict[str, Any]:
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM viewings WHERE id=?", (viewing_id,)).fetchone()
            if existing is not None and existing["result_json"] is not None:
                result: dict[str, Any] = json.loads(existing["result_json"])
                return result
            viewing = self._pending(db, viewing_id)
            entries = self._entries(db, viewing)
            applied = [entry for entry in entries if entry["included"]]
            activity = ActivityService(self.library)
            now = datetime.now(UTC).isoformat()
            for entry in applied:
                db.execute(
                    "INSERT INTO word_knowledge VALUES(?,?,0,'manual',NULL,?)",
                    (entry["lexeme_id"], entry["score"], now),
                )
                activity.record(
                    db,
                    "knowledge_changed",
                    lexeme_id=entry["lexeme_id"],
                    material_id=viewing["material_id"],
                    cue_id=entry["cue_id"],
                    token_id=entry["token_id"],
                    data={
                        "previous": knowledge_value(None),
                        "current": knowledge_value(entry["score"], source="manual"),
                        "reason": "viewing_summary",
                        "viewing_id": viewing_id,
                        "preparation": entry["preparation"],
                        "suggested_score": entry["suggested_score"],
                    },
                )
            if applied:
                db.execute("UPDATE learning_profile SET revision=revision+1 WHERE id=1")
                from backend.app.progress import ProgressService

                ProgressService(self.library).capture(db)
            result = {"viewing_id": viewing_id, "saved": len(applied)}
            activity.record(
                db, "viewing_confirmed", material_id=viewing["material_id"], data=result
            )
            db.execute(
                "UPDATE viewings SET result_json=? WHERE id=?", (json.dumps(result), viewing_id)
            )
            return result


def create_viewing_router(library: MaterialLibrary) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["Viewing vocabulary"])
    service = ViewingService(library)

    @router.get("/settings/learning")
    def settings() -> dict[str, Any]:
        return service.settings()

    @router.put("/settings/learning")
    def set_settings(incoming: LearningSettingsInput) -> dict[str, Any]:
        return service.set_settings(incoming.enabled)

    @router.post("/materials/{material_id}/viewing")
    def resume(material_id: str) -> dict[str, Any]:
        return service.resume(material_id)

    @router.post("/viewings/{viewing_id}/cues")
    def listen(viewing_id: str, incoming: ListenedCuesInput) -> dict[str, int]:
        return service.listen(viewing_id, incoming.cue_ids)

    @router.get("/viewings/{viewing_id}")
    def summary(viewing_id: str) -> dict[str, Any]:
        return service.summary(viewing_id)

    @router.put("/viewings/{viewing_id}/choice")
    def choose(viewing_id: str, incoming: ViewingChoice) -> dict[str, Any]:
        return service.choose(viewing_id, incoming)

    @router.post("/viewings/{viewing_id}/confirm")
    def confirm(viewing_id: str) -> dict[str, Any]:
        return service.confirm(viewing_id)

    return router
