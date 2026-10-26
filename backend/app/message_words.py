"""Persisted word occurrences anchored to completed assistant messages."""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from uuid import uuid4

from backend.app.knowledge import KnowledgeService
from backend.app.language import english_adapter
from backend.app.materials import MaterialError, MaterialLibrary
from backend.app.vocabulary import VocabularyPolicy, vocabulary_value


class MessageWords:
    def __init__(self, library: MaterialLibrary):
        self.library = library

    def index(
        self, db: sqlite3.Connection, conversation_id: str, message_id: str, text: str
    ) -> None:
        adapter = english_adapter()
        for word in adapter.tokenize(text):
            if not any("a" <= character.lower() <= "z" for character in word["surface"]):
                continue
            db.execute(
                "INSERT OR IGNORE INTO lexemes VALUES(?,?,?,?)",
                (
                    uuid4().hex,
                    adapter.language,
                    word["lemma"],
                    word["part_of_speech"],
                ),
            )
            lexeme_id = db.execute(
                "SELECT id FROM lexemes WHERE language=? AND lemma=? AND part_of_speech=?",
                (adapter.language, word["lemma"], word["part_of_speech"]),
            ).fetchone()[0]
            db.execute(
                "INSERT OR IGNORE INTO message_occurrences VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    uuid4().hex,
                    conversation_id,
                    message_id,
                    lexeme_id,
                    word["surface"],
                    word["start_offset"],
                    word["end_offset"],
                    int(word["uncertain"]),
                    adapter.language,
                    adapter.version,
                ),
            )

    def context(self, conversation_id: str, message_id: str) -> dict[str, Any]:
        with self.library.connect() as db:
            row = db.execute(
                "SELECT response_json,status FROM learning_messages "
                "WHERE conversation_id=? AND request_id=?",
                (conversation_id, message_id),
            ).fetchone()
        if row is None or row["status"] != "complete" or not row["response_json"]:
            raise KeyError("Завершённое сообщение не найдено в этом разговоре.")
        return {
            "source": "message",
            "conversation_id": conversation_id,
            "message_id": message_id,
            "text": json.loads(row["response_json"])["answer"],
        }

    def words(self, conversation_id: str, message_id: str) -> dict[str, Any]:
        result = self.context(conversation_id, message_id)
        with self.library.connect() as db:
            if not db.execute(
                "SELECT 1 FROM message_occurrences WHERE conversation_id=? AND message_id=?",
                (conversation_id, message_id),
            ).fetchone():
                db.execute("BEGIN IMMEDIATE")
                self.context(conversation_id, message_id)
                self.index(db, conversation_id, message_id, result["text"])
            VocabularyPolicy(self.library).ensure_message(db, conversation_id, message_id)
            tokens = []
            for row in db.execute(
                "SELECT o.*,l.lemma,l.part_of_speech,v.reason AS vocabulary_reason "
                "FROM message_occurrences o JOIN lexemes l ON l.id=o.lexeme_id "
                "LEFT JOIN message_vocabulary v ON v.token_id=o.id "
                "WHERE conversation_id=? AND message_id=? "
                "ORDER BY start_offset",
                (conversation_id, message_id),
            ):
                token = dict(row)
                token["uncertain"] = bool(token["uncertain"])
                token["knowledge"] = KnowledgeService(self.library).get(token["lexeme_id"], db=db)
                token["vocabulary"] = vocabulary_value(
                    token.pop("vocabulary_reason"), token["knowledge"]
                )
                tokens.append(token)
        result["tokens"] = tokens
        return result

    def selection(self, conversation_id: str, message_id: str, token_id: str) -> dict[str, Any]:
        context = self.words(conversation_id, message_id)
        token = next((word for word in context["tokens"] if word["id"] == token_id), None)
        if token is None:
            raise KeyError("Слово не относится к выбранному сообщению.")
        begin = max(0, token["start_offset"] - 350)
        return {
            **token,
            "source": "message",
            "token_id": token_id,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "text": context["text"][begin : begin + 1000],
        }

    def correct(
        self,
        conversation_id: str,
        message_id: str,
        token_id: str,
        lemma: str,
        part_of_speech: str,
    ) -> dict[str, Any]:
        from backend.app.activity import ActivityService

        selected = self.selection(conversation_id, message_id, token_id)
        lemma = lemma.strip().casefold()
        if not lemma or len(lemma) > 100 or any(character.isspace() for character in lemma):
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
            self.context(conversation_id, message_id)
            db.execute(
                "INSERT OR IGNORE INTO lexemes VALUES(?,?,?,?)",
                (
                    uuid4().hex,
                    selected["language"],
                    lemma,
                    part_of_speech,
                ),
            )
            identity = db.execute(
                "SELECT id FROM lexemes WHERE language=? AND lemma=? AND part_of_speech=?",
                (selected["language"], lemma, part_of_speech),
            ).fetchone()[0]
            db.execute(
                "UPDATE message_occurrences SET lexeme_id=?,uncertain=0 WHERE id=? "
                "AND conversation_id=? AND message_id=?",
                (identity, token_id, conversation_id, message_id),
            )
            if identity != selected["lexeme_id"] or selected["uncertain"]:
                db.execute("UPDATE learning_profile SET revision=revision+1 WHERE id=1")
                ActivityService(self.library).record(
                    db,
                    "lexeme_corrected",
                    lexeme_id=identity,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    message_token_id=token_id,
                    data={
                        "previous_word": {
                            key: selected[key] for key in ("lexeme_id", "lemma", "part_of_speech")
                        }
                    },
                )
        return self.selection(conversation_id, message_id, token_id)
