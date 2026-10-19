-- Historical material schema v1, retained as an upgrade fixture.

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

PRAGMA user_version=1;
