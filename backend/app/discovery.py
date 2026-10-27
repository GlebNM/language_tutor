"""Local video discovery with bounded work and side-effect-free familiarity previews."""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_ai.models import Model

from backend.app.agents.discovery import plan_query
from backend.app.ai_connection.service import ConnectionService
from backend.app.ai_connection.storage import CredentialStore
from backend.app.diagnostic_data import frequency_group
from backend.app.discovery_store import DiscoveryCache
from backend.app.knowledge import calculate_difficulty, knowledge_snapshot, knowledge_value
from backend.app.language import english_adapter
from backend.app.materials import MaterialError, MaterialLibrary, parse_track, youtube_id
from backend.app.vocabulary import CALENDAR_NAMES, NAME_ENTITIES, sound_reason, vocabulary_value
from backend.app.youtube import YouTubeTrack, fetch_youtube_track
from backend.app.youtube_search import search_videos

Effort = Literal["any", "easy", "some", "challenging"]
TERMINAL = {"completed", "failed", "cancelled"}


class DiscoveryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=2, max_length=300)
    minimum_seconds: int = Field(default=0, ge=0, le=3600)
    maximum_seconds: int = Field(default=1800, ge=1, le=7200)
    effort: Effort = "any"
    translate_query: bool = False

    @model_validator(mode="after")
    def valid_range(self) -> DiscoveryInput:
        self.query = self.query.strip()
        if len(self.query) < 2 or self.minimum_seconds > self.maximum_seconds:
            raise ValueError("Укажите тему и корректный диапазон длительности.")
        return self


class SearchKeyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    api_key: SecretStr = Field(min_length=10, max_length=200)


@dataclass
class DiscoveryJob:
    request: DiscoveryInput
    generation: str
    id: str = field(default_factory=lambda: uuid4().hex)
    created: float = field(default_factory=time.monotonic)
    finished: float | None = None
    status: str = "searching"
    search_query: str = ""
    error: str | None = None
    notice: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    prepared: dict[str, tuple[YouTubeTrack, list[dict[str, Any]]]] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)


def analyze_track(track: YouTubeTrack) -> list[dict[str, Any]]:
    """Use the same cue context, vocabulary policy and lemma version as material import."""
    cues = parse_track(track.content, "srt")
    if sum(len(cue["text"]) for cue in cues) > 60000:
        raise MaterialError("Для быстрого подбора выберите видео с более короткой дорожкой.")
    adapter = english_adapter()
    features = adapter.vocabulary_features("\n".join(cue["text"] for cue in cues))
    offset = 0
    for cue in cues:
        cue["tokens"] = adapter.tokenize(cue["text"])
        for token in cue["tokens"]:
            reason = sound_reason(token["surface"])
            pos, entity = features.get(
                (offset + token["start_offset"], offset + token["end_offset"]), ("", "")
            )
            if (
                reason is None
                and token["part_of_speech"] == pos == "PROPN"
                and (
                    entity in NAME_ENTITIES
                    or entity == "DATE"
                    and token["surface"].casefold() in CALENDAR_NAMES
                )
            ):
                reason = "proper_name"
            token["reason"] = reason
        offset += len(cue["text"]) + 1
    return cues


def score_analysis(
    cues: list[dict[str, Any]],
    snapshot: dict[tuple[str, str, str], dict[str, Any]],
    prior_groups: set[int],
) -> tuple[dict[str, Any], list[str]]:
    scored = []
    reviewing: Counter[str] = Counter()
    for cue in cues:
        tokens = []
        markers = [(m.start(), m.end()) for m in re.finditer(r"\[[^\]]*\]", cue["text"])]
        for token in cue["tokens"]:
            key = ("en", token["lemma"], token["part_of_speech"])
            knowledge = snapshot.get(key)
            if knowledge is None:
                group = frequency_group(token["lemma"])
                knowledge = (
                    knowledge_value(2, source="prior")
                    if group in prior_groups
                    else knowledge_value(None)
                )
            vocabulary = vocabulary_value(token["reason"], knowledge)
            tokens.append(
                {
                    **token,
                    "lexeme_id": "|".join(key),
                    "knowledge": knowledge,
                    "vocabulary": vocabulary,
                }
            )
            if (
                vocabulary["eligible"]
                and not knowledge["ignored"]
                and knowledge["score"] == 1
                and not any(start <= token["start_offset"] < end for start, end in markers)
            ):
                reviewing[token["lemma"]] += 1
        scored.append({**cue, "tokens": tokens})
    return calculate_difficulty({"cues": scored, "is_complete": True}), [
        word for word, _ in reviewing.most_common(5)
    ]


