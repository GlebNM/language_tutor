"""Read-only word practice context shared by the agent and the MCP server."""

from __future__ import annotations

import json
from typing import Any

from backend.app.knowledge import KnowledgeService
from backend.app.learning_context import LearningContext, bounded_json
from backend.app.materials import MaterialLibrary
from backend.app.vocabulary import VocabularyPolicy

STATUS_GUIDE = {
    "null": "Пока не оценено; ручной сброс не означает 'Не знаю'.",
    "0": "Не знаю: нужен понятный контекст и помощь.",
    "1": "Учу: обычно нужна подсказка.",
    "2": "Узнаю в контексте; это не подтверждает самостоятельное употребление.",
    "3": "Уверенно знаю; меньше внимания к целевому повторению.",
    "ignored": "Не выбирать для целевой практики; обычное употребление допустимо.",
}


class PracticeContext:
    def __init__(self, library: MaterialLibrary):
        self.library = library

    def snapshot(
        self,
        language: str,
        conversation_id: str | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        if language != self.library.get_profile()["target_language"]:
            raise ValueError("Этот язык не относится к учебному профилю.")
        if not 0 <= offset <= 100000:
            raise ValueError("Некорректное продолжение списка слов.")
        knowledge = KnowledgeService(self.library)
        with self.library.connect() as db:
            eligible = VocabularyPolicy(self.library).eligible_lexemes(db)
            if (
                conversation_id
                and not db.execute(
                    "SELECT 1 FROM conversations WHERE id=?", (conversation_id,)
                ).fetchone()
            ):
                raise ValueError("Разговор не найден.")
            rows = db.execute(
                "SELECT l.*,MAX(a.id) recent,MAX(CASE WHEN a.event_type='knowledge_changed' "
                "THEN a.id ELSE 0 END) changed FROM lexemes l "
                "LEFT JOIN word_knowledge k ON k.lexeme_id=l.id "
                "LEFT JOIN learning_activity a ON a.lexeme_id=l.id "
                "WHERE l.language=? AND COALESCE(k.ignored,0)=0 "
                "AND (k.score IN (0,1) OR a.id IS NOT NULL) "
                "GROUP BY l.id ORDER BY (k.score IN (0,1)) DESC,changed DESC,recent DESC,l.id",
                (language,),
            ).fetchall()
            rows = [row for row in rows if row["id"] in eligible][offset : offset + 9]
            words = []
            for row in rows[:8]:
                current = knowledge.get(row["id"], db=db)
                if current["ignored"]:
                    continue
                changes = db.execute(
                    "SELECT id,occurred_at,data_json FROM learning_activity "
                    "WHERE lexeme_id=? AND event_type='knowledge_changed' ORDER BY id DESC LIMIT 3",
                    (row["id"],),
                ).fetchall()
                words.append(
                    {
                        "lexeme_id": row["id"],
                        "lemma": row["lemma"],
                        "part_of_speech": row["part_of_speech"],
                        "knowledge": current,
                        "history": [
                            {
                                "id": change["id"],
                                "occurred_at": change["occurred_at"],
                                **json.loads(change["data_json"]),
                            }
                            for change in changes
                        ],
                    }
                )
                if len(words) == 8:
                    break
            material_ids = [
                row[0]
                for row in db.execute(
                    "SELECT material_id FROM learning_activity WHERE material_id IS NOT NULL "
                    "GROUP BY material_id ORDER BY MAX(id) DESC LIMIT 5"
                )
            ]
            if conversation_id:
                material_ids = list(
                    dict.fromkeys(
                        [
                            row[0]
                            for row in db.execute(
                                "SELECT material_id FROM conversation_materials "
                                "WHERE conversation_id=?",
                                (conversation_id,),
                            )
                        ]
                        + material_ids
                    )
                )[:20]
            materials = (
                [
                    dict(row)
                    for row in db.execute(
                        "SELECT id,title,is_complete,kind FROM materials WHERE id IN ("
                        + ",".join("?" for _ in material_ids)
                        + ")",
                        material_ids,
                    )
                ]
                if material_ids
                else []
            )
        for material in materials:
            material["title"] = material["title"][:80]
        from backend.app.expressions import ExpressionStore

        expressions = ExpressionStore(self.library).practice()
        result = {
            "language": language,
            "status_guide": STATUS_GUIDE,
            "words": words,
            "expressions": expressions,
            "materials": materials,
            "has_more": len(rows) > len(words),
            "notice": "История слов общая; тексты других бесед не передаются. "
            "Импорт и учебные действия не доказывают просмотр или знание.",
        }
        while True:
            result["next_offset"] = offset + len(words) if result["has_more"] else None
            try:
                bounded_json(result)
                return result
            except ValueError:
                if len(words) <= 1 and expressions:
                    expressions.pop()
                elif len(words) == 1 and materials:
                    materials.pop()
                elif words:
                    words.pop()
                    result["has_more"] = True
                elif materials:
                    materials.pop()
                elif expressions:
                    expressions.pop()
                else:
                    raise

    def material_context(self, material_id: str, query: str = "", limit: int = 5) -> dict[str, Any]:
        if not 1 <= limit <= 5 or len(query) > 200:
            raise ValueError("Запрашивайте до пяти реплик и короткий поисковый текст.")
        with self.library.connect() as db:
            material = db.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
            if material is None:
                raise ValueError("Материал не найден.")
            rows = db.execute(
                "SELECT c.id cue_id,c.material_id,c.start_ms,c.end_ms,c.text,"
                "m.youtube_video_id,m.version,m.title FROM cues c "
                "JOIN materials m ON m.id=c.material_id WHERE c.material_id=? "
                "AND instr(lower(c.text),lower(?))>0 ORDER BY c.position LIMIT ?",
                (material_id, query, limit),
            ).fetchall()
        result = {
            "material_id": material_id,
            "title": material["title"],
            "is_complete": bool(material["is_complete"]),
            "cues": [LearningContext.source(dict(row)) for row in rows],
            "notice": "Результат содержит выбранные фрагменты, а не весь текст видео.",
        }
        bounded_json(result)
        return result
