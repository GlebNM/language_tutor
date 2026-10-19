"""Bounded, read-only learning context shared by MCP and citation verification."""

from __future__ import annotations

import json
import re
from typing import Any

from backend.app.materials import MaterialLibrary

MAX_TOOL_CHARACTERS = 6000


def bounded_json(value: Any) -> str:
    result = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(result) > MAX_TOOL_CHARACTERS:
        raise ValueError("Учебный контекст превышает предел 6000 символов.")
    return result


class LearningContext:
    def __init__(self, library: MaterialLibrary):
        self.library = library

    def selection(self, material_id: str, cue_id: str, token_id: str) -> dict[str, Any]:
        with self.library.connect() as db:
            row = db.execute(
                "SELECT o.id token_id,o.material_id,o.cue_id,o.surface,o.lexeme_id,"
                "o.start_offset,o.end_offset,"
                "l.language,l.lemma,l.part_of_speech,c.text,c.start_ms,c.end_ms,"
                "m.youtube_video_id,m.transcript_hash,m.version "
                "FROM occurrences o JOIN lexemes l ON l.id=o.lexeme_id "
                "JOIN cues c ON c.id=o.cue_id JOIN materials m ON m.id=o.material_id "
                "WHERE o.material_id=? AND o.cue_id=? AND o.id=?",
                (material_id, cue_id, token_id),
            ).fetchone()
        if row is None:
            raise KeyError("Реплика и слово должны принадлежать выбранному материалу.")
        result = dict(row)
        begin = max(0, result["start_offset"] - 350)
        result["text"] = result["text"][begin : begin + 1000]
        return result

    def subtitle_context(self, material_id: str, cue_id: str, neighbors: int = 2) -> dict[str, Any]:
        if not 0 <= neighbors <= 2:
            raise ValueError("Допускается до двух соседних реплик с каждой стороны.")
        with self.library.connect() as db:
            selected = db.execute(
                "SELECT position FROM cues WHERE id=? AND material_id=?", (cue_id, material_id)
            ).fetchone()
            if selected is None:
                raise ValueError("Реплика не относится к выбранному материалу.")
            rows = db.execute(
                "SELECT c.id cue_id,c.material_id,c.start_ms,c.end_ms,c.text,"
                "m.youtube_video_id,m.version,m.title "
                "FROM cues c JOIN materials m ON m.id=c.material_id "
                "WHERE c.material_id=? AND c.position BETWEEN ? AND ? ORDER BY c.position",
                (material_id, selected["position"] - neighbors, selected["position"] + neighbors),
            ).fetchall()
        # Split the fixed character budget across cues, preserving identities and times.
        cues = [self.source(dict(row), text_limit=650) for row in rows]
        result = {"material_id": material_id, "selected_cue_id": cue_id, "cues": cues}
        bounded_json(result)
        return result

    @staticmethod
    def source(row: dict[str, Any], *, text_limit: int = 650) -> dict[str, Any]:
        text = row["text"]
        return {
            "citation_id": row["material_id"] + ":" + row["cue_id"],
            "material_id": row["material_id"],
            "cue_id": row["cue_id"],
            "text": text[:text_limit],
            "truncated": len(text) > text_limit,
            "start_ms": row["start_ms"],
            "end_ms": row["end_ms"],
            "youtube_video_id": row["youtube_video_id"],
            "version": row["version"],
            "title": row["title"][:80],
        }

    def find_occurrences(
        self,
        language: str,
        lemma: str,
        part_of_speech: str,
        limit: int = 5,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 5 or len(lemma) > 100 or len(part_of_speech) > 20:
            raise ValueError("Поиск принимает точную базовую форму и до пяти результатов.")
        with self.library.connect() as db:
            rows = db.execute(
                "SELECT DISTINCT c.id cue_id,c.material_id,c.start_ms,c.end_ms,c.text,"
                "m.youtube_video_id,m.version,m.title "
                "FROM lexemes l JOIN occurrences o ON o.lexeme_id=l.id "
                "JOIN cues c ON c.id=o.cue_id JOIN materials m ON m.id=c.material_id "
                "WHERE l.language=? AND l.lemma=? AND l.part_of_speech=? "
                "ORDER BY m.created_at DESC,c.start_ms LIMIT ?",
                (language, lemma, part_of_speech, limit),
            ).fetchall()
        result = {
            "language": language,
            "lemma": lemma,
            "part_of_speech": part_of_speech,
            "occurrences": [self.source(dict(row)) for row in rows],
            "notice": "Сохранённый материал не доказывает просмотр каждой реплики.",
        }
        bounded_json(result)
        return result

    def verify_sources(self, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        verified: list[dict[str, Any]] = []
        with self.library.connect() as db:
            for source in sources[:5]:
                row = db.execute(
                    "SELECT c.id cue_id,c.material_id,c.start_ms,c.end_ms,c.text,"
                    "m.youtube_video_id,m.version,m.title "
                    "FROM cues c JOIN materials m ON m.id=c.material_id "
                    "WHERE c.id=? AND c.material_id=?",
                    (source.get("cue_id"), source.get("material_id")),
                ).fetchone()
                if row is None:
                    continue
                trusted = self.source(dict(row))
                if any(source.get(key) != value for key, value in trusted.items()):
                    continue
                video_id = trusted["youtube_video_id"]
                if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                    continue
                trusted["youtube_url"] = (
                    "https://www.youtube.com/watch?v="
                    + video_id
                    + "&t="
                    + str(trusted["start_ms"] // 1000)
                )
                verified.append(trusted)
        return verified
