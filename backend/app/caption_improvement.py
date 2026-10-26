"""Persistent progress for bounded background caption editing jobs."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic_ai.models import Model

from backend.app.agents.subtitles import (
    MAX_BATCH_CHARACTERS,
    PROMPT_VERSION,
    caption_batches,
    improve_batch,
)
from backend.app.ai_connection.service import ConnectionService
from backend.app.ai_connection.settings import AISettings
from backend.app.materials import MaterialError, MaterialLibrary

logger = logging.getLogger("backend.app.agents.captions")


class CaptionImprovementService:
    def __init__(
        self,
        library: MaterialLibrary,
        connection: ConnectionService | None,
        *,
        model_factory: Callable[[], Model] | None = None,
    ) -> None:
        self.library = library
        self.connection = connection
        self.model_factory = model_factory
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.slots = asyncio.Semaphore(1)
        # A process restart cannot silently leave a job looking active forever.
        with library.connect() as db:
            db.execute(
                "UPDATE subtitle_improvements SET status='failed',error=? "
                "WHERE status IN ('queued','running')",
                ("Обработка прервалась при перезапуске. Можно запустить её снова.",),
            )

    def get(self, source_id: str) -> dict[str, Any] | None:
        with self.library.connect() as db:
            row = db.execute(
                "SELECT * FROM subtitle_improvements WHERE source_id=?", (source_id,)
            ).fetchone()
            return dict(row) if row else None

    def start(self, source_id: str) -> dict[str, Any]:
        source = self.library.get_material(source_id)
        if source["source"] != "youtube-automatic":
            raise MaterialError("Улучшение доступно для автоматической дорожки YouTube.")
        existing = self.get(source_id)
        if existing and existing["status"] in {"queued", "running", "completed"}:
            return existing
        settings = (
            self.connection.settings_store.read()
            if self.connection
            else AISettings(model="gpt-6-luna", reasoning_effort="low")
        )
        generation = self.library.get_profile()["storage_generation"]
        job_id = uuid.uuid4().hex
        with self.library.connect() as db:
            db.execute(
                "INSERT INTO subtitle_improvements "
                "(source_id,id,status,total_batches,model,reasoning_effort,"
                "prompt_version,created_at) "
                "VALUES(?,?,'queued',?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
                "id=excluded.id,status='queued',completed_batches=0,result_id=NULL,changed_cues=0,"
                "error=NULL,total_batches=excluded.total_batches,model=excluded.model,"
                "reasoning_effort=excluded.reasoning_effort,prompt_version=excluded.prompt_version,"
                "created_at=excluded.created_at",
                (
                    source_id,
                    job_id,
                    len(caption_batches(source["cues"])),
                    settings.model,
                    settings.reasoning_effort,
                    PROMPT_VERSION,
                    datetime.now(UTC).isoformat(),
                ),
            )
        self.tasks[source_id] = asyncio.create_task(self._run(source, job_id, settings, generation))
        result = self.get(source_id)
        assert result is not None
        return result

    def _update(self, job_id: str, **values: Any) -> None:
        fields = ",".join(f"{key}=?" for key in values)
        with self.library.connect() as db:
            db.execute(
                f"UPDATE subtitle_improvements SET {fields} WHERE id=?", (*values.values(), job_id)
            )

    async def _run(
        self,
        source: dict[str, Any],
        job_id: str,
        settings: AISettings,
        generation: str,
    ) -> None:
        try:
            async with self.slots:
                self._update(job_id, status="running")
                edits: dict[str, str] = {}
                cues = source["cues"]
                offset = 0
                for index, batch in enumerate(caption_batches(cues), 1):
                    if any(len(cue["text"]) > MAX_BATCH_CHARACTERS for cue in batch):
                        raise HTTPException(
                            422,
                            "Реплика слишком длинная для улучшения. "
                            "Исходная дорожка доступна для просмотра.",
                        )
                    context = {
                        "before": [
                            {"cue_id": c["id"], "text": c["text"][:1500]}
                            for c in cues[max(0, offset - 2) : offset]
                        ],
                        "after": [
                            {"cue_id": c["id"], "text": c["text"][:1500]}
                            for c in cues[offset + len(batch) : offset + len(batch) + 2]
                        ],
                    }

                    async def operation(model: Model) -> dict[str, str]:
                        return await improve_batch(
                            model=model,
                            batch=batch,
                            context=context,
                            title=source["title"],
                            reasoning_effort=settings.reasoning_effort,
                        )

                    if self.model_factory is not None:
                        async with asyncio.timeout(30):
                            accepted = await operation(self.model_factory())
                    elif self.connection is not None:
                        accepted = await self.connection.execute(operation, settings=settings)
                    else:
                        raise HTTPException(409, "Сначала подключите подписку в настройках.")
                    edits.update(accepted)
                    offset += len(batch)
                    self._update(job_id, completed_batches=index)
                # No await between checking generation, saving and publishing the result.
                # Reset cancels jobs first; the database guard also rejects stale saves.
                if edits:
                    revised = [
                        {
                            **cue,
                            "text": edits.get(cue["id"], cue["text"]),
                            "original_text": cue["text"],
                        }
                        for cue in cues
                    ]
                    result = self.library.import_cues(
                        youtube_url=source["youtube_url"],
                        title=source["title"],
                        cues=revised,
                        language=source["language"],
                        source="youtube-improved",
                        is_complete=source["is_complete"],
                        replaces=source["id"],
                        expected_generation=generation,
                    )
                else:
                    result = source
                self._update(
                    job_id, status="completed", result_id=result["id"], changed_cues=len(edits)
                )
                logger.info("caption_job=%s status=completed changed_cues=%s", job_id, len(edits))
        except asyncio.CancelledError:
            self._update(job_id, status="failed", error="Обработка прервана. Можно повторить.")
            raise
        except Exception as error:
            message = (
                error.detail
                if isinstance(error, HTTPException) and isinstance(error.detail, str)
                else "Не удалось улучшить субтитры. Исходная дорожка сохранена; можно повторить."
            )
            self._update(job_id, status="failed", error=message)
            logger.info("caption_job=%s status=failed", job_id)
        finally:
            if self.tasks.get(source["id"]) is asyncio.current_task():
                self.tasks.pop(source["id"], None)

    async def close(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def create_caption_router(service: CaptionImprovementService) -> APIRouter:
    router = APIRouter(prefix="/api/materials")

    @router.get("/{source_id}/improvement")
    def improvement_status(source_id: str) -> dict[str, Any] | None:
        service.library.get_material(source_id)
        return service.get(source_id)

    @router.post("/{source_id}/improvement", status_code=202)
    async def start_improvement(source_id: str) -> dict[str, Any]:
        return service.start(source_id)

    return router