class DiscoveryService:
    def __init__(self, library: MaterialLibrary, connection: ConnectionService | None) -> None:
        self.library = library
        self.connection = connection
        self.cache = DiscoveryCache(library.database_path.parent)
        self.credentials = CredentialStore(library.database_path.parent / "credentials")
        self.key_path = self.credentials.directory / "youtube-api-key"
        self.jobs: dict[str, DiscoveryJob] = {}
        # Kept inside synchronous workers: cancellation does not release a live HTTP slot.
        self.network_slots = threading.BoundedSemaphore(2)
        self.analysis_slot = threading.Lock()
        self.blocked_until = 0.0
        self.timeout_seconds = 60.0
        self.search_cache: dict[tuple[str, int, int], tuple[float, list[dict[str, Any]]]] = {}

    def api_key(self) -> str:
        try:
            return self.key_path.read_text().strip()
        except FileNotFoundError:
            return os.getenv("YOUTUBE_API_KEY", "").strip()

    def settings(self) -> dict[str, Any]:
        return {
            "configured": bool(self.api_key()),
            "source": (
                "saved" if self.key_path.exists() else "environment" if self.api_key() else None
            ),
            "ai_available": bool(self.connection and self.connection.available),
        }

    def start(self, request: DiscoveryInput) -> DiscoveryJob:
        is_url = re.match(r"(?:https?://|www\.|youtu\.be/)", request.query, re.I)
        if is_url:
            youtube_id(request.query)
        elif not self.api_key():
            raise HTTPException(409, "Добавьте ключ поиска YouTube или вставьте ссылку на видео.")
        if any(job.status not in TERMINAL for job in self.jobs.values()):
            raise HTTPException(409, "Подбор уже выполняется. Дождитесь его или остановите.")
        if time.monotonic() < self.blocked_until:
            raise HTTPException(429, "YouTube временно ограничил субтитры. Повторите через минуту.")
        # Retain a small recent history for back navigation and cached imports.
        for old in sorted(self.jobs.values(), key=lambda value: value.created)[:-9]:
            self.jobs.pop(old.id, None)
        job = DiscoveryJob(request, self.library.get_profile()["storage_generation"])
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job, bool(is_url)))
        return job

    def job(self, job_id: str) -> DiscoveryJob:
        job = self.jobs.get(job_id)
        if job is None or job.generation != self.library.get_profile()["storage_generation"]:
            raise HTTPException(404, "Подбор больше недоступен. Запустите поиск заново.")
        return job

    def public(self, job: DiscoveryJob) -> dict[str, Any]:
        snapshot = knowledge_snapshot(self.library)
        with self.library.connect() as db:
            groups = {
                row[0]
                for row in db.execute(
                    "SELECT frequency_group FROM diagnostic_rules WHERE language='en' AND enabled=1"
                )
            }
        results = []
        for position, candidate in enumerate(job.candidates):
            value = {**candidate, "position": position}
            video_id = candidate["video_id"]
            if prepared := job.prepared.get(video_id):
                track, analysis = prepared
                difficulty, words = score_analysis(analysis, snapshot, groups)
                value.update(
                    status="ready",
                    source=track.source,
                    difficulty=difficulty,
                    review_words=words,
                )
            results.append(value)
        labels = {"Мало новой лексики": 0, "Понадобятся подсказки": 1, "Много новой лексики": 2}
        target = {"easy": 0, "some": 1, "challenging": 2}.get(job.request.effort)

        def order(result: dict[str, Any]) -> tuple[int, int, int]:
            label = labels.get(result.get("difficulty", {}).get("label"))
            return (
                0 if result["status"] == "ready" else 1,
                abs(label - target)
                if label is not None and target is not None
                else 3
                if target is not None
                else 0,
                result["position"],
            )

        return {
            "id": job.id,
            "status": job.status,
            "query": job.request.query,
            "search_query": job.search_query,
            "error": job.error,
            "notice": job.notice,
            "elapsed_seconds": round((job.finished or time.monotonic()) - job.created, 1),
            "checked": sum(result["status"] != "pending" for result in results),
            "total": len(results),
            "results": sorted(results, key=order),
        }

    def _fetch(self, job: DiscoveryJob, video_id: str) -> YouTubeTrack:
        with self.network_slots:
            if job.cancelled.is_set():
                raise MaterialError("Подбор остановлен.")
            return fetch_youtube_track(f"https://www.youtube.com/watch?v={video_id}")

    def _search(self, job: DiscoveryJob, query: str) -> list[dict[str, Any]]:
        with self.network_slots:
            if job.cancelled.is_set():
                return []
            return search_videos(
                query, self.api_key(), job.request.minimum_seconds, job.request.maximum_seconds
            )

    def _analyze(self, job: DiscoveryJob, track: YouTubeTrack) -> list[dict[str, Any]]:
        with self.analysis_slot:
            if job.cancelled.is_set():
                raise MaterialError("Подбор остановлен.")
            return analyze_track(track)

    async def _candidate(self, job: DiscoveryJob, candidate: dict[str, Any]) -> None:
        video_id = candidate["video_id"]
        try:
            cached = self.cache.get(video_id)
            if cached is None:
                track = await asyncio.to_thread(self._fetch, job, video_id)
                analysis = await asyncio.to_thread(self._analyze, job, track)
                if not job.cancelled.is_set():
                    self.cache.put(track, analysis)
            else:
                track, analysis = cached
            if job.cancelled.is_set():
                return
            candidate["cached"] = cached is not None
            if candidate.get("direct"):
                candidate["title"] = track.title
                candidate["duration_seconds"] = round(analysis[-1]["end_ms"] / 1000)
            job.prepared[video_id] = (track, analysis)
            candidate["status"] = "ready"
        except MaterialError as error:
            candidate.update(status="unavailable", error=str(error))
            if "ограничил" in str(error):
                self.blocked_until = time.monotonic() + 60
        except Exception:
            candidate.update(status="unavailable", error="Не удалось оценить субтитры этого видео.")

    async def _run(self, job: DiscoveryJob, direct: bool) -> None:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                query = job.request.query
                if direct:
                    candidates = [
                        {
                            "video_id": youtube_id(query),
                            "title": "Видео по ссылке",
                            "channel": "",
                            "duration_seconds": None,
                            "direct": True,
                        }
                    ]
                else:
                    if job.request.translate_query:
                        if self.connection is None:
                            raise MaterialError("Для перевода запроса подключите подписку ChatGPT.")
                        job.status = "planning"
                        settings = self.connection.settings_store.read().effective()

                        async def operation(model: Model) -> str:
                            return await plan_query(model, query, settings.reasoning_effort)

                        query = await self.connection.execute(operation, settings=settings)
                    job.search_query = query
                    job.status = "searching"
                    cache_key = (query, job.request.minimum_seconds, job.request.maximum_seconds)
                    saved = self.search_cache.get(cache_key)
                    if saved and saved[0] > time.monotonic() - 3600:
                        candidates = [dict(item) for item in saved[1]]
                    else:
                        candidates = await asyncio.to_thread(self._search, job, query)
                        if len(self.search_cache) >= 20:
                            self.search_cache.pop(next(iter(self.search_cache)))
                        self.search_cache[cache_key] = (time.monotonic(), candidates)
                        candidates = [dict(item) for item in candidates]
                job.candidates = [{**item, "status": "pending"} for item in candidates]
                job.status = "analyzing"
                # Two workers avoid queueing an unbounded number of synchronous requests.
                queue = iter(job.candidates)

                async def worker() -> None:
                    for candidate in queue:
                        if job.cancelled.is_set():
                            return
                        if time.monotonic() < self.blocked_until:
                            candidate.update(
                                status="unavailable", error="YouTube ограничил субтитры."
                            )
                            continue
                        await self._candidate(job, candidate)

                await asyncio.gather(worker(), worker())
                job.status = "completed"
                if not job.candidates:
                    job.notice = "Подходящих видео не найдено. Расширьте тему или длительность."
        except asyncio.CancelledError:
            job.status = "cancelled"
        except TimeoutError:
            job.status = "completed" if job.prepared else "failed"
            job.notice = "Время подбора истекло. Уже оценённые видео доступны."
            if not job.prepared:
                job.error = "Не удалось получить результаты за минуту. Попробуйте ещё раз."
        except (MaterialError, HTTPException) as error:
            job.status = "failed"
            job.error = str(error.detail) if isinstance(error, HTTPException) else str(error)
        except Exception:
            job.status = "failed"
            job.error = "Не удалось подобрать видео. Проверьте подключение и повторите."
        finally:
            job.finished = time.monotonic()
            job.cancelled.set()
            for candidate in job.candidates:
                if candidate["status"] == "pending":
                    candidate.update(status="unavailable", error="Проверка не завершена.")

    async def cancel(self, job: DiscoveryJob) -> None:
        if job.status in TERMINAL:
            return
        job.cancelled.set()
        if job.task:
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
        job.status = "cancelled"
        job.finished = time.monotonic()

    async def close(self) -> None:
        for job in list(self.jobs.values()):
            await self.cancel(job)

    async def reset(self) -> None:
        await self.close()
        self.jobs.clear()
        self.search_cache.clear()
        self.cache.clear()
        self.key_path.unlink(missing_ok=True)


