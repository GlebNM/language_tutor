"""Personal expression meanings, immutable source anchors and explicit familiarity."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.agents.expressions import ExpressionCandidate, normalized
from backend.app.knowledge import knowledge_value
from backend.app.learning_context import LearningContext, bounded_json
from backend.app.materials import MaterialError, MaterialLibrary
from backend.app.message_words import MessageWords


def now() -> str:
    return datetime.now(UTC).isoformat()


class ExpressionSource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    material_id: str | None = Field(default=None, min_length=1, max_length=100)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=100)
    message_id: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def one_source(self) -> Self:
        if self.material_id:
            if self.conversation_id or self.message_id:
                raise ValueError("Выберите один источник выражения.")
        elif not self.conversation_id or not self.message_id:
            raise ValueError("Нужен материал либо завершённое сообщение разговора.")
        return self

    @property
    def key(self) -> str:
        return (
            "material:" + self.material_id
            if self.material_id
            else f"message:{self.conversation_id}:{self.message_id}"
        )


class ExpressionSelection(ExpressionSource):
    token_ids: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        min_length=1, max_length=24
    )
    manual: bool = False


class ExpressionKnowledge(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    score: int | None = Field(default=1, ge=0, le=3)
    ignored: bool = False


class ExpressionStore:
    def __init__(self, library: MaterialLibrary):
        self.library = library

    def source(self, selected: ExpressionSource) -> dict[str, Any]:
        if selected.material_id:
            with self.library.connect() as db:
                material = db.execute(
                    "SELECT * FROM materials WHERE id=?",
                    (selected.material_id,),
                ).fetchone()
                if material is None:
                    raise KeyError("Материал не найден.")
                cues = [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM cues WHERE material_id=? ORDER BY position",
                        (selected.material_id,),
                    )
                ]
                tokens: dict[str, list[dict[str, Any]]] = {}
                for row in db.execute(
                    "SELECT o.*,l.lemma,l.part_of_speech FROM occurrences o "
                    "JOIN lexemes l ON l.id=o.lexeme_id WHERE o.material_id=? "
                    "ORDER BY o.start_offset",
                    (selected.material_id,),
                ):
                    tokens.setdefault(row["cue_id"], []).append(dict(row))
                for cue in cues:
                    cue["tokens"] = tokens.get(cue["id"], [])
                return {
                    "key": selected.key,
                    "ref": selected.model_dump(),
                    "cues": cues,
                    "title": material["title"],
                    "language": material["language"],
                    "youtube_video_id": material["youtube_video_id"],
                }
        assert selected.conversation_id and selected.message_id
        message = MessageWords(self.library).words(selected.conversation_id, selected.message_id)
        return {
            "key": selected.key,
            "ref": selected.model_dump(),
            "language": "en",
            "title": "Сообщение агента",
            "cues": [
                {"id": selected.message_id, "text": message["text"], "tokens": message["tokens"]}
            ],
        }

    def known(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        lemmas = {normalized(t["lemma"]) for c in source["cues"] for t in c["tokens"]}
        with self.library.connect() as db:
            rows = db.execute(
                "SELECT id,canonical,kind,sense_key,translation FROM expressions "
                "WHERE language=? ORDER BY created_at DESC",
                (source["language"],),
            ).fetchall()
        return [dict(row) for row in rows if row["canonical"].split()[0] in lemmas][:100]

    def focus(self, source: dict[str, Any], selected: ExpressionSelection) -> list[dict[str, Any]]:
        all_tokens = [t for c in source["cues"] for t in c["tokens"]]
        positions = {t["id"]: i for i, t in enumerate(all_tokens)}
        ids = selected.token_ids
        if len(ids) != len(set(ids)) or any(key not in positions for key in ids):
            raise MaterialError("Все выбранные слова должны принадлежать одному источнику.")
        if not selected.manual and len(ids) != 1:
            raise MaterialError("Для разбора по нажатию выберите одно слово.")
        order = [positions[key] for key in ids]
        if order != sorted(order):
            raise MaterialError("Выбирайте слова в порядке исходного текста.")
        cue_indexes = [
            i for i, c in enumerate(source["cues"]) if any(t["id"] in ids for t in c["tokens"])
        ]
        if cue_indexes[-1] - cue_indexes[0] > 3 or order[-1] - order[0] > 60:
            raise MaterialError("Выберите короткое выражение в соседних репликах.")
        cues: list[dict[str, Any]] = source["cues"]
        return cues[max(0, cue_indexes[0] - 3) : cue_indexes[-1] + 4]

    def put(
        self,
        source: dict[str, Any],
        candidates: list[ExpressionCandidate],
        *,
        generation: str,
        manual: bool = False,
        job_id: str | None = None,
    ) -> None:
        by_id = {t["id"]: c for c in source["cues"] for t in c["tokens"]}
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT value FROM storage_metadata WHERE key='generation'"
            ).fetchone()[0]
            if current != generation:
                raise MaterialError("Учебные данные изменились. Откройте материал заново.")
            if (
                job_id
                and not db.execute(
                    "SELECT 1 FROM expression_jobs WHERE id=? AND status='running'",
                    (job_id,),
                ).fetchone()
            ):
                raise MaterialError("Разбор выражений уже завершён или отменён.")
            # Revalidate the source before saving a late model response.
            ref = source["ref"]
            if ref["material_id"]:
                exists = db.execute(
                    "SELECT 1 FROM materials WHERE id=?", (ref["material_id"],)
                ).fetchone()
            else:
                exists = db.execute(
                    "SELECT 1 FROM learning_messages WHERE conversation_id=? AND request_id=? "
                    "AND status='complete'",
                    (ref["conversation_id"], ref["message_id"]),
                ).fetchone()
            if not exists:
                raise KeyError("Источник выражения больше не доступен.")
            for candidate in candidates:
                canonical = normalized(candidate.canonical)
                sense = re.sub(r"[\s_]+", "-", normalized(candidate.sense_key))
                existing = db.execute(
                    "SELECT * FROM expressions WHERE language=? AND canonical=? AND sense_key=?",
                    (source["language"], canonical, sense),
                ).fetchone()
                if candidate.existing_id:
                    previous = db.execute(
                        "SELECT * FROM expressions WHERE id=?", (candidate.existing_id,)
                    ).fetchone()
                    if previous and previous["canonical"] == canonical:
                        existing = previous
                identity = existing["id"] if existing else uuid4().hex
                card = {
                    key: getattr(candidate, key)
                    for key in ("translation", "explanation", "example", "usage")
                }
                if existing is None:
                    db.execute(
                        "INSERT INTO expressions VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            identity,
                            source["language"],
                            canonical,
                            candidate.kind,
                            sense,
                            candidate.translation,
                            candidate.explanation,
                            candidate.example,
                            candidate.usage,
                            now(),
                        ),
                    )
                contexts = []
                for cue in source["cues"]:
                    if not any(by_id[key]["id"] == cue["id"] for key in candidate.token_ids):
                        continue
                    context: dict[str, Any] = {"text": cue["text"], "cue_id": cue["id"]}
                    context["anchors"] = [
                        {"start_offset": token["start_offset"], "end_offset": token["end_offset"]}
                        for token in cue["tokens"]
                        if token["id"] in candidate.token_ids
                    ]
                    if ref["material_id"]:
                        context.update(
                            {
                                "material_id": ref["material_id"],
                                "title": source["title"],
                                "start_ms": cue["start_ms"],
                                "end_ms": cue["end_ms"],
                                "youtube_video_id": source["youtube_video_id"],
                                "citation_id": ref["material_id"] + ":" + cue["id"],
                            }
                        )
                    contexts.append(context)
                ids_json = json.dumps(candidate.token_ids)
                row = db.execute(
                    "SELECT * FROM expression_occurrences WHERE source_key=? AND token_ids_json=?",
                    (source["key"], ids_json),
                ).fetchone()
                if row:
                    # A full-track result must not overwrite a manually corrected/dismissed match.
                    if manual:
                        db.execute(
                            "UPDATE expression_occurrences SET expression_id=?,card_json=?,"
                            "origin='manual',dismissed=0 WHERE id=?",
                            (identity, json.dumps(card, ensure_ascii=False), row["id"]),
                        )
                    continue
                db.execute(
                    "INSERT INTO expression_occurrences VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        uuid4().hex,
                        identity,
                        source["key"],
                        ref["material_id"],
                        ref["conversation_id"],
                        ref["message_id"],
                        ids_json,
                        json.dumps(contexts, ensure_ascii=False),
                        json.dumps(card, ensure_ascii=False),
                        "manual" if manual else "automatic",
                        0,
                        now(),
                    ),
                )

    @staticmethod
    def entry(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["saved"] = result.pop("knowledge_id", None) is not None
        result["knowledge"] = knowledge_value(
            result.pop("score", None),
            bool(result.pop("ignored", 0)),
            "manual" if result["saved"] else None,
        )
        return result

    def occurrences(self, key: str) -> list[dict[str, Any]]:
        with self.library.connect() as db:
            rows = db.execute(
                "SELECT e.*,o.id occurrence_id,o.token_ids_json,o.context_json,o.card_json,"
                "o.material_id,o.conversation_id,o.message_id,o.origin,"
                "k.expression_id knowledge_id,k.score,k.ignored "
                "FROM expression_occurrences o JOIN expressions e ON e.id=o.expression_id "
                "LEFT JOIN expression_knowledge k ON k.expression_id=e.id "
                "WHERE o.source_key=? AND o.dismissed=0 ORDER BY o.created_at,o.id",
                (key,),
            ).fetchall()
        result = []
        for row in rows:
            item = self.entry(row)
            item["token_ids"] = json.loads(item.pop("token_ids_json"))
            item["contexts"] = json.loads(item.pop("context_json"))
            item.update(json.loads(item.pop("card_json")))
            result.append(item)
        return result

    def dictionary(self, search: str = "", score: str = "", kind: str = "") -> list[dict[str, Any]]:
        with self.library.connect() as db:
            rows = db.execute(
                "SELECT e.*,k.expression_id knowledge_id,k.score,k.ignored FROM expressions e "
                "JOIN expression_knowledge k ON k.expression_id=e.id ORDER BY k.updated_at DESC",
            ).fetchall()
        return [
            self.entry(row)
            for row in rows
            if (
                not search
                or normalized(search) in normalized(row["canonical"] + " " + row["translation"])
            )
            and (
                not score
                or (score == "unassessed" and row["score"] is None)
                or str(row["score"]) == score
            )
            and (not kind or row["kind"] == kind)
        ]

    def detail(
        self, identity: str, *, conversation_id: str | None = None, scoped: bool = False
    ) -> dict[str, Any]:
        with self.library.connect() as db:
            row = db.execute(
                "SELECT e.*,k.expression_id knowledge_id,k.score,k.ignored FROM expressions e "
                "LEFT JOIN expression_knowledge k ON k.expression_id=e.id WHERE e.id=?",
                (identity,),
            ).fetchone()
            if row is None:
                raise KeyError("Выражение не найдено.")
            result = self.entry(row)
            examples = db.execute(
                "SELECT id,source_key,material_id,conversation_id,message_id,context_json FROM "
                "expression_occurrences WHERE expression_id=? AND dismissed=0 "
                "ORDER BY created_at DESC",
                (identity,),
            ).fetchall()
            result["history"] = [
                dict(event)
                for event in db.execute(
                    "SELECT event_type,occurred_at,data_json FROM expression_events "
                    "WHERE expression_id=? ORDER BY id DESC LIMIT 10",
                    (identity,),
                )
            ]
        result["examples"] = [
            {
                **{key: e[key] for key in ("id", "material_id", "conversation_id", "message_id")},
                "contexts": json.loads(e["context_json"]),
            }
            for e in examples
            if not scoped or e["material_id"] or e["conversation_id"] == conversation_id
        ][:20]
        return result

    def set_knowledge(self, identity: str, value: ExpressionKnowledge) -> dict[str, Any]:
        self.detail(identity)
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT score,ignored FROM expression_knowledge WHERE expression_id=?", (identity,)
            ).fetchone()
            if previous is None or (previous["score"], bool(previous["ignored"])) != (
                value.score,
                value.ignored,
            ):
                db.execute(
                    "INSERT INTO expression_knowledge VALUES(?,?,?,?) ON CONFLICT(expression_id) "
                    "DO UPDATE SET score=excluded.score,ignored=excluded.ignored,"
                    "updated_at=excluded.updated_at",
                    (identity, value.score, int(value.ignored), now()),
                )
                db.execute(
                    "INSERT INTO expression_events(expression_id,event_type,occurred_at,data_json) "
                    "VALUES(?,'knowledge_changed',?,?)",
                    (
                        identity,
                        now(),
                        json.dumps(
                            {
                                "previous": dict(previous) if previous else None,
                                "current": value.model_dump(),
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
                db.execute("UPDATE learning_profile SET revision=revision+1 WHERE id=1")
        return self.detail(identity)

    def dismiss(self, occurrence_id: str) -> dict[str, str]:
        with self.library.connect() as db:
            row = db.execute(
                "SELECT * FROM expression_occurrences WHERE id=?", (occurrence_id,)
            ).fetchone()
            if row is None:
                raise KeyError("Вхождение выражения не найдено.")
            db.execute("UPDATE expression_occurrences SET dismissed=1 WHERE id=?", (occurrence_id,))
        return {"status": "dismissed"}

    def practice(self) -> list[dict[str, Any]]:
        with self.library.connect() as db:
            return [
                {
                    "id": row["id"],
                    "canonical": row["canonical"],
                    "kind": row["kind"],
                    "sense_key": row["sense_key"],
                    "translation": row["translation"],
                    "knowledge": knowledge_value(row["score"], False, "manual"),
                    "updated_at": row["updated_at"],
                    "history": self.history(row["id"]),
                }
                for row in db.execute(
                    "SELECT e.*,k.score,k.updated_at FROM expressions e "
                    "JOIN expression_knowledge k ON k.expression_id=e.id WHERE k.ignored=0 "
                    "AND k.score IN (0,1,2) ORDER BY (k.score IN (0,1)) DESC,"
                    "k.updated_at DESC LIMIT 4"
                )
            ]

    def history(self, identity: str) -> list[dict[str, Any]]:
        with self.library.connect() as db:
            rows = db.execute(
                "SELECT occurred_at,data_json FROM expression_events WHERE expression_id=? "
                "AND event_type='knowledge_changed' ORDER BY id DESC LIMIT 3",
                (identity,),
            ).fetchall()
        return [{"occurred_at": row["occurred_at"], **json.loads(row["data_json"])} for row in rows]

    def context(self, identity: str, conversation_id: str | None = None) -> dict[str, Any]:
        detail = self.detail(identity, conversation_id=conversation_id, scoped=True)
        # The shared meaning is metadata. Contextual explanations/examples may contain
        # details from the originating conversation, so do not send them to another chat.
        result = {
            key: detail[key]
            for key in (
                "id",
                "canonical",
                "kind",
                "sense_key",
                "translation",
                "saved",
                "knowledge",
            )
        }
        result["history"] = self.history(identity)
        # Only subtitle examples are citations; messages remain confined to their conversation.
        cues = []
        for example in detail.pop("examples")[:5]:
            if example["material_id"]:
                for cue in example["contexts"]:
                    with self.library.connect() as db:
                        row = db.execute(
                            "SELECT m.version,c.text,c.start_ms,c.end_ms,m.title,"
                            "m.youtube_video_id,c.material_id,c.id cue_id FROM cues c "
                            "JOIN materials m "
                            "ON m.id=c.material_id WHERE c.id=? AND c.material_id=?",
                            (cue["cue_id"], cue["material_id"]),
                        ).fetchone()
                    if row:
                        cues.append(LearningContext.source(dict(row), text_limit=350))
        result["occurrences"] = cues[:5]
        while True:
            try:
                bounded_json(result)
                return result
            except ValueError:
                if not result["occurrences"]:
                    raise
                result["occurrences"].pop()
