"""Read-only retrieval of a learner's real text materials and word occurrences."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any

from backend.app.language import english_adapter
from backend.app.learning_context import MAX_TOOL_CHARACTERS, bounded_json
from backend.app.materials import MaterialLibrary


class TextRetrieval:
    def __init__(self, library: MaterialLibrary):
        self.library = library

    def find_texts_with_word(
        self,
        word: str,
        part_of_speech: str | None = None,
        cursor: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        word = word.strip().casefold()
        if not word or len(word) > 100:
            raise ValueError("Укажите одно слово до 100 символов.")
        tokens = english_adapter().lemmatize(word, part_of_speech)
        if len(tokens) != 1 or tokens[0]["surface"].casefold() != word:
            raise ValueError("Укажите одно слово до 100 символов.")
        if not 1 <= limit <= 5:
            raise ValueError("Допускается от одного до пяти текстов.")
        lemmas = {word, tokens[0]["lemma"]}
        with self.library.connect() as db:
            # A word without sentence context may have several readings. Saved surface
            # occurrences recover the actual lemma for an explicitly requested POS.
            for row in db.execute(
                "SELECT DISTINCT l.lemma FROM occurrences o JOIN lexemes l ON l.id=o.lexeme_id "
                "WHERE lower(o.surface)=? AND (? IS NULL OR l.part_of_speech=?)",
                (word, part_of_speech, part_of_speech),
            ):
                lemmas.add(row["lemma"])
            placeholders = ",".join("?" for _ in lemmas)
            matching = db.execute(
                "SELECT o.cue_id block_id,o.id token_id,o.surface,o.start_offset,o.end_offset,"
                "l.lemma,l.part_of_speech FROM materials m "
                "JOIN occurrences o ON o.material_id=m.id JOIN lexemes l ON l.id=o.lexeme_id "
                f"WHERE m.kind='text' AND l.lemma IN ({placeholders}) "
                "AND (? IS NULL OR l.part_of_speech=?) "
                "ORDER BY o.start_offset",
                (*sorted(lemmas), part_of_speech, part_of_speech),
            ).fetchall()
            matches: dict[str, list[dict[str, Any]]] = {}
            for row in matching:
                matches.setdefault(row["block_id"], []).append({
                    name: row[name] for name in (
                        "token_id", "surface", "start_offset", "end_offset",
                        "lemma", "part_of_speech",
                    )
                })
            # Read each paragraph once, even when it contains thousands of matches.
            paragraphs = [dict(row) for row in db.execute(
                "SELECT m.id material_id,m.version,m.title,m.source,"
                "c.id block_id,c.position,c.text "
                "FROM materials m JOIN cues c ON c.material_id=m.id "
                "WHERE m.kind='text' ORDER BY m.created_at DESC,m.id,c.position"
            ) if row["block_id"] in matches]
        return self._page(
            paragraphs, matches, ["search", word, part_of_speech, limit], cursor, limit,
        )

    def get_text(self, material_id: str, cursor: str | None = None) -> dict[str, Any]:
        with self.library.connect() as db:
            material = db.execute(
                "SELECT id FROM materials WHERE id=? AND kind='text'", (material_id,),
            ).fetchone()
            if material is None:
                raise ValueError("Текст не найден.")
            rows = [dict(row) for row in db.execute(
                "SELECT m.id material_id,m.version,m.title,m.source,"
                "c.id block_id,c.position,c.text "
                "FROM materials m JOIN cues c ON c.material_id=m.id "
                "WHERE m.id=? ORDER BY c.position", (material_id,),
            )]
        page = self._page(rows, None, ["text", material_id], cursor, 1)
        return {
            **page["texts"][0], "next_cursor": page["next_cursor"], "complete": page["complete"],
        }

    def _page(
        self,
        rows: list[dict[str, Any]],
        matches: dict[str, list[dict[str, Any]]] | None,
        scope: list[Any],
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        generation = self.library.get_profile()["storage_generation"]
        identity = [generation, scope, [row["block_id"] for row in rows]]
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
        index, offset = 0, 0
        if cursor:
            try:
                if len(cursor) > 512:
                    raise ValueError
                decoded = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                index, offset = decoded["index"], decoded["offset"]
                if (
                    decoded["key"] != key or type(index) is not int or type(offset) is not int
                    or not 0 <= index < len(rows) or not 0 <= offset < len(rows[index]["text"])
                ):
                    raise ValueError
            except (ValueError, KeyError, TypeError, binascii.Error) as exc:
                raise ValueError(
                    "Страница устарела или относится к другому запросу. Начните заново."
                ) from exc

        def continuation(next_index: int, next_offset: int) -> str | None:
            if next_index == len(rows):
                return None
            return base64.urlsafe_b64encode(json.dumps({
                "key": key, "index": next_index, "offset": next_offset,
            }, separators=(",", ":")).encode()).decode()

        result: dict[str, Any] = {"texts": [], "next_cursor": None, "complete": True}
        while index < len(rows):
            row = rows[index]
            entries = result["texts"]
            if not entries or entries[-1]["material_id"] != row["material_id"]:
                if len(entries) == limit:
                    break
                entries.append({
                    **{name: row[name] for name in ("material_id", "title", "source", "version")},
                    "kind": "text", "paragraphs": [],
                })
            paragraphs = entries[-1]["paragraphs"]
            text = row["text"]
            block_matches = matches[row["block_id"]] if matches is not None else []

            def candidate(end: int) -> tuple[int, dict[str, Any]]:
                # Keep each actual matched token in one fragment, with its original offsets.
                for match in block_matches:
                    if match["start_offset"] < end < match["end_offset"]:
                        end = match["start_offset"]
                        break
                fragment: dict[str, Any] = {
                    "block_id": row["block_id"], "position": row["position"],
                    "text": text[offset:end], "start_offset": offset, "end_offset": end,
                    "total_characters": len(text),
                }
                if matches is not None:
                    fragment["matches"] = [
                        match for match in block_matches if offset <= match["start_offset"] < end
                    ]
                return end, fragment

            best: tuple[int, dict[str, Any]] | None = None
            low, high = offset + 1, min(len(text), offset + MAX_TOOL_CHARACTERS)
            while low <= high:
                middle = (low + high) // 2
                end, fragment = candidate(middle)
                paragraphs.append(fragment)
                next_index, next_offset = (index + 1, 0) if end == len(text) else (index, end)
                result["next_cursor"] = continuation(next_index, next_offset)
                result["complete"] = result["next_cursor"] is None
                try:
                    bounded_json(result)
                    fits = True
                except ValueError:
                    fits = False
                paragraphs.pop()
                if fits:
                    if end > offset:
                        best = end, fragment
                    low = middle + 1
                else:
                    high = middle - 1
            if best is None:
                if not paragraphs:
                    entries.pop()
                if not entries:
                    raise ValueError("Не удалось вместить фрагмент текста в страницу.")
                break
            end, fragment = best
            paragraphs.append(fragment)
            if end < len(text):
                offset = end
                break
            index, offset = index + 1, 0
        result["next_cursor"] = continuation(index, offset)
        result["complete"] = result["next_cursor"] is None
        return result
