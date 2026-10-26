"""Bounded background analysis and foreground phrase selection for one learner store."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic_ai.models import Model

from backend.app.agents.expressions import (
    PROMPT_VERSION,
    ExpressionExtraction,
    expression_batches,
    extract_expressions,
)
from backend.app.ai_connection.service import ConnectionService
from backend.app.ai_connection.settings import AISettings
from backend.app.expressions import (
    ExpressionKnowledge,
    ExpressionSelection,
    ExpressionSource,
    ExpressionStore,
    now,
)
from backend.app.materials import MaterialLibrary


class ExpressionAnalysis:
    def __init__(
        self,
        library: MaterialLibrary,
        connection: ConnectionService | None,
        *,
        model_factory: Callable[[], Model] | None = None,
    ) -> None:
        self.library = library
        self.store = ExpressionStore(library)
        self.connection = connection
        self.model_factory = model_factory
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.focus_tasks: dict[str, asyncio.Task[None]] = {}
        self.background_slot = asyncio.Semaphore(1)
        self.foreground_slot = asyncio.Semaphore(1)
        with library.connect() as db:
            db.execute(
                "UPDATE expression_jobs SET status='failed',error=? "
                "WHERE status IN ('queued','running')",
                ("Разбор прервался при перезапуске. Можно повторить.",),
            )

    @property
    def available(self) -> bool:
        return self.model_factory is not None or (
            self.connection is not None
            and self.connection.state.status in {"connected", "limit_reached"}
        )

    def settings(self) -> AISettings:
        return (
            self.connection.settings_store.read()
            if self.connection
            else AISettings(
                model="gpt-6-luna",
                reasoning_effort="low",
            )
        )

    def job(self, key: str) -> dict[str, Any] | None:
        with self.library.connect() as db:
            row = db.execute("SELECT * FROM expression_jobs WHERE source_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def state(self, source: ExpressionSource) -> dict[str, Any]:
        self.store.source(source)  # Authorize and reject deleted sources before cache lookup.
        occurrences = self.store.occurrences(source.key)
        return {
            "job": self.job(source.key),
            "occurrences": occurrences,
            "available": self.available,
            "counts": {
                "total": len(occurrences),
                "unassessed": sum(e["knowledge"]["score"] is None for e in occurrences),
                "learning": sum(e["knowledge"]["score"] in (0, 1) for e in occurrences),
            },
        }

    def start(self, selected: ExpressionSource) -> dict[str, Any]:
        source = self.store.source(selected)
        previous = self.job(selected.key)
        if previous and previous["status"] in {"queued", "running", "completed"}:
            return self.state(selected)
        if not self.available:
            raise HTTPException(409, "Для поиска новых выражений подключите подписку в настройках.")
        settings = self.settings()
        generation = self.library.get_profile()["storage_generation"]
        identity = uuid4().hex
        batches = expression_batches(source["cues"])
        with self.library.connect() as db:
            db.execute(
                "INSERT INTO expression_jobs "
                "(source_key,id,status,total_batches,model,reasoning_effort,"
                "prompt_version,created_at) "
                "VALUES(?,?,'queued',?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET "
                "id=excluded.id,status='queued',completed_batches=0,total_batches=excluded.total_batches,"
                "model=excluded.model,reasoning_effort=excluded.reasoning_effort,"
                "prompt_version=excluded.prompt_version,error=NULL,created_at=excluded.created_at",
                (
                    selected.key,
                    identity,
                    len(batches),
                    settings.model,
                    settings.reasoning_effort,
                    PROMPT_VERSION,
                    now(),
                ),
            )
        self.tasks[selected.key] = asyncio.create_task(
            self._background(source, identity, settings, generation),
        )
        return self.state(selected)

    def update(self, identity: str, **values: Any) -> None:
        with self.library.connect() as db:
            db.execute(
                "UPDATE expression_jobs SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
                (*values.values(), identity),
            )

    async def extract(
        self,
        source: dict[str, Any],
        cues: list[dict[str, Any]],
        settings: AISettings,
        *,
        selected: ExpressionSelection | None = None,
    ) -> ExpressionExtraction:
        known = self.store.known(source)

        async def operation(model: Model) -> ExpressionExtraction:
            return await extract_expressions(
                model=model,
                cues=cues,
                known=known,
                reasoning_effort=settings.reasoning_effort,
                focus_token_ids=selected.token_ids if selected else None,
                manual=selected.manual if selected else False,
            )

        if self.model_factory:
            async with asyncio.timeout(30):
                return await operation(self.model_factory())
        if self.connection:
            return await self.connection.execute(operation, settings=settings)
        raise HTTPException(409, "Сначала подключите подписку в настройках.")

    async def _background(
        self,
        source: dict[str, Any],
        identity: str,
        settings: AISettings,
        generation: str,
    ) -> None:
        try:
            async with self.background_slot:
                self.update(identity, status="running")
                offset = 0
                for index, batch in enumerate(expression_batches(source["cues"]), 1):
                    # Overlap neighbors; source IDs make overlapping results idempotent.
                    window = source["cues"][max(0, offset - 2) : offset + len(batch) + 2]
                    result = await self.extract(source, window, settings)
                    self.store.put(
                        source, result.expressions, generation=generation, job_id=identity
                    )
                    self.update(identity, completed_batches=index)
                    offset += len(batch)
                self.update(identity, status="completed")
        except asyncio.CancelledError:
            self.update(identity, status="failed", error="Разбор прерван. Можно повторить.")
            raise
        except Exception as error:
            message = (
                error.detail
                if isinstance(error, HTTPException) and isinstance(error.detail, str)
                else (
                    "Не удалось завершить поиск выражений. "
                    "Найденные примеры сохранены; можно повторить."
                )
            )
            self.update(identity, status="failed", error=message)
        finally:
            if self.tasks.get(source["key"]) is asyncio.current_task():
                self.tasks.pop(source["key"], None)

    async def resolve(self, selected: ExpressionSelection) -> dict[str, Any]:
        source = self.store.source(selected)
        cues = self.store.focus(source, selected)
        cached = self.store.occurrences(source["key"])
        phrasal = [
            e
            for e in cached
            if e["kind"] == "phrasal_verb" and selected.token_ids[0] in e["token_ids"]
        ]
        job = self.job(source["key"])
        if not selected.manual and (phrasal or (job and job["status"] == "completed")):
            return self.state(selected)
        if not self.available:
            if selected.manual:
                raise HTTPException(409, "Для разбора нового выражения подключите подписку.")
            return self.state(selected)
        generation = self.library.get_profile()["storage_generation"]
        key = source["key"] + ":" + ":".join(selected.token_ids) + str(selected.manual)
        if key not in self.focus_tasks:
            settings = self.settings()

            async def run() -> None:
                # The interactive deadline includes waiting for another foreground request.
                async with asyncio.timeout(30):
                    async with self.foreground_slot:
                        result = await self.extract(source, cues, settings, selected=selected)
                        self.store.put(
                            source,
                            result.expressions,
                            generation=generation,
                            manual=selected.manual,
                        )

            self.focus_tasks[key] = asyncio.create_task(run())

            def finished(completed: asyncio.Task[None]) -> None:
                if self.focus_tasks.get(key) is completed:
                    self.focus_tasks.pop(key, None)
                if not completed.cancelled():
                    completed.exception()  # Also consume errors after all browser callers leave.

            self.focus_tasks[key].add_done_callback(finished)
        task = self.focus_tasks[key]
        try:
            # A browser abort must not cancel another caller waiting for the same analysis.
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except HTTPException:
            raise
        except TimeoutError:
            raise HTTPException(504, "AI не ответил за 30 секунд. Повторите разбор.") from None
        except Exception:
            raise HTTPException(502, "Не удалось разобрать выражение. Можно повторить.") from None
        finally:
            if task.done() and self.focus_tasks.get(key) is task:
                self.focus_tasks.pop(key, None)
        return self.state(selected)

    async def close(self) -> None:
        tasks = [*self.tasks.values(), *self.focus_tasks.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.focus_tasks.clear()


def create_expression_router(service: ExpressionAnalysis) -> APIRouter:
    router = APIRouter(prefix="/api/expressions")

    @router.get("/source")
    def source(
        material_id: str | None = None,
        conversation_id: str | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        # Query parameters are validated with the same source invariants as mutations.
        from pydantic import ValidationError

        try:
            selected = ExpressionSource(
                material_id=material_id, conversation_id=conversation_id, message_id=message_id
            )
        except ValidationError:
            raise HTTPException(422, "Выберите материал либо сообщение разговора.") from None
        return service.state(selected)

    @router.post("/analyze", status_code=202)
    async def analyze(selected: ExpressionSource) -> dict[str, Any]:
        return service.start(selected)

    @router.post("/selection")
    async def selection(selected: ExpressionSelection) -> dict[str, Any]:
        return await service.resolve(selected)

    @router.get("")
    def dictionary(
        search: str = Query(default="", max_length=200),
        score: str = Query(default="", max_length=12),
        kind: str = Query(default="", max_length=20),
    ) -> list[dict[str, Any]]:
        return service.store.dictionary(search, score, kind)

    @router.get("/{expression_id}")
    def detail(expression_id: str) -> dict[str, Any]:
        return service.store.detail(expression_id)

    @router.put("/{expression_id}/knowledge")
    def knowledge(expression_id: str, value: ExpressionKnowledge) -> dict[str, Any]:
        return service.store.set_knowledge(expression_id, value)

    @router.post("/occurrences/{occurrence_id}/dismiss")
    def dismiss(occurrence_id: str) -> dict[str, str]:
        return service.store.dismiss(occurrence_id)

    return router