def create_discovery_router(service: DiscoveryService) -> APIRouter:
    router = APIRouter(prefix="/api/discovery")

    @router.get("/settings")
    def settings() -> dict[str, Any]:
        return service.settings()

    @router.put("/settings")
    def save_key(body: SearchKeyInput) -> dict[str, Any]:
        key = body.api_key.get_secret_value().strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", key):
            raise HTTPException(422, "Проверьте формат ключа YouTube API.")
        service.credentials.write(service.key_path, key)
        service.search_cache.clear()
        return service.settings()

    @router.delete("/settings")
    def delete_key() -> dict[str, Any]:
        service.key_path.unlink(missing_ok=True)
        service.search_cache.clear()
        return service.settings()

    @router.post("/jobs", status_code=202)
    async def start(body: DiscoveryInput) -> dict[str, Any]:
        return service.public(service.start(body))

    @router.get("/jobs/{job_id}")
    def status(job_id: str) -> dict[str, Any]:
        return service.public(service.job(job_id))

    @router.delete("/jobs/{job_id}")
    async def cancel(job_id: str) -> dict[str, Any]:
        job = service.job(job_id)
        await service.cancel(job)
        return service.public(job)

    @router.post("/jobs/{job_id}/open/{video_id}", status_code=201)
    def open_video(job_id: str, video_id: str) -> dict[str, Any]:
        job = service.job(job_id)
        prepared = job.prepared.get(video_id)
        if prepared is None:
            raise HTTPException(409, "Сначала дождитесь загрузки субтитров этого видео.")
        track = prepared[0]
        return service.library.import_track(
            youtube_url=f"https://www.youtube.com/watch?v={track.video_id}",
            title=track.title,
            content=track.content,
            format="srt",
            source=track.source,
            expected_generation=job.generation,
        )

    return router
