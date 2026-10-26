"""Contextual automatic vocabulary selection, independent of learner familiarity."""

from __future__ import annotations

import json
import re
import sqlite3
from itertools import groupby
from typing import TYPE_CHECKING, Any

from backend.app.language import english_adapter

if TYPE_CHECKING:
    from backend.app.materials import MaterialLibrary

VERSION = "en-vocabulary-1/spacy-3.8.11/en_core_web_sm-3.8.0"
FILLER = re.compile(r"(?:h+m+|m{2,}|u+h+|u+m+)", re.IGNORECASE)
STRETCHED = [
    (
        base,
        re.compile(
            "".join(re.escape(c) + "{" + str(len(list(run))) + ",}" for c, run in groupby(base))
        ),
    )
    for base in ("wow", "hello", "ouch", "oh", "ah", "hey", "yeah", "yes", "no", "whoa")
]
NAME_ENTITIES = {
    "PERSON",
    "ORG",
    "GPE",
    "LOC",
    "FAC",
    "PRODUCT",
    "EVENT",
    "WORK_OF_ART",
    "LAW",
    "LANGUAGE",
    "NORP",
}
CALENDAR_NAMES = set(
    "january february march april may june july august september october november december "
    "monday tuesday wednesday thursday friday saturday sunday".split()
)
TABLES = {
    "video": ("occurrences", "occurrence_vocabulary"),
    "message": ("message_occurrences", "message_vocabulary"),
}


def sound_reason(surface: str) -> str | None:
    word = surface.casefold()
    if FILLER.fullmatch(word):
        return "filler"
    if any(word != base and pattern.fullmatch(word) for base, pattern in STRETCHED):
        return "stretched"
    return None


def vocabulary_value(reason: str | None, knowledge: dict[str, Any]) -> dict[str, Any]:
    return {
        "eligible": reason is None or knowledge["source"] == "manual",
        "automatically_eligible": reason is None,
        "reason": reason,
    }


class VocabularyPolicy:
    def __init__(self, library: MaterialLibrary):
        self.library = library

    @staticmethod
    def _stale(db: sqlite3.Connection, source: str, where: str, args: tuple[str, ...]) -> bool:
        table, cache = TABLES[source]
        return (
            db.execute(
                f"SELECT 1 FROM {table} o LEFT JOIN {cache} v ON v.token_id=o.id "
                f"WHERE {where} AND (v.token_id IS NULL OR v.version<>? "
                "OR v.lexeme_id<>o.lexeme_id) LIMIT 1",
                (*args, VERSION),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _cache(
        db: sqlite3.Connection,
        source: str,
        text: str,
        rows: list[dict[str, Any]],
    ) -> None:
        table, cache = TABLES[source]
        features = english_adapter().vocabulary_features(text)
        for row in rows:
            reason = sound_reason(row["surface"])
            pos, entity = features.get((row["begin"], row["end"]), ("", ""))
            if (
                reason is None
                and row["part_of_speech"] == pos == "PROPN"
                and (
                    entity in NAME_ENTITIES
                    or (entity == "DATE" and row["surface"].casefold() in CALENDAR_NAMES)
                )
            ):
                reason = "proper_name"
            db.execute(
                f"INSERT INTO {cache}(token_id,lexeme_id,reason,version) "
                f"SELECT id,lexeme_id,?,? FROM {table} WHERE id=? AND lexeme_id=? "
                "ON CONFLICT(token_id) DO UPDATE SET lexeme_id=excluded.lexeme_id,"
                "reason=excluded.reason,version=excluded.version",
                (reason, VERSION, row["id"], row["lexeme_id"]),
            )

    def ensure_material(self, db: sqlite3.Connection, material_id: str) -> None:
        if not self._stale(db, "video", "o.material_id=?", (material_id,)):
            return
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        # Re-read under the writer lock: corrections and data reset cannot race the cache.
        if not self._stale(db, "video", "o.material_id=?", (material_id,)):
            return
        cues = db.execute(
            "SELECT id,text FROM cues WHERE material_id=? ORDER BY position", (material_id,)
        ).fetchall()
        offsets = {}
        position = 0
        for cue in cues:
            offsets[cue["id"]] = position
            position += len(cue["text"]) + 1
        rows = [
            dict(row)
            for row in db.execute(
                "SELECT o.*,l.part_of_speech FROM occurrences o "
                "JOIN lexemes l ON l.id=o.lexeme_id WHERE o.material_id=?",
                (material_id,),
            )
        ]
        for row in rows:
            row["begin"] = offsets[row["cue_id"]] + row["start_offset"]
            row["end"] = offsets[row["cue_id"]] + row["end_offset"]
        self._cache(db, "video", "\n".join(cue["text"] for cue in cues), rows)

    def ensure_message(
        self,
        db: sqlite3.Connection,
        conversation_id: str,
        message_id: str,
    ) -> None:
        where = "o.conversation_id=? AND o.message_id=?"
        args = (conversation_id, message_id)
        if not self._stale(db, "message", where, args):
            return
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT response_json FROM learning_messages WHERE conversation_id=? "
            "AND request_id=? AND status='complete'",
            args,
        ).fetchone()
        if row is None or not row["response_json"]:
            return
        rows = [
            dict(token)
            for token in db.execute(
                "SELECT o.*,l.part_of_speech,o.start_offset AS begin,o.end_offset AS end "
                "FROM message_occurrences o JOIN lexemes l ON l.id=o.lexeme_id "
                f"WHERE {where}",
                args,
            )
        ]
        self._cache(db, "message", json.loads(row["response_json"])["answer"], rows)

    def eligible_lexemes(self, db: sqlite3.Connection) -> set[str]:
        # Backfill only missing/outdated contexts; identities and knowledge stay intact.
        for row in db.execute(
            "SELECT DISTINCT o.material_id FROM occurrences o "
            "LEFT JOIN occurrence_vocabulary v ON v.token_id=o.id "
            "WHERE v.token_id IS NULL OR v.version<>? OR v.lexeme_id<>o.lexeme_id",
            (VERSION,),
        ).fetchall():
            self.ensure_material(db, row[0])
        for row in db.execute(
            "SELECT DISTINCT o.conversation_id,o.message_id FROM message_occurrences o "
            "LEFT JOIN message_vocabulary v ON v.token_id=o.id "
            "WHERE v.token_id IS NULL OR v.version<>? OR v.lexeme_id<>o.lexeme_id",
            (VERSION,),
        ).fetchall():
            self.ensure_message(db, row[0], row[1])
        eligible = {
            row[0]
            for row in db.execute(
                "SELECT lexeme_id FROM word_knowledge WHERE source='manual' "
                "UNION SELECT lexeme_id FROM occurrence_vocabulary WHERE reason IS NULL "
                "UNION SELECT lexeme_id FROM message_vocabulary WHERE reason IS NULL"
            )
        }
        # Diagnostic items need not have a transcript occurrence. Without context,
        # retain them conservatively, except for unambiguous sound spellings.
        for row in db.execute(
            "SELECT l.id,l.lemma FROM lexemes l "
            "JOIN word_knowledge k ON k.lexeme_id=l.id AND k.source='diagnostic' WHERE "
            "NOT EXISTS(SELECT 1 FROM occurrences o WHERE o.lexeme_id=l.id) AND "
            "NOT EXISTS(SELECT 1 FROM message_occurrences o WHERE o.lexeme_id=l.id)"
        ):
            if sound_reason(row["lemma"]) is None:
                eligible.add(row["id"])
        return eligible
