"""Persistent public material operations shared by the web API and later MCP tools."""

from __future__ import annotations

import fcntl
import hashlib
import html
import json
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from backend.app.language import english_adapter

MAX_FILE_BYTES = 2_000_000
MAX_CUES = 10_000
MAX_TEXT_CHARACTERS = 200_000


class MaterialError(ValueError):
    pass


def youtube_id(value: str) -> str:
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise MaterialError("Укажите корректную ссылку YouTube.") from exc
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise MaterialError("Укажите ссылку на видео YouTube.")
    host = parsed.hostname
    if host == "youtu.be":
        candidate = parsed.path.strip("/")
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parsed.path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [""])[0]
        elif re.fullmatch(r"/(shorts|live|embed)/[A-Za-z0-9_-]{11}/?", parsed.path):
            candidate = parsed.path.strip("/").split("/")[1]
        else:
            raise MaterialError("Вставьте ссылку YouTube на отдельное видео.")
    else:
        raise MaterialError("Поддерживаются обычные ссылки youtube.com/watch и youtu.be.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
        raise MaterialError("В ссылке отсутствует корректный ID видео YouTube.")
    return candidate


def timestamp(value: str) -> int:
    match = re.fullmatch(r"(?:(\d{2,}):)?(\d{2}):(\d{2})[,.](\d{3})", value)
    if not match:
        raise MaterialError(f"Неверная временная метка: {value}.")
    hours, minutes, seconds, milliseconds = match.groups()
    if int(minutes) > 59 or int(seconds) > 59:
        raise MaterialError("Минуты и секунды должны быть меньше 60.")
    return ((int(hours or 0) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(milliseconds)


def parse_track(content: str, track_format: str) -> list[dict[str, Any]]:
    try:
        file_size = len(content.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise MaterialError("Не удалось прочитать UTF-8. Проверьте кодировку файла.") from exc
    if file_size > MAX_FILE_BYTES:
        raise MaterialError("Файл должен быть не больше 2 МБ.")
    if "\x00" in content:
        raise MaterialError("Файл содержит недопустимый нулевой символ. Используйте UTF-8.")
    content = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if track_format == "vtt":
        if not re.match(r"^WEBVTT(?:[ \t].*)?(?:\n|$)", content):
            raise MaterialError("VTT должен начинаться с WEBVTT.")
        # WebVTT permits metadata after the signature, before the first blank line.
        content = (
            re.split(r"\n[ \t]*\n", content, maxsplit=1)[1]
            if re.search(r"\n[ \t]*\n", content)
            else ""
        )
    elif track_format != "srt":
        raise MaterialError("Поддерживаются только SRT и VTT.")
    cues: list[dict[str, Any]] = []
    total = 0
    previous_start = -1
    count = 0
    for block in re.split(r"\n[ \t]*\n", content.strip()):
        lines = block.splitlines()
        if not lines:
            continue
        if track_format == "vtt" and re.match(r"^(NOTE(?:\s|$)|STYLE$|REGION$)", lines[0]):
            continue
        timing_index = 0 if "-->" in lines[0] else 1
        if timing_index >= len(lines):
            raise MaterialError("В каждой реплике нужны время начала, конца и текст.")
        timing = re.fullmatch(r"(\S+)\s+-->\s+(\S+)(?:[ \t]+.*)?", lines[timing_index])
        if not timing:
            raise MaterialError("Не удалось прочитать время реплики. Проверьте формат SRT/VTT.")
        start, end = timestamp(timing[1]), timestamp(timing[2])
        if start < previous_start or end <= start:
            raise MaterialError("Реплики должны идти по времени; конец должен быть позже начала.")
        previous_start = start
        original = "\n".join(lines[timing_index + 1 :])
        if "-->" in original:
            raise MaterialError("Между репликами нужна пустая строка.")
        total += len(original)
        count += 1
        if count > MAX_CUES:
            raise MaterialError("Допускается не больше 10 000 реплик.")
        if total > MAX_TEXT_CHARACTERS:
            raise MaterialError("Текст субтитров должен содержать не больше 200 000 символов.")
        text = " ".join(
            html.unescape(
                re.sub(
                    r"<[^>]*>",
                    "",
                    re.sub(r"<br\s*/?>", " ", original, flags=re.IGNORECASE),
                )
            ).split()
        )
        if not text:
            raise MaterialError("Реплика не должна быть пустой.")
        # Only temporally overlapping repetitions are merged. Later speech stays separate.
        if cues and start < cues[-1]["end_ms"]:
            previous_words, current_words = cues[-1]["text"].split(), text.split()
            overlap = 0
            for length in range(min(len(previous_words), len(current_words)), 0, -1):
                if previous_words[-length:] == current_words[:length]:
                    overlap = length
                    break
            # A whole repeated cue or a multiword rolling prefix can safely coalesce.
            if overlap and (overlap >= 2 or text == cues[-1]["text"]):
                cues[-1]["text"] = " ".join(previous_words + current_words[overlap:])
                cues[-1]["end_ms"] = max(end, cues[-1]["end_ms"])
                cues[-1]["original_fragments"].append(original)
                continue
        cues.append(
            {
                "start_ms": start,
                "end_ms": end,
                "text": text,
                "original_text": original,
                "original_fragments": [original],
            }
        )
    if not cues:
        raise MaterialError("Дорожка пустая. Добавьте хотя бы одну реплику.")
    return cues


SCHEMA = """
CREATE TABLE learning_profile(
    id INTEGER PRIMARY KEY CHECK(id=1),
    target_language TEXT NOT NULL,
    explanation_language TEXT NOT NULL,
    diagnostic_version INTEGER NOT NULL DEFAULT 0
);
INSERT INTO learning_profile VALUES(1, 'en', 'ru', 0);
CREATE TABLE materials(
    id TEXT PRIMARY KEY,
    youtube_video_id TEXT NOT NULL,
    title TEXT NOT NULL,
    language TEXT NOT NULL,
    source TEXT NOT NULL,
    is_complete INTEGER NOT NULL,
    transcript_hash TEXT NOT NULL,
    version INTEGER NOT NULL,
    previous_version_id TEXT REFERENCES materials(id),
    adapter_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(youtube_video_id, language, transcript_hash)
);
CREATE TABLE cues(
    id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id),
    position INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    text TEXT NOT NULL,
    original_text TEXT NOT NULL,
    original_fragments TEXT NOT NULL
);
CREATE INDEX cues_material ON cues(material_id, position);
CREATE TABLE lexemes(
    id TEXT PRIMARY KEY,
    language TEXT NOT NULL,
    lemma TEXT NOT NULL,
    part_of_speech TEXT NOT NULL,
    UNIQUE(language,lemma,part_of_speech)
);
CREATE TABLE occurrences(
    id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id),
    cue_id TEXT NOT NULL REFERENCES cues(id),
    lexeme_id TEXT NOT NULL REFERENCES lexemes(id),
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    surface TEXT NOT NULL,
    uncertain INTEGER NOT NULL
);
CREATE INDEX occurrences_material ON occurrences(material_id,cue_id,start_offset);
CREATE INDEX occurrences_lexeme ON occurrences(lexeme_id,material_id);
"""


# Append migrations here so every schema change has one ordered, backed-up path.
MIGRATIONS = [
    SCHEMA,
    """
    ALTER TABLE learning_profile ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE storage_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    INSERT INTO storage_metadata VALUES('generation', lower(hex(randomblob(16))));
    INSERT INTO storage_metadata VALUES('seeded_demo', '0');
    CREATE TABLE word_knowledge(
        lexeme_id TEXT PRIMARY KEY REFERENCES lexemes(id),
        score INTEGER CHECK(score IS NULL OR score BETWEEN 0 AND 3),
        ignored INTEGER NOT NULL DEFAULT 0 CHECK(ignored IN (0,1)),
        source TEXT NOT NULL CHECK(source IN ('manual','diagnostic')),
        assessment_version INTEGER,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE diagnostic_rules(
        language TEXT NOT NULL,
        frequency_group INTEGER NOT NULL,
        version INTEGER NOT NULL,
        enabled INTEGER NOT NULL,
        PRIMARY KEY(language,frequency_group)
    );
    """,
    """
    CREATE TABLE contextual_cards(cache_key TEXT PRIMARY KEY,result_json TEXT NOT NULL,
        created_at TEXT NOT NULL);
    CREATE TABLE conversations(id TEXT PRIMARY KEY,
        material_id TEXT NOT NULL REFERENCES materials(id),
        cue_id TEXT NOT NULL REFERENCES cues(id),token_id TEXT NOT NULL REFERENCES occurrences(id),
        created_at TEXT NOT NULL);
    CREATE INDEX conversations_material ON conversations(material_id,created_at);
    CREATE TABLE learning_messages(conversation_id TEXT NOT NULL REFERENCES conversations(id),
        request_id TEXT NOT NULL,user_text TEXT NOT NULL,
        response_json TEXT,created_at TEXT NOT NULL,
        PRIMARY KEY(conversation_id,request_id));
    CREATE INDEX messages_conversation ON learning_messages(conversation_id,created_at);
    """,
    """
    CREATE TABLE diagnostic_sessions(
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL CHECK(status IN ('in_progress','completed','skipped')),
        pool_version INTEGER NOT NULL,
        generation TEXT NOT NULL,
        items_json TEXT NOT NULL,
        answers_json TEXT NOT NULL,
        summary_json TEXT,
        created_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE learning_activity(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL CHECK(event_type IN (
            'word_selected','translation_received','knowledge_changed',
            'lexeme_corrected','message_sent')),
        schema_version INTEGER NOT NULL DEFAULT 1,
        occurred_at TEXT NOT NULL,
        language TEXT NOT NULL,
        material_id TEXT REFERENCES materials(id),
        cue_id TEXT REFERENCES cues(id),
        token_id TEXT REFERENCES occurrences(id),
        lexeme_id TEXT NOT NULL REFERENCES lexemes(id),
        request_id TEXT,
        deduplication_key TEXT UNIQUE,
        word_json TEXT NOT NULL,
        data_json TEXT NOT NULL
    );
    CREATE INDEX activity_recent ON learning_activity(language,id DESC);
    CREATE INDEX activity_type ON learning_activity(language,event_type,id DESC);
    CREATE INDEX activity_material ON learning_activity(material_id,id DESC);
    CREATE INDEX activity_lexeme ON learning_activity(lexeme_id,id DESC);
    CREATE INDEX activity_time ON learning_activity(language,occurred_at,id DESC);
    """,
    """
    CREATE TABLE conversations_next(id TEXT PRIMARY KEY,
        material_id TEXT REFERENCES materials(id),
        cue_id TEXT REFERENCES cues(id),token_id TEXT REFERENCES occurrences(id),
        created_at TEXT NOT NULL);
    INSERT INTO conversations_next SELECT * FROM conversations;
    CREATE TABLE learning_messages_next(
        conversation_id TEXT NOT NULL REFERENCES conversations_next(id),
        request_id TEXT NOT NULL,user_text TEXT NOT NULL,
        response_json TEXT,created_at TEXT NOT NULL,
        PRIMARY KEY(conversation_id,request_id));
    INSERT INTO learning_messages_next SELECT * FROM learning_messages;
    DROP TABLE learning_messages;
    DROP TABLE conversations;
    ALTER TABLE conversations_next RENAME TO conversations;
    ALTER TABLE learning_messages_next RENAME TO learning_messages;
    CREATE INDEX conversations_material ON conversations(material_id,created_at);
    CREATE INDEX messages_conversation ON learning_messages(conversation_id,created_at);
    """,
    """
    ALTER TABLE conversations ADD COLUMN title TEXT NOT NULL DEFAULT 'Новый разговор';
    CREATE TABLE conversation_materials(
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        material_id TEXT NOT NULL REFERENCES materials(id),
        PRIMARY KEY(conversation_id,material_id));
    INSERT INTO conversation_materials SELECT id,material_id FROM conversations
        WHERE material_id IS NOT NULL;
    """,
    """
    CREATE TABLE learning_activity_next(
        id INTEGER PRIMARY KEY AUTOINCREMENT,event_type TEXT NOT NULL CHECK(event_type IN (
            'word_selected','translation_received','knowledge_changed',
            'lexeme_corrected','message_sent')),
        schema_version INTEGER NOT NULL DEFAULT 1,occurred_at TEXT NOT NULL,
        language TEXT NOT NULL,material_id TEXT REFERENCES materials(id),
        cue_id TEXT REFERENCES cues(id),token_id TEXT REFERENCES occurrences(id),
        lexeme_id TEXT REFERENCES lexemes(id),request_id TEXT,deduplication_key TEXT UNIQUE,
        word_json TEXT NOT NULL,data_json TEXT NOT NULL);
    INSERT INTO learning_activity_next SELECT * FROM learning_activity;
    DROP TABLE learning_activity;
    ALTER TABLE learning_activity_next RENAME TO learning_activity;
    CREATE INDEX activity_recent ON learning_activity(language,id DESC);
    CREATE INDEX activity_type ON learning_activity(language,event_type,id DESC);
    CREATE INDEX activity_material ON learning_activity(material_id,id DESC);
    CREATE INDEX activity_lexeme ON learning_activity(lexeme_id,id DESC);
    CREATE INDEX activity_time ON learning_activity(language,occurred_at,id DESC);
    """,
    """
    ALTER TABLE conversations ADD COLUMN summary TEXT NOT NULL DEFAULT '';
    ALTER TABLE conversations ADD COLUMN summary_count INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE learning_messages ADD COLUMN history_json TEXT;
    """,
    """
    ALTER TABLE learning_messages ADD COLUMN status TEXT NOT NULL DEFAULT 'pending';
    UPDATE learning_messages SET status=CASE WHEN response_json IS NULL THEN 'failed'
        ELSE 'complete' END;
    """,
    """
    CREATE TABLE message_occurrences(
        id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,message_id TEXT NOT NULL,
        lexeme_id TEXT NOT NULL REFERENCES lexemes(id),surface TEXT NOT NULL,
        start_offset INTEGER NOT NULL,end_offset INTEGER NOT NULL,uncertain INTEGER NOT NULL,
        language TEXT NOT NULL,adapter_version TEXT NOT NULL,
        FOREIGN KEY(conversation_id,message_id)
            REFERENCES learning_messages(conversation_id,request_id) ON DELETE CASCADE,
        UNIQUE(conversation_id,message_id,start_offset));
    CREATE INDEX message_words_context ON message_occurrences(conversation_id,message_id);
    ALTER TABLE learning_activity ADD COLUMN conversation_id TEXT;
    ALTER TABLE learning_activity ADD COLUMN message_id TEXT;
    ALTER TABLE learning_activity ADD COLUMN message_token_id TEXT;
    UPDATE learning_activity SET conversation_id=json_extract(data_json,'$.conversation_id')
        WHERE event_type='message_sent';
    CREATE INDEX activity_conversation ON learning_activity(conversation_id,id DESC);
    """,
    """
    CREATE TABLE learning_settings(id INTEGER PRIMARY KEY CHECK(id=1),
        enabled INTEGER NOT NULL DEFAULT 0,epoch INTEGER NOT NULL DEFAULT 0);
    INSERT INTO learning_settings VALUES(1,0,0);
    CREATE TABLE viewings(id TEXT PRIMARY KEY,
        material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
        epoch INTEGER NOT NULL,created_at TEXT NOT NULL,result_json TEXT);
    CREATE UNIQUE INDEX viewing_pending ON viewings(material_id) WHERE result_json IS NULL;
    CREATE TABLE viewing_cues(viewing_id TEXT NOT NULL REFERENCES viewings(id) ON DELETE CASCADE,
        cue_id TEXT NOT NULL REFERENCES cues(id),PRIMARY KEY(viewing_id,cue_id));
    CREATE TABLE viewing_clicks(viewing_id TEXT NOT NULL REFERENCES viewings(id) ON DELETE CASCADE,
        token_id TEXT NOT NULL REFERENCES occurrences(id),PRIMARY KEY(viewing_id,token_id));
    CREATE TABLE viewing_choices(viewing_id TEXT NOT NULL REFERENCES viewings(id) ON DELETE CASCADE,
        lexeme_id TEXT NOT NULL REFERENCES lexemes(id),
        score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 3),
        included INTEGER NOT NULL CHECK(included IN (0,1)),PRIMARY KEY(viewing_id,lexeme_id));
    CREATE TABLE learning_activity_viewing(
        id INTEGER PRIMARY KEY AUTOINCREMENT,event_type TEXT NOT NULL CHECK(event_type IN (
            'word_selected','translation_received','knowledge_changed',
            'lexeme_corrected','message_sent','cue_listened','viewing_confirmed')),
        schema_version INTEGER NOT NULL DEFAULT 1,occurred_at TEXT NOT NULL,
        language TEXT NOT NULL,material_id TEXT REFERENCES materials(id),
        cue_id TEXT REFERENCES cues(id),token_id TEXT REFERENCES occurrences(id),
        lexeme_id TEXT REFERENCES lexemes(id),request_id TEXT,deduplication_key TEXT UNIQUE,
        word_json TEXT NOT NULL,data_json TEXT NOT NULL,
        conversation_id TEXT,message_id TEXT,message_token_id TEXT);
    INSERT INTO learning_activity_viewing SELECT * FROM learning_activity;
    DROP TABLE learning_activity;
    ALTER TABLE learning_activity_viewing RENAME TO learning_activity;
    CREATE INDEX activity_recent ON learning_activity(language,id DESC);
    CREATE INDEX activity_type ON learning_activity(language,event_type,id DESC);
    CREATE INDEX activity_material ON learning_activity(material_id,id DESC);
    CREATE INDEX activity_lexeme ON learning_activity(lexeme_id,id DESC);
    CREATE INDEX activity_time ON learning_activity(language,occurred_at,id DESC);
    CREATE INDEX activity_conversation ON learning_activity(conversation_id,id DESC);
    """,
    """
    CREATE TABLE occurrence_vocabulary(
        token_id TEXT PRIMARY KEY REFERENCES occurrences(id) ON DELETE CASCADE,
        lexeme_id TEXT NOT NULL REFERENCES lexemes(id),reason TEXT,version TEXT NOT NULL);
    CREATE TABLE message_vocabulary(
        token_id TEXT PRIMARY KEY REFERENCES message_occurrences(id) ON DELETE CASCADE,
        lexeme_id TEXT NOT NULL REFERENCES lexemes(id),reason TEXT,version TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS message_words_lexeme ON message_occurrences(lexeme_id);
    """,
    """
    CREATE TABLE subtitle_improvements(
        source_id TEXT PRIMARY KEY REFERENCES materials(id) ON DELETE CASCADE,
        id TEXT NOT NULL UNIQUE,status TEXT NOT NULL,
        completed_batches INTEGER NOT NULL DEFAULT 0,total_batches INTEGER NOT NULL,
        result_id TEXT REFERENCES materials(id) ON DELETE SET NULL,
        changed_cues INTEGER NOT NULL DEFAULT 0,error TEXT,
        model TEXT NOT NULL,reasoning_effort TEXT,prompt_version TEXT NOT NULL,
        created_at TEXT NOT NULL);
    """,
    """
    CREATE TABLE progress_vocabulary(
        id INTEGER PRIMARY KEY AUTOINCREMENT,occurred_at TEXT NOT NULL,
        reason TEXT NOT NULL,counts_json TEXT NOT NULL);
    CREATE TABLE playback_intervals(
        id TEXT PRIMARY KEY,material_id TEXT NOT NULL REFERENCES materials(id),
        started_at TEXT NOT NULL,ended_at TEXT NOT NULL);
    CREATE INDEX playback_time ON playback_intervals(started_at,ended_at);
    INSERT INTO storage_metadata VALUES('playback_tracking_since',
        strftime('%Y-%m-%dT%H:%M:%f+00:00','now'));
    """,
    """
    CREATE TABLE expressions(
        id TEXT PRIMARY KEY,language TEXT NOT NULL,canonical TEXT NOT NULL,kind TEXT NOT NULL,
        sense_key TEXT NOT NULL,translation TEXT NOT NULL,explanation TEXT NOT NULL,
        example TEXT NOT NULL,usage TEXT NOT NULL,created_at TEXT NOT NULL,
        UNIQUE(language,canonical,sense_key));
    CREATE TABLE expression_knowledge(
        expression_id TEXT PRIMARY KEY REFERENCES expressions(id) ON DELETE CASCADE,
        score INTEGER CHECK(score BETWEEN 0 AND 3),ignored INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL);
    CREATE TABLE expression_occurrences(
        id TEXT PRIMARY KEY,
        expression_id TEXT NOT NULL REFERENCES expressions(id) ON DELETE CASCADE,
        source_key TEXT NOT NULL,material_id TEXT REFERENCES materials(id) ON DELETE CASCADE,
        conversation_id TEXT,message_id TEXT,token_ids_json TEXT NOT NULL,
        context_json TEXT NOT NULL,card_json TEXT NOT NULL,origin TEXT NOT NULL,
        dismissed INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,
        FOREIGN KEY(conversation_id,message_id)
            REFERENCES learning_messages(conversation_id,request_id) ON DELETE CASCADE,
        UNIQUE(source_key,token_ids_json));
    CREATE INDEX expression_sources ON expression_occurrences(source_key,dismissed);
    CREATE INDEX expression_examples ON expression_occurrences(expression_id);
    CREATE TABLE expression_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        expression_id TEXT NOT NULL REFERENCES expressions(id) ON DELETE CASCADE,
        occurrence_id TEXT REFERENCES expression_occurrences(id) ON DELETE SET NULL,
        event_type TEXT NOT NULL,occurred_at TEXT NOT NULL,data_json TEXT NOT NULL);
    CREATE TABLE expression_jobs(
        source_key TEXT PRIMARY KEY,id TEXT NOT NULL UNIQUE,status TEXT NOT NULL,
        completed_batches INTEGER NOT NULL DEFAULT 0,total_batches INTEGER NOT NULL,
        model TEXT NOT NULL,reasoning_effort TEXT,prompt_version TEXT NOT NULL,
        error TEXT,created_at TEXT NOT NULL);
    """,
    """
    CREATE TABLE materials_next(
        id TEXT PRIMARY KEY,youtube_video_id TEXT,title TEXT NOT NULL,
        language TEXT NOT NULL,source TEXT NOT NULL,is_complete INTEGER NOT NULL,
        transcript_hash TEXT NOT NULL,version INTEGER NOT NULL,
        previous_version_id TEXT REFERENCES materials(id),adapter_version TEXT NOT NULL,
        created_at TEXT NOT NULL,kind TEXT NOT NULL DEFAULT 'video'
            CHECK(kind IN ('video','text')),
        CHECK((kind='video' AND youtube_video_id IS NOT NULL)
            OR (kind='text' AND youtube_video_id IS NULL)),
        UNIQUE(youtube_video_id,language,transcript_hash));
    INSERT INTO materials_next SELECT *, 'video' FROM materials;
    CREATE TABLE cues_next(
        id TEXT PRIMARY KEY,material_id TEXT NOT NULL REFERENCES materials(id),
        position INTEGER NOT NULL,start_ms INTEGER,end_ms INTEGER,text TEXT NOT NULL,
        original_text TEXT NOT NULL,original_fragments TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'subtitle' CHECK(kind IN ('subtitle','paragraph')),
        CHECK((kind='subtitle' AND start_ms IS NOT NULL AND end_ms IS NOT NULL AND end_ms>start_ms)
            OR (kind='paragraph' AND start_ms IS NULL AND end_ms IS NULL)));
    INSERT INTO cues_next SELECT *, 'subtitle' FROM cues;
    DROP TABLE cues;
    DROP TABLE materials;
    ALTER TABLE materials_next RENAME TO materials;
    ALTER TABLE cues_next RENAME TO cues;
    CREATE INDEX cues_material ON cues(material_id,position);
    CREATE TABLE reading_positions(
        material_id TEXT PRIMARY KEY REFERENCES materials(id) ON DELETE CASCADE,
        cue_id TEXT NOT NULL REFERENCES cues(id) ON DELETE CASCADE,
        offset INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);
    """,
]


class MaterialLibrary:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite serializes migrations; the file lock also serializes the initial WAL switch.
        with self.database_path.with_suffix(self.database_path.suffix + ".lock").open(
            "a+b"
        ) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.connect() as connection:
                # Serialize initializers from API and MCP processes before checking the version.
                # Parent-table rebuilds preserve foreign-key identities only while FK
                # enforcement is disabled before the transaction. Check all links before commit.
                connection.execute("PRAGMA foreign_keys=OFF")
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version > len(MIGRATIONS):
                    raise RuntimeError("Версия базы новее приложения. Обновите приложение.")
                if (
                    version < len(MIGRATIONS)
                    and connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchone()
                ):
                    backup_path = self.database_path.with_name(
                        self.database_path.name
                        + f".before-v{len(MIGRATIONS)}-"
                        + uuid.uuid4().hex
                        + ".bak"
                    )
                    # A second reader sees committed WAL data while this writer reserves the
                    # migration lock. Backing up the writer itself would wait on its lock.
                    with sqlite3.connect(self.database_path) as source:
                        with sqlite3.connect(backup_path) as backup:
                            source.backup(backup)
                for target, script in enumerate(MIGRATIONS, start=1):
                    if version >= target:
                        continue
                    for statement in script.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(f"PRAGMA user_version={target}")
                if connection.execute("PRAGMA foreign_key_check").fetchone():
                    raise RuntimeError("Миграция нарушила связи учебных данных.")
                connection.commit()
                connection.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get_profile(self) -> dict[str, Any]:
        with self.connect() as db:
            result = dict(db.execute("SELECT * FROM learning_profile WHERE id=1").fetchone())
            result["diagnostic_groups"] = [
                {
                    "group": row["frequency_group"],
                    "version": row["version"],
                    "inferred": bool(row["enabled"]),
                }
                for row in db.execute("SELECT * FROM diagnostic_rules ORDER BY frequency_group")
            ]
            summary = db.execute(
                "SELECT summary_json FROM diagnostic_sessions WHERE status='completed' "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            result["diagnostic_summary"] = (
                json.loads(summary[0])["description"] if summary else None
            )
            result["storage_generation"] = db.execute(
                "SELECT value FROM storage_metadata WHERE key='generation'"
            ).fetchone()[0]
            return result

    def reset_learning_data(self) -> dict[str, Any]:
        """Clear the single learner atomically and rotate the late-response guard."""
        with self.database_path.with_suffix(self.database_path.suffix + ".lock").open(
            "a+b"
        ) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.connect() as db:
                db.execute("PRAGMA secure_delete=ON")
                db.execute("BEGIN IMMEDIATE")
                # Children precede parents; optional modules may not have any rows yet.
                tables = {
                    row[0]
                    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                for table in (
                    "expression_jobs",
                    "expression_events",
                    "expression_occurrences",
                    "expression_knowledge",
                    "expressions",
                    "playback_intervals",
                    "progress_vocabulary",
                    "subtitle_improvements",
                    "viewings",
                    "learning_activity",
                    "message_occurrences",
                    "conversation_materials",
                    "learning_messages",
                    "conversations",
                    "contextual_cards",
                    "diagnostic_answers",
                    "diagnostic_sessions",
                    "diagnostic_rules",
                    "word_knowledge",
                    "occurrences",
                    "reading_positions",
                    "cues",
                    "materials",
                    "lexemes",
                ):
                    if table in tables:
                        db.execute(f"DELETE FROM {table}")
                db.execute(
                    "UPDATE learning_profile SET diagnostic_version=diagnostic_version+1,"
                    "revision=revision+1 WHERE id=1"
                )
                db.execute(
                    "UPDATE storage_metadata SET value=? WHERE key='generation'",
                    (uuid.uuid4().hex,),
                )
                db.execute(
                    "UPDATE storage_metadata SET value=? WHERE key='playback_tracking_since'",
                    (datetime.now(UTC).isoformat(),),
                )
                from backend.app.progress import ProgressService

                ProgressService(self).capture(db)
                db.execute("UPDATE learning_settings SET enabled=0,epoch=epoch+1 WHERE id=1")
                db.execute("UPDATE storage_metadata SET value='1' WHERE key='seeded_demo'")
            backup_pattern = re.compile(
                re.escape(self.database_path.name)
                + r"\.before-v[0-9]+-[0-9a-f]{32}\.bak(?:-wal|-shm)?"
            )
            for backup_path in self.database_path.parent.iterdir():
                if backup_pattern.fullmatch(backup_path.name):
                    try:
                        backup_path.unlink(missing_ok=True)
                    except OSError as exc:
                        raise MaterialError(
                            "Учебные данные удалены, но резервную копию удалить не удалось. "
                            "Проверьте права на каталог данных и повторите удаление."
                        ) from exc
            # Release old WAL pages where possible; an active reader can defer checkpointing.
            with self.connect() as db:
                try:
                    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    db.execute("VACUUM")
                except sqlite3.OperationalError:
                    pass
            return self.get_profile()

    def list_materials(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                self._metadata(row)
                for row in db.execute(
                    "SELECT * FROM materials ORDER BY created_at DESC,version DESC"
                )
            ]

    @staticmethod
    def _metadata(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["is_complete"] = bool(result["is_complete"])
        result["youtube_url"] = (
            "https://www.youtube.com/watch?v=" + result["youtube_video_id"]
            if result["youtube_video_id"]
            else None
        )
        return result

    def get_material(self, material_id: str) -> dict[str, Any]:
        from backend.app.vocabulary import VocabularyPolicy, vocabulary_value

        with self.connect() as db:
            row = db.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
            if row is None:
                raise KeyError("Материал не найден.")
            VocabularyPolicy(self).ensure_material(db, material_id)
            result = self._metadata(row)
            result["cues"] = []
            tokens_by_cue: dict[str, list[dict[str, Any]]] = {}
            for token in db.execute(
                "SELECT o.*,l.language,l.lemma,l.part_of_speech,v.reason AS vocabulary_reason "
                "FROM occurrences o JOIN lexemes l ON l.id=o.lexeme_id "
                "LEFT JOIN occurrence_vocabulary v ON v.token_id=o.id "
                "WHERE o.material_id=? ORDER BY o.start_offset",
                (material_id,),
            ):
                occurrence = dict(token)
                occurrence["uncertain"] = bool(occurrence["uncertain"])
                occurrence["block_id"] = occurrence["cue_id"]
                from backend.app.knowledge import KnowledgeService

                occurrence["knowledge"] = KnowledgeService(self).get(occurrence["lexeme_id"], db=db)
                occurrence["vocabulary"] = vocabulary_value(
                    occurrence.pop("vocabulary_reason"), occurrence["knowledge"]
                )
                tokens_by_cue.setdefault(occurrence["cue_id"], []).append(occurrence)
            for cue in db.execute(
                "SELECT * FROM cues WHERE material_id=? ORDER BY position", (material_id,)
            ):
                item = dict(cue)
                item["original_fragments"] = json.loads(item["original_fragments"])
                item["tokens"] = tokens_by_cue.get(item["id"], [])
                result["cues"].append(item)
            from backend.app.knowledge import calculate_difficulty

            result["blocks"] = result["cues"]
            result["difficulty"] = calculate_difficulty(result)
            return result

    def selection(self, material_id: str, cue_id: str, token_id: str) -> dict[str, Any]:
        material = self.get_material(material_id)
        for cue in material["cues"]:
            if cue["id"] == cue_id:
                for token in cue["tokens"]:
                    if token["id"] == token_id:
                        return {
                            "material_id": material_id,
                            "youtube_video_id": material["youtube_video_id"],
                            "kind": material["kind"],
                            "cue": cue,
                            "block": cue,
                            "token": token,
                            "youtube_url": (
                                material["youtube_url"] + "&t=" + str(cue["start_ms"] // 1000)
                                if material["youtube_url"]
                                else None
                            ),
                        }
        raise KeyError("Реплика и слово должны принадлежать выбранному материалу.")

    def import_text(
        self,
        *,
        title: str,
        content: str,
        source: str = "manual_text",
        language: str = "en",
        replaces: str | None = None,
        expected_generation: str | None = None,
        expected_profile_revision: int | None = None,
    ) -> dict[str, Any]:
        """Save paragraph text as an immutable material without inventing video metadata."""
        if language != "en" or source not in {"manual_text", "text_file", "generated"}:
            raise MaterialError("Проверьте язык и источник текста.")
        if not title.strip() or len(title) > 200:
            raise MaterialError("Укажите название текста до 200 символов.")
        try:
            size = len(content.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise MaterialError("Текст должен быть в кодировке UTF-8.") from exc
        if size > MAX_FILE_BYTES or len(content) > MAX_TEXT_CHARACTERS:
            raise MaterialError("Текст должен содержать не больше 200 000 символов и 2 МБ.")
        if "\x00" in content:
            raise MaterialError("Текст содержит недопустимый нулевой символ.")
        normalized = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
        paragraphs = [part.strip() for part in re.split(r"\n[ \t]*\n", normalized) if part.strip()]
        if not paragraphs or len(paragraphs) > MAX_CUES:
            raise MaterialError("Добавьте непустой текст до 10 000 абзацев.")
        digest = hashlib.sha256(
            json.dumps(
                {"title": title.strip(), "paragraphs": paragraphs, "language": language},
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            profile = db.execute("SELECT revision FROM learning_profile WHERE id=1").fetchone()
            generation = db.execute(
                "SELECT value FROM storage_metadata WHERE key='generation'"
            ).fetchone()[0]
            if expected_generation is not None and expected_generation != generation:
                raise MaterialError("Данные были удалены. Создайте текст заново.")
            if expected_profile_revision is not None and expected_profile_revision != profile[0]:
                raise MaterialError("Словарь изменился. Проверьте сложность текста заново.")
            previous = None
            if replaces:
                previous = db.execute("SELECT * FROM materials WHERE id=?", (replaces,)).fetchone()
                if previous is None:
                    raise KeyError("Исходный текст не найден.")
                if previous["kind"] != "text":
                    raise MaterialError("Заменять текстом можно только текстовый материал.")
            if source != "generated":
                existing = db.execute(
                    "SELECT id FROM materials WHERE kind='text' AND language=? "
                    "AND source=? AND transcript_hash=? AND "
                    "(previous_version_id=? OR (previous_version_id IS NULL AND ? IS NULL))",
                    (language, source, digest, replaces, replaces),
                ).fetchone()
                if existing:
                    db.commit()
                    return self.get_material(existing["id"])
            material_id = uuid.uuid4().hex
            adapter = english_adapter()
            db.execute(
                "INSERT INTO materials "
                "(id,youtube_video_id,title,language,source,is_complete,transcript_hash,version,"
                "previous_version_id,adapter_version,created_at,kind) "
                "VALUES(?,NULL,?,?,?,1,?,?,?,?,?,'text')",
                (
                    material_id,
                    title.strip(),
                    language,
                    source,
                    digest,
                    previous["version"] + 1 if previous else 1,
                    replaces,
                    adapter.version,
                    datetime.now(UTC).isoformat(),
                ),
            )
            self._insert_blocks(
                db,
                material_id,
                language,
                [
                    {
                        "kind": "paragraph",
                        "start_ms": None,
                        "end_ms": None,
                        "text": text,
                        "original_text": text,
                        "original_fragments": [text],
                    }
                    for text in paragraphs
                ],
            )
            from backend.app.progress import ProgressService

            ProgressService(self).capture(db, reason="import")
        return self.get_material(material_id)

    def reading_position(self, material_id: str) -> dict[str, Any]:
        material = self.get_material(material_id)
        if material["kind"] != "text":
            raise MaterialError("Место чтения доступно только для текста.")
        with self.connect() as db:
            row = db.execute(
                "SELECT cue_id block_id,offset FROM reading_positions WHERE material_id=?",
                (material_id,),
            ).fetchone()
        return dict(row) if row else {"block_id": material["blocks"][0]["id"], "offset": 0}

    def save_reading_position(self, material_id: str, block_id: str, offset: int) -> dict[str, Any]:
        material = self.get_material(material_id)
        if material["kind"] != "text":
            raise MaterialError("Место чтения доступно только для текста.")
        block = next((block for block in material["blocks"] if block["id"] == block_id), None)
        if block is None or offset < 0 or offset > len(block["text"]):
            raise MaterialError("Проверьте абзац и позицию чтения.")
        with self.connect() as db:
            db.execute(
                "INSERT INTO reading_positions VALUES(?,?,?,?) ON CONFLICT(material_id) "
                "DO UPDATE SET cue_id=excluded.cue_id,offset=excluded.offset,"
                "updated_at=excluded.updated_at",
                (material_id, block_id, offset, datetime.now(UTC).isoformat()),
            )
        return {"block_id": block_id, "offset": offset}

    @staticmethod
    def _insert_blocks(
        db: sqlite3.Connection, material_id: str, language: str, blocks: list[dict[str, Any]]
    ) -> None:
        adapter = english_adapter()
        for position, block in enumerate(blocks):
            block_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO cues "
                "(id,material_id,position,start_ms,end_ms,text,original_text,"
                "original_fragments,kind) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    block_id,
                    material_id,
                    position,
                    block["start_ms"],
                    block["end_ms"],
                    block["text"],
                    block["original_text"],
                    json.dumps(block["original_fragments"]),
                    block.get("kind", "subtitle"),
                ),
            )
            for token in adapter.tokenize(block["text"]):
                db.execute(
                    "INSERT OR IGNORE INTO lexemes VALUES(?,?,?,?)",
                    (uuid.uuid4().hex, language, token["lemma"], token["part_of_speech"]),
                )
                lexeme = db.execute(
                    "SELECT id FROM lexemes WHERE language=? AND lemma=? AND part_of_speech=?",
                    (language, token["lemma"], token["part_of_speech"]),
                ).fetchone()
                db.execute(
                    "INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex,
                        material_id,
                        block_id,
                        lexeme["id"],
                        token["start_offset"],
                        token["end_offset"],
                        token["surface"],
                        int(token["uncertain"]),
                    ),
                )

    def import_track(
        self,
        *,
        youtube_url: str,
        title: str,
        content: str,
        format: str,
        language: str = "en",
        source: str | None = None,
        is_complete: bool = True,
        replaces: str | None = None,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        return self.import_cues(
            youtube_url=youtube_url,
            title=title,
            cues=parse_track(content, format),
            language=language,
            source=source or format,
            is_complete=is_complete,
            replaces=replaces,
            expected_generation=expected_generation,
        )

    def import_cues(
        self,
        *,
        youtube_url: str,
        title: str,
        cues: list[dict[str, Any]],
        language: str = "en",
        source: str,
        is_complete: bool = True,
        replaces: str | None = None,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Save normalized cues without reparsing or changing their time boundaries."""
        video_id = youtube_id(youtube_url)
        if not cues or len(cues) > MAX_CUES:
            raise MaterialError("Дорожка должна содержать от 1 до 10 000 реплик.")
        if sum(len(cue["text"]) for cue in cues) > MAX_TEXT_CHARACTERS:
            raise MaterialError("Текст субтитров должен содержать не больше 200 000 символов.")
        previous_start = -1
        for cue in cues:
            if (
                not cue["text"].strip()
                or "\x00" in cue["text"]
                or cue["start_ms"] < previous_start
                or cue["end_ms"] <= cue["start_ms"]
            ):
                raise MaterialError("Проверьте текст и временные метки реплик.")
            previous_start = cue["start_ms"]
        if language != "en":
            raise MaterialError("В этой версии поддерживается английский язык (en).")
        if replaces and self.get_material(replaces)["youtube_video_id"] != video_id:
            raise MaterialError("Новая дорожка должна относиться к тому же видео.")
        canonical: dict[str, Any] = {
            "video_id": video_id,
            "language": language,
            "cues": [{k: cue[k] for k in ("start_ms", "end_ms", "text")} for cue in cues],
        }
        # Equal text with a different known origin must not hide automatic captions
        # behind a previous file import, or attach an edited result to the wrong source.
        if source.startswith("youtube-"):
            canonical["caption_source"] = source
        if source == "youtube-improved":
            canonical["origin_id"] = replaces
        digest = hashlib.sha256(
            json.dumps(
                canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                expected_generation is not None
                and db.execute(
                    "SELECT value FROM storage_metadata WHERE key='generation'"
                ).fetchone()[0]
                != expected_generation
            ):
                raise MaterialError("Данные были удалены. Добавьте материал заново.")
            existing = db.execute(
                "SELECT id FROM materials "
                "WHERE youtube_video_id=? AND language=? AND transcript_hash=?",
                (video_id, language, digest),
            ).fetchone()
            if existing:
                db.commit()
                return self.get_material(existing["id"])
            material_id = uuid.uuid4().hex
            adapter = english_adapter()
            previous = db.execute(
                "SELECT id,version FROM materials WHERE youtube_video_id=? AND language=? "
                "ORDER BY version DESC LIMIT 1",
                (video_id, language),
            ).fetchone()
            db.execute(
                "INSERT INTO materials "
                "(id,youtube_video_id,title,language,source,is_complete,transcript_hash,version,"
                "previous_version_id,adapter_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    material_id,
                    video_id,
                    title.strip() or video_id,
                    language,
                    source,
                    int(is_complete),
                    digest,
                    previous["version"] + 1 if previous else 1,
                    replaces or (previous["id"] if previous else None),
                    adapter.version,
                    datetime.now(UTC).isoformat(),
                ),
            )
            self._insert_blocks(db, material_id, language, cues)
            from backend.app.progress import ProgressService

            ProgressService(self).capture(db, reason="import")
        return self.get_material(material_id)
