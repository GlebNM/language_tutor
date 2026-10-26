"""Vocabulary snapshots and elapsed, audible playback in the learner's timezone."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from backend.app.knowledge import KnowledgeService
from backend.app.materials import MaterialLibrary


def timezone_for(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(422, "Укажите существующий часовой пояс.") from exc


def midnight(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, zone).astimezone(UTC)


def elapsed_ms(intervals: list[tuple[datetime, datetime]]) -> int:
    """Union simultaneous tabs, while retaining later replays as elapsed time."""
    total = 0.0
    end: datetime | None = None
    for start, stop in sorted(intervals):
        if end is None or start > end:
            total += (stop - start).total_seconds()
        elif stop > end:
            total += (stop - end).total_seconds()
        end = max(end, stop) if end else stop
    return round(total * 1000)


class PlaybackInterval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    started_at: AwareDatetime
    ended_at: AwareDatetime

    @model_validator(mode="after")
    def valid_duration(self) -> PlaybackInterval:
        duration = (self.ended_at - self.started_at).total_seconds()
        if not 0 < duration <= 300:
            raise ValueError("Отрезок воспроизведения должен длиться от 0 до 300 секунд.")
        return self


class PlaybackInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    material_id: str = Field(min_length=1, max_length=100)
    generation: str = Field(min_length=1, max_length=100)
    intervals: list[PlaybackInterval] = Field(min_length=1, max_length=100)


class ProgressService:
    def __init__(self, library: MaterialLibrary):
        self.library = library

    def capture(self, db: sqlite3.Connection, *, reason: str = "learning") -> dict[str, int]:
        counts = dict.fromkeys(["0", "1", "2", "3", "unassessed", "ignored", "provisional"], 0)
        for entry in KnowledgeService(self.library).dictionary(db=db):
            knowledge = entry["knowledge"]
            if knowledge["ignored"]:
                counts["ignored"] += 1
                continue
            score = knowledge["score"]
            counts["unassessed" if score is None else str(score)] += 1
            if knowledge["provisional"] and score in {2, 3}:
                counts["provisional"] += 1
        counts["known"] = counts["2"] + counts["3"]
        counts["confident"] = counts["3"]
        encoded = json.dumps(counts, sort_keys=True)
        previous = db.execute(
            "SELECT counts_json FROM progress_vocabulary ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if previous is None or previous[0] != encoded or reason == "diagnostic":
            db.execute(
                "INSERT INTO progress_vocabulary(occurred_at,reason,counts_json) VALUES(?,?,?)",
                (datetime.now(UTC).isoformat(), reason if previous else "initial", encoded),
            )
        return counts

    def record_playback(self, incoming: PlaybackInput) -> dict[str, int]:
        now = datetime.now(UTC)
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            generation = db.execute(
                "SELECT value FROM storage_metadata WHERE key='generation'"
            ).fetchone()[0]
            if incoming.generation != generation:
                raise HTTPException(409, "Учебные данные изменились. Откройте материал заново.")
            if not db.execute(
                "SELECT 1 FROM materials WHERE id=?", (incoming.material_id,)
            ).fetchone():
                raise HTTPException(404, "Материал не найден.")
            saved = 0
            for interval in incoming.intervals:
                start = interval.started_at.astimezone(UTC)
                end = interval.ended_at.astimezone(UTC)
                values = (incoming.material_id, start.isoformat(), end.isoformat())
                existing = db.execute(
                    "SELECT material_id,started_at,ended_at FROM playback_intervals WHERE id=?",
                    (interval.id,),
                ).fetchone()
                if existing:
                    if tuple(existing) != values:
                        raise HTTPException(409, "Этот отрезок уже относится к другому просмотру.")
                    continue
                if end > now + timedelta(seconds=30) or start < now - timedelta(days=1):
                    raise HTTPException(422, "Проверьте дату и время на устройстве.")
                db.execute("INSERT INTO playback_intervals VALUES(?,?,?,?)", (interval.id, *values))
                saved += 1
        return {"saved": saved}

    @staticmethod
    def playback_rows(db: sqlite3.Connection, start: datetime, end: datetime) -> list[sqlite3.Row]:
        return db.execute(
            "SELECT p.*,m.title FROM playback_intervals p JOIN materials m ON m.id=p.material_id "
            "WHERE p.ended_at>? AND p.started_at<? ORDER BY p.started_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()

    @staticmethod
    def clipped(
        rows: list[sqlite3.Row], start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        return [
            (
                max(start, datetime.fromisoformat(row["started_at"])),
                min(end, datetime.fromisoformat(row["ended_at"])),
            )
            for row in rows
            if datetime.fromisoformat(row["ended_at"]) > start
            and datetime.fromisoformat(row["started_at"]) < end
        ]

    def summary(self, days: int, timezone: str) -> dict[str, Any]:
        if days not in {7, 30, 90, 365}:
            raise HTTPException(422, "Выберите период: 7, 30, 90 или 365 дней.")
        zone = timezone_for(timezone)
        today = datetime.now(zone).date()
        first = today - timedelta(days=days - 1)
        calendar_first = today - timedelta(days=364)
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            counts = self.capture(db, reason="reconcile")
            snapshots = db.execute("SELECT * FROM progress_vocabulary ORDER BY id").fetchall()
            tracking_since = db.execute(
                "SELECT value FROM storage_metadata WHERE key='playback_tracking_since'"
            ).fetchone()[0]
            activities = db.execute(
                "SELECT occurred_at FROM learning_activity WHERE language=? AND occurred_at>=?",
                (
                    self.library.get_profile()["target_language"],
                    midnight(calendar_first, zone).isoformat(),
                ),
            ).fetchall()
            playback = self.playback_rows(
                db, midnight(calendar_first, zone), midnight(today + timedelta(days=1), zone)
            )
        active = {datetime.fromisoformat(row[0]).astimezone(zone).date() for row in activities}
        active.update(
            datetime.fromisoformat(row["occurred_at"]).astimezone(zone).date()
            for row in snapshots
            if row["reason"] == "diagnostic"
        )
        intervals_by_day: dict[date, list[tuple[datetime, datetime]]] = defaultdict(list)
        for row in playback:
            start = max(midnight(calendar_first, zone), datetime.fromisoformat(row["started_at"]))
            stop = min(
                midnight(today + timedelta(days=1), zone), datetime.fromisoformat(row["ended_at"])
            )
            while start < stop:
                day = start.astimezone(zone).date()
                end = min(stop, midnight(day + timedelta(days=1), zone))
                intervals_by_day[day].append((start, end))
                start = end
        totals: dict[date, int] = {}
        for i in range(365):
            day = calendar_first + timedelta(days=i)
            value = elapsed_ms(intervals_by_day[day])
            totals[day] = value
            if value:
                active.add(day)
        initial = snapshots[0]
        initial_day = datetime.fromisoformat(initial["occurred_at"]).astimezone(zone).date()
        state = json.loads(initial["counts_json"])
        start_state = state.copy()
        cursor = 1
        series = []
        for i in range(days):
            day = first + timedelta(days=i)
            changes = 0
            baseline_change = 0
            end = midnight(day + timedelta(days=1), zone)
            while cursor < len(snapshots):
                snapshot = snapshots[cursor]
                stamp = datetime.fromisoformat(snapshot["occurred_at"])
                if stamp >= end:
                    break
                following = json.loads(snapshot["counts_json"])
                if stamp >= midnight(day, zone):
                    difference = following["known"] - state["known"]
                    changes += difference
                    if snapshot["reason"] in {"diagnostic", "import", "reconcile"}:
                        baseline_change += difference
                state = following
                cursor += 1
                if stamp < midnight(first, zone):
                    start_state = state.copy()
            series.append(
                {
                    "date": day.isoformat(),
                    "known": state["known"] if day >= initial_day else None,
                    "confident": state["confident"] if day >= initial_day else None,
                    "known_change": changes,
                    "baseline_change": baseline_change,
                    "watched_ms": totals[day],
                    "active": day in active,
                }
            )
        streak = 0
        day = today if today in active else today - timedelta(days=1)
        while day in active:
            streak += 1
            day -= timedelta(days=1)
        return {
            "timezone": timezone,
            "days": days,
            "today": today.isoformat(),
            "vocabulary_tracking_since": initial["occurred_at"],
            "playback_tracking_since": tracking_since,
            "counts": counts,
            "known_change": counts["known"] - start_state["known"],
            "confident_change": counts["confident"] - start_state["confident"],
            "watched_ms": sum(totals[first + timedelta(days=i)] for i in range(days)),
            "active_days": sum(first <= day <= today for day in active),
            "streak": streak,
            "series": series,
            "calendar": [
                {
                    "date": (calendar_first + timedelta(days=i)).isoformat(),
                    "watched_ms": totals[calendar_first + timedelta(days=i)],
                    "active": calendar_first + timedelta(days=i) in active,
                }
                for i in range(365)
            ],
        }

    def day(self, day: date, timezone: str, offset: int) -> dict[str, Any]:
        zone = timezone_for(timezone)
        start, end = midnight(day, zone), midnight(day + timedelta(days=1), zone)
        with self.library.connect() as db:
            playback = self.playback_rows(db, start, end)
            snapshots = db.execute(
                "SELECT * FROM progress_vocabulary WHERE occurred_at>=? AND occurred_at<? "
                "ORDER BY id",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
            previous = db.execute(
                "SELECT counts_json FROM progress_vocabulary WHERE occurred_at<? "
                "ORDER BY id DESC LIMIT 1",
                (start.isoformat(),),
            ).fetchone()
            words = db.execute(
                "SELECT a.word_json,a.data_json,a.material_id,m.title FROM learning_activity a "
                "LEFT JOIN materials m ON m.id=a.material_id WHERE event_type='knowledge_changed' "
                "AND a.occurred_at>=? AND a.occurred_at<? ORDER BY a.id DESC LIMIT 101 OFFSET ?",
                (start.isoformat(), end.isoformat(), offset),
            ).fetchall()
        baseline_change = 0
        previous_known = json.loads(previous[0])["known"] if previous else 0
        for snapshot in snapshots:
            known = json.loads(snapshot["counts_json"])["known"]
            if snapshot["reason"] in {"diagnostic", "import", "reconcile"}:
                baseline_change += known - previous_known
            previous_known = known
        material_ids = dict.fromkeys(row["material_id"] for row in playback)
        return {
            "date": day.isoformat(),
            "baseline_change": baseline_change,
            "watched_ms": elapsed_ms(self.clipped(playback, start, end)),
            "materials": [
                {
                    "id": material_id,
                    "title": next(
                        row["title"] for row in playback if row["material_id"] == material_id
                    ),
                    "watched_ms": elapsed_ms(
                        self.clipped(
                            [row for row in playback if row["material_id"] == material_id],
                            start,
                            end,
                        )
                    ),
                }
                for material_id in material_ids
            ],
            "words": [
                {
                    "word": json.loads(row["word_json"]),
                    "data": json.loads(row["data_json"]),
                    "material_id": row["material_id"],
                    "material_title": row["title"],
                }
                for row in words[:100]
            ],
            "has_more": len(words) > 100,
        }


def create_progress_router(library: MaterialLibrary) -> APIRouter:
    service = ProgressService(library)
    with library.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        service.capture(db)
    router = APIRouter(prefix="/api/progress", tags=["Learning progress"])

    @router.get("")
    def summary(
        days: int = Query(default=30, ge=1, le=365),
        timezone: str = Query(default="UTC", max_length=100),
    ) -> dict[str, Any]:
        return service.summary(days, timezone)

    @router.get("/day")
    def day(
        date: date,
        timezone: str = Query(default="UTC", max_length=100),
        offset: int = Query(default=0, ge=0, le=100000),
    ) -> dict[str, Any]:
        return service.day(date, timezone, offset)

    @router.post("/playback")
    def playback(incoming: PlaybackInput) -> dict[str, int]:
        return service.record_playback(incoming)

    return router
