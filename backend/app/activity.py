"""Persistent learning actions, recorded with the operation that accepted them."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from backend.app.learning_context import LearningContext, bounded_json
from backend.app.materials import MaterialLibrary

ActivityType = Literal[
    "word_selected",
    "translation_received",
    "knowledge_changed",
    "lexeme_corrected",
    "message_sent",
    "cue_listened",
    "viewing_confirmed",
]


class WordSelectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    material_id: str = Field(min_length=1, max_length=100)
    cue_id: str = Field(min_length=1, max_length=100)
    token_id: str = Field(min_length=1, max_length=100)
    viewing_id: str | None = Field(default=None, min_length=1, max_length=100)
    request_id: str = Field(
        default_factory=lambda: str(uuid4()),
        min_length=8,
        max_length=100,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class ActivityService:
    def __init__(self, library: MaterialLibrary):
        self.library = library

    def record(
        self,
        db: sqlite3.Connection,
        event_type: ActivityType,
        *,
        lexeme_id: str | None = None,
        material_id: str | None = None,
        cue_id: str | None = None,
        token_id: str | None = None,
        conversation_id: str | None = None,
        message_id: str | None = None,
        message_token_id: str | None = None,
        request_id: str | None = None,
        deduplication_key: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> int:
        # Use the caller's transaction so a failed operation cannot leave a success event.
        if message_token_id is not None:
            row = db.execute(
                "SELECT l.*,o.surface FROM message_occurrences o "
                "JOIN lexemes l ON l.id=o.lexeme_id "
                "WHERE o.id=? AND o.conversation_id=? AND o.message_id=? AND o.lexeme_id=?",
                (message_token_id, conversation_id, message_id, lexeme_id),
            ).fetchone()
        elif token_id is not None:
            row = db.execute(
                "SELECT l.*,o.surface FROM occurrences o JOIN lexemes l ON l.id=o.lexeme_id "
                "WHERE o.id=? AND o.cue_id=? AND o.material_id=? AND o.lexeme_id=?",
                (token_id, cue_id, material_id, lexeme_id),
            ).fetchone()
        else:
            row = db.execute("SELECT * FROM lexemes WHERE id=?", (lexeme_id,)).fetchone()
        if row is None and (
            lexeme_id is not None
            or event_type not in {"message_sent", "cue_listened", "viewing_confirmed"}
        ):
            raise KeyError("Слово и реплика должны принадлежать выбранному материалу.")
        word = (
            {key: row[key][:100] for key in ("language", "lemma", "part_of_speech")}
            if row is not None
            else {}
        )
        if token_id is not None or message_token_id is not None:
            word["surface"] = row["surface"][:100]
        cursor = db.execute(
            "INSERT INTO learning_activity(event_type,occurred_at,language,material_id,cue_id,"
            "token_id,lexeme_id,request_id,deduplication_key,word_json,data_json,"
            "conversation_id,message_id,message_token_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_type,
                datetime.now(UTC).isoformat(timespec="microseconds"),
                row["language"] if row else self.library.get_profile()["target_language"],
                material_id,
                cue_id,
                token_id,
                lexeme_id,
                request_id,
                deduplication_key,
                json.dumps(word, ensure_ascii=False),
                json.dumps(data or {}, ensure_ascii=False),
                conversation_id,
                message_id,
                message_token_id,
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def select_word(self, incoming: WordSelectionInput) -> dict[str, Any]:
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            selected = self.library.selection(
                incoming.material_id, incoming.cue_id, incoming.token_id
            )
            key = "selection:" + incoming.request_id
            existing = db.execute(
                "SELECT * FROM learning_activity WHERE deduplication_key=?", (key,)
            ).fetchone()
            if existing is not None:
                if any(
                    existing[name] != getattr(incoming, name)
                    for name in ("material_id", "cue_id", "token_id")
                ) or json.loads(existing["data_json"]).get("viewing_id") != incoming.viewing_id:
                    raise HTTPException(
                        409, "Этот request_id уже относится к другому выбору слова."
                    )
                selected["activity_id"] = existing["id"]
                return selected
            data: dict[str, Any] = {}
            if incoming.viewing_id is not None:
                from backend.app.viewing import ViewingService

                prepared = ViewingService(self.library).record_click(
                    db, incoming.viewing_id, selected
                )
                data = {
                    "selection_origin": "current_subtitles",
                    "viewing_id": incoming.viewing_id,
                    "viewing_active": prepared,
                }
            selected["activity_id"] = self.record(
                db,
                "word_selected",
                lexeme_id=selected["token"]["lexeme_id"],
                material_id=incoming.material_id,
                cue_id=incoming.cue_id,
                token_id=incoming.token_id,
                request_id=incoming.request_id,
                deduplication_key=key,
                data=data,
            )
            return selected

    def recent(
        self,
        *,
        language: str = "en",
        event_type: ActivityType | None = None,
        material_id: str | None = None,
        lexeme_id: str | None = None,
        since: datetime | None = None,
        before_id: int | None = None,
        limit: int = 20,
        bounded: bool = False,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100 or (before_id is not None and before_id < 1):
            raise ValueError("Допускается от 1 до 100 событий и положительный before_id.")
        if since is not None and since.utcoffset() is None:
            raise ValueError("Укажите часовой пояс времени since.")
        conditions, params = ["a.language=?"], [language]
        for column, value in (
            ("event_type", event_type),
            ("material_id", material_id),
            ("lexeme_id", lexeme_id),
        ):
            if value is not None:
                conditions.append(f"a.{column}=?")
                params.append(value)
        if since is not None:
            conditions.append("a.occurred_at>=?")
            params.append(since.astimezone(UTC).isoformat(timespec="microseconds"))
        if before_id is not None:
            conditions.append("a.id<?")
            params.append(str(before_id))
        with self.library.connect() as db:
            if language != self.library.get_profile()["target_language"]:
                raise ValueError("Этот язык не относится к учебному профилю.")
            rows = db.execute(
                "SELECT a.*,c.text,c.start_ms,c.end_ms,m.youtube_video_id,m.version,m.title "
                "FROM learning_activity a LEFT JOIN cues c ON c.id=a.cue_id "
                "LEFT JOIN materials m ON m.id=a.material_id WHERE "
                + " AND ".join(conditions)
                + " ORDER BY a.id DESC LIMIT ?",
                [*params, limit + 1],
            ).fetchall()
        events = []
        for row in rows[:limit]:
            event = {
                name: row[name]
                for name in (
                    "id",
                    "event_type",
                    "schema_version",
                    "occurred_at",
                    "language",
                    "material_id",
                    "cue_id",
                    "token_id",
                    "lexeme_id",
                    "request_id",
                    "conversation_id",
                    "message_id",
                    "message_token_id",
                )
            }
            event["word"] = json.loads(row["word_json"])
            event["data"] = json.loads(row["data_json"])
            event["context"] = LearningContext.source(dict(row)) if row["cue_id"] else None
            if row["conversation_id"] and row["message_id"]:
                if not bounded or row["conversation_id"] == conversation_id:
                    from backend.app.message_words import MessageWords

                    try:
                        event["context"] = MessageWords(self.library).context(
                            row["conversation_id"],
                            row["message_id"],
                        )
                        event["context"]["text"] = event["context"]["text"][:1000]
                    except KeyError:
                        event["context"] = None
            if bounded and row["conversation_id"] and row["conversation_id"] != conversation_id:
                event["context"] = None
                event["data"] = {
                    key: value
                    for key, value in event["data"].items()
                    if key in {"previous", "current"}
                }
                event["omitted_fields"] = ["context", "data.conversation_content"]
            events.append(event)
        result = {
            "events": events,
            "has_more": len(rows) > limit,
            "next_before_id": events[-1]["id"] if len(rows) > limit and events else None,
            "notice": (
                "События подтверждают действия в приложении, "
                "но не знание слова и не просмотр видео."
            ),
        }
        if bounded:
            # Prefer complete events. JSON escapes can make even a single valid card too large.
            while True:
                try:
                    bounded_json(result)
                    break
                except ValueError:
                    if len(events) <= 1:
                        event = events[0]
                        card = event["data"].get("card")
                        if card is not None and len(card) > 1:
                            event["omitted_fields"] = [
                                "data.card." + key for key in card if key != "translation"
                            ]
                            event["data"]["card"] = {"translation": card["translation"]}
                        elif event["context"] is not None:
                            event.setdefault("omitted_fields", []).append("context")
                            event["context"] = None
                        else:
                            raise
                        continue
                    events.pop()
                    result.update(has_more=True, next_before_id=events[-1]["id"])
        return result


def create_activity_router(library: MaterialLibrary) -> APIRouter:
    service = ActivityService(library)
    router = APIRouter(prefix="/api/activity", tags=["Learning activity"])

    @router.post("/word-selections")
    def select_word(incoming: WordSelectionInput) -> dict[str, Any]:
        return service.select_word(incoming)

    @router.get("")
    def recent(
        language: str = "en",
        event_type: ActivityType | None = None,
        material_id: str | None = Query(default=None, min_length=1, max_length=100),
        lexeme_id: str | None = Query(default=None, min_length=1, max_length=100),
        since: AwareDatetime | None = None,
        before_id: int | None = Query(default=None, ge=1),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        try:
            return service.recent(
                language=language,
                event_type=event_type,
                material_id=material_id,
                lexeme_id=lexeme_id,
                since=since,
                before_id=before_id,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return router
