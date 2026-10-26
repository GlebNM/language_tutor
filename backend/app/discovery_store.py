"""Small expiring SQLite caption cache, separate from the learning library."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from backend.app.language import EnglishAdapter
from backend.app.vocabulary import VERSION as VOCABULARY_VERSION
from backend.app.youtube import YouTubeTrack

ANALYSIS_VERSION = f"discovery-cues-1/{EnglishAdapter.version}/{VOCABULARY_VERSION}"


class DiscoveryCache:
    def __init__(self, directory: Path) -> None:
        self.path = directory / "discovery.sqlite3"
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS tracks (video_id TEXT PRIMARY KEY, "
                "title TEXT, source TEXT, content TEXT, analysis TEXT, version TEXT, saved REAL)"
            )
            db.execute("DELETE FROM tracks WHERE saved<?", (time.time() - 7 * 86400,))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, video_id: str) -> tuple[YouTubeTrack, list[dict[str, Any]]] | None:
        with self.connect() as db:
            db.execute("DELETE FROM tracks WHERE saved<?", (time.time() - 7 * 86400,))
            row = db.execute(
                "SELECT * FROM tracks WHERE video_id=? AND version=? AND saved>?",
                (video_id, ANALYSIS_VERSION, time.time() - 7 * 86400),
            ).fetchone()
        if row is None:
            return None
        return (
            YouTubeTrack(video_id, row["title"], row["content"], row["source"]),
            json.loads(row["analysis"]),
        )

    def put(self, track: YouTubeTrack, analysis: list[dict[str, Any]]) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM tracks WHERE saved<?", (time.time() - 7 * 86400,))
            db.execute(
                "INSERT OR REPLACE INTO tracks VALUES(?,?,?,?,?,?,?)",
                (
                    track.video_id,
                    track.title,
                    track.source,
                    track.content,
                    json.dumps(analysis, ensure_ascii=False),
                    ANALYSIS_VERSION,
                    time.time(),
                ),
            )
            db.execute(
                "DELETE FROM tracks WHERE video_id NOT IN "
                "(SELECT video_id FROM tracks ORDER BY saved DESC LIMIT 30)"
            )

    def clear(self) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM tracks")
        # DELETE removes rows; VACUUM also removes cached text from free pages.
        with self.connect() as db:
            db.execute("VACUUM")
