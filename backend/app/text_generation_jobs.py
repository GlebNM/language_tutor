"""Persistent, bounded background jobs for private generated reading materials."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.mcp import CallToolFunc, MCPToolset, ToolResult
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage, UsageLimits

from backend.app.agents.text_generation import review_context, review_requirements, write_text
from backend.app.ai_connection.service import AIBudgetExceeded, AIResponseError, ConnectionService
from backend.app.ai_connection.settings import AISettings
from backend.app.learning_context import MAX_TOOL_CHARACTERS
from backend.app.materials import MaterialError, MaterialLibrary
from backend.app.tenant_context import mcp_headers
from backend.app.text_generation_analysis import (
    LENGTHS,
    LOAD_LIMITS,
    CannotGenerate,
    DraftText,
    GenerationRequest,
    VocabularySnapshot,
    assess_draft,
)

logger = logging.getLogger(__name__)
ACTIVE = {"queued", "preparing", "writing", "checking", "revising", "saving"}
TOOLS = {
    "get_generation_profile",
    "list_generation_vocabulary",
    "list_generation_expressions",
    "get_generation_word_knowledge",
    "check_text_difficulty",
    "list_draft_vocabulary_issues",
    "find_texts_with_word",
    "get_text",
}
JOB_TOOLS = TOOLS - {"find_texts_with_word", "get_text"}
T = TypeVar("T")


def now() -> str:
    return datetime.now(UTC).isoformat()


def _row(library: MaterialLibrary, job_id: str) -> dict[str, Any]:
    with library.connect() as db:
        if (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='text_generation_jobs'"
            ).fetchone()
            is None
        ):
            raise HTTPException(404, "Задание генерации не найдено.")
        row = db.execute("SELECT * FROM text_generation_jobs WHERE id=?", (job_id,)).fetchone()
        generation = db.execute(
            "SELECT value FROM storage_metadata WHERE key='generation'"
        ).fetchone()[0]
    if row is None or row["storage_generation"] != generation:
        raise HTTPException(404, "Задание больше недоступно. Запустите генерацию заново.")
    return dict(row)


def get_job_snapshot(library: MaterialLibrary, job_id: str) -> VocabularySnapshot:
    return VocabularySnapshot.from_dict(json.loads(_row(library, job_id)["snapshot_json"]))


def get_job(library: MaterialLibrary, job_id: str) -> dict[str, Any]:
    row = _row(library, job_id)
    result = {
        key: row[key]
        for key in (
            "id",
            "status",
            "revision",
            "model",
            "reasoning_effort",
            "error",
            "result_material_id",
            "storage_generation",
            "created_at",
            "updated_at",
        )
    }
    for key in ("request", "draft", "analysis", "feedback", "usage"):
        value = row[key + "_json"]
        result[key] = json.loads(value) if value else None
    return result


@dataclass
class ResponseRetryBudget:
    used: bool = False


class JobStopped(Exception):
    """A cancelled, deleted, or otherwise inactive job cannot write a late result."""


class TextGenerationService:
    def __init__(
        self,
        library: MaterialLibrary,
        connection: ConnectionService | None,
        mcp_url: str,
        *,
        model_factory: Callable[[], Model] | None = None,
    ) -> None:
        self.library = library
        self.connection = connection
        self.mcp_url = mcp_url
        self.model_factory = model_factory
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self._notifications: dict[str, dict[str, Any]] = {}
        self._last_completed_id: str | None = None
        self.max_versions = 5
        self.timeout_seconds = 300.0
        self.model_request_limit = 30
        self.tool_attempt_limit = 50
        with library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS text_generation_jobs(
                id TEXT PRIMARY KEY, storage_generation TEXT NOT NULL,
                idempotency_key TEXT, request_json TEXT NOT NULL, snapshot_json TEXT NOT NULL,
                status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                model TEXT NOT NULL, reasoning_effort TEXT, error TEXT,
                draft_json TEXT, analysis_json TEXT, feedback_json TEXT, usage_json TEXT,
                result_material_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(storage_generation,idempotency_key))""")
            db.execute("""CREATE TABLE IF NOT EXISTS text_generation_drafts(
                job_id TEXT NOT NULL REFERENCES text_generation_jobs(id) ON DELETE CASCADE,
                revision INTEGER NOT NULL, draft_json TEXT NOT NULL, analysis_json TEXT NOT NULL,
                context_json TEXT, requirements_json TEXT, created_at TEXT NOT NULL,
                PRIMARY KEY(job_id,revision))""")
            if "generation_job_id" in {
                row["name"] for row in db.execute("PRAGMA table_info(materials)")
            }:
                db.execute(
                    "UPDATE text_generation_jobs SET status='completed',result_material_id=("
                    "SELECT id FROM materials WHERE generation_job_id=text_generation_jobs.id),"
                    "error=NULL,updated_at=? WHERE status IN "
                    "('queued','preparing','writing','checking','revising','saving') AND EXISTS("
                    "SELECT 1 FROM materials WHERE generation_job_id=text_generation_jobs.id)",
                    (now(),),
                )
            db.execute("DELETE FROM text_generation_jobs WHERE status<>'completed'")

    @property
    def available(self) -> bool:
        return self.model_factory is not None or bool(
            self.connection and self.connection.state.status in {"connected", "limit_reached"}
        )

    def settings(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "max_versions": self.max_versions,
            "lengths": {
                key: {
                    name: value
                    for name, value in GenerationRequest.model_validate({"length": key})
                    .constraints()
                    .items()
                    if name
                    in {
                        "min_words",
                        "max_words",
                        "preferred_min_words",
                        "preferred_max_words",
                        "word_tolerance",
                    }
                }
                for key in LENGTHS
            },
            "difficulty_limits": {
                key: {
                    name: value
                    for name, value in GenerationRequest.model_validate({"difficulty": key})
                    .constraints()
                    .items()
                    if name in {"learning_ratio_max", "new_or_unassessed_ratio_max"}
                }
                for key in LOAD_LIMITS
            },
        }

    def list_jobs(self) -> list[dict[str, Any]]:
        with self.library.connect() as db:
            row = db.execute(
                "SELECT id FROM text_generation_jobs WHERE storage_generation=? "
                "AND status IN ('queued','preparing','writing','checking','revising','saving') "
                "ORDER BY created_at DESC LIMIT 1",
                (self.library.get_profile()["storage_generation"],),
            ).fetchone()
        if row:
            return [get_job(self.library, row["id"])]
        if self._last_completed_id:
            with suppress(HTTPException):
                return [get_job(self.library, self._last_completed_id)]
        return []

    def take_error(self) -> dict[str, Any] | None:
        return self._notifications.pop("error", None)

    def start(
        self,
        request: GenerationRequest,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        snapshot = VocabularySnapshot.capture(self.library)
        encoded = json.dumps(request.model_dump(), ensure_ascii=False, sort_keys=True)
        if idempotency_key:
            with self.library.connect() as db:
                existing = db.execute(
                    "SELECT id,request_json FROM text_generation_jobs "
                    "WHERE storage_generation=? AND idempotency_key=?",
                    (snapshot.storage_generation, idempotency_key),
                ).fetchone()
            if existing:
                if existing["request_json"] != encoded:
                    raise HTTPException(409, "Этот ключ повтора относится к другим настройкам.")
                return get_job(self.library, existing["id"])
        if not self.available:
            raise HTTPException(409, "Для создания текста подключите подписку в настройках.")
        settings = (
            self.connection.settings_store.read()
            if self.connection
            else AISettings(
                model="gpt-6-luna",
                reasoning_effort="low",
            )
        )
        identity = uuid4().hex
        stamp = now()
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM text_generation_jobs WHERE storage_generation=? "
                "AND status IN ('queued','preparing','writing','checking','revising','saving')",
                (snapshot.storage_generation,),
            ).fetchone():
                raise HTTPException(
                    409, "Текст уже создаётся. Дождитесь результата или остановите."
                )
            db.execute(
                "INSERT INTO text_generation_jobs(id,storage_generation,idempotency_key,"
                "request_json,snapshot_json,status,model,reasoning_effort,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'queued',?,?,?,?)",
                (
                    identity,
                    snapshot.storage_generation,
                    idempotency_key,
                    encoded,
                    json.dumps(snapshot.to_dict(), ensure_ascii=False),
                    settings.model,
                    settings.reasoning_effort,
                    stamp,
                    stamp,
                ),
            )
        self._notifications.clear()
        self._last_completed_id = None
        self.tasks[identity] = asyncio.create_task(self._run(identity, settings))
        return get_job(self.library, identity)

    def _update(self, identity: str, **values: Any) -> None:
        row = _row(self.library, identity)
        if row["status"] not in ACTIVE:
            raise JobStopped
        values["updated_at"] = now()
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT j.status,j.storage_generation,m.value FROM text_generation_jobs j "
                "JOIN storage_metadata m ON m.key='generation' WHERE j.id=?",
                (identity,),
            ).fetchone()
            if (
                current is None
                or current["status"] not in ACTIVE
                or current["storage_generation"] != current["value"]
            ):
                raise JobStopped
            db.execute(
                "UPDATE text_generation_jobs SET "
                + ",".join(key + "=?" for key in values)
                + " WHERE id=?",
                (*values.values(), identity),
            )

    async def _operation(
        self,
        settings: AISettings,
        operation: Callable[[Model], Awaitable[T]],
        *,
        timeout_seconds: float = 30.0,
        retry_budget: ResponseRetryBudget | None = None,
    ) -> T:
        async with asyncio.timeout(timeout_seconds):
            while True:
                try:
                    if self.model_factory:
                        return await operation(self.model_factory())
                    if self.connection:
                        return await self.connection.execute(
                            operation,
                            settings=settings,
                            timeout_seconds=timeout_seconds,
                        )
                    raise HTTPException(409, "Для создания текста подключите подписку.")
                except (AIResponseError, UnexpectedModelBehavior):
                    if retry_budget is None or retry_budget.used:
                        raise
                    retry_budget.used = True
                    logger.warning("text_generation_retry response_validation")

    def _fail(self, identity: str, status: str, message: str, usage: RunUsage) -> None:
        """A committed material wins over interruption of its awaiting task."""
        with suppress(HTTPException, JobStopped):
            row = _row(self.library, identity)
            with self.library.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                material = None
                if "generation_job_id" in {
                    column["name"] for column in db.execute("PRAGMA table_info(materials)")
                }:
                    material = db.execute(
                        "SELECT id FROM materials WHERE generation_job_id=?", (identity,)
                    ).fetchone()
                if material is not None:
                    self._last_completed_id = identity
                    db.execute(
                        "UPDATE text_generation_jobs SET status='completed',"
                        "result_material_id=?,error=NULL,updated_at=? "
                        "WHERE id=? AND storage_generation=?",
                        (material["id"], now(), identity, row["storage_generation"]),
                    )
                    return
                db.execute(
                    "UPDATE text_generation_jobs SET status=?,error=?,usage_json=?,updated_at=? "
                    "WHERE id=? AND storage_generation=? AND status IN "
                    "('queued','preparing','writing','checking','revising','saving')",
                    (
                        status,
                        message,
                        json.dumps(self._usage(usage)),
                        now(),
                        identity,
                        row["storage_generation"],
                    ),
                )

    def _save_version(
        self,
        identity: str,
        revision: int,
        draft: DraftText,
        analysis: dict[str, Any],
        context: dict[str, Any] | None,
        requirements: dict[str, Any] | None,
    ) -> None:
        self._update(
            identity,
            revision=revision,
            draft_json=json.dumps(draft.model_dump()),
            analysis_json=json.dumps(analysis, ensure_ascii=False),
        )
        with self.library.connect() as db:
            db.execute(
                "INSERT INTO text_generation_drafts VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(job_id,revision) DO UPDATE SET draft_json=excluded.draft_json,"
                "analysis_json=excluded.analysis_json,context_json=excluded.context_json,"
                "requirements_json=excluded.requirements_json",
                (
                    identity,
                    revision,
                    json.dumps(draft.model_dump(), ensure_ascii=False),
                    json.dumps(analysis, ensure_ascii=False),
                    json.dumps(context, ensure_ascii=False) if context is not None else None,
                    json.dumps(requirements, ensure_ascii=False)
                    if requirements is not None
                    else None,
                    now(),
                ),
            )

    async def _checks(
        self,
        identity: str,
        settings: AISettings,
        request: GenerationRequest,
        analysis: dict[str, Any],
        usage: RunUsage,
        limits: UsageLimits,
        retry_budget: ResponseRetryBudget | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]]:
        self._update(identity, status="checking")
        issues = list(analysis["violations"])
        if issues:
            return None, None, issues
        context = await self._operation(
            settings,
            lambda model: review_context(
                model=model,
                analysis=analysis,
                reasoning_effort=settings.reasoning_effort,
                usage=usage,
                usage_limits=limits,
            ),
            timeout_seconds=60,
            retry_budget=retry_budget,
        )
        self._update(identity, status="checking")
        requirements = await self._operation(
            settings,
            lambda model: review_requirements(
                model=model,
                analysis=analysis,
                settings=request,
                reasoning_effort=settings.reasoning_effort,
                usage=usage,
                usage_limits=limits,
            ),
            timeout_seconds=60,
            retry_budget=retry_budget,
        )
        context_report, requirements_report = context.as_report(), requirements.as_report()
        issues += context_report["blocking_issues"] + requirements_report["blocking_issues"]
        return context_report, requirements_report, issues

    async def _run(self, identity: str, settings: AISettings) -> None:
        usage = RunUsage()
        limits = UsageLimits(
            request_limit=self.model_request_limit, tool_calls_limit=self.tool_attempt_limit
        )
        attempts = 0
        retry_budget = ResponseRetryBudget()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                self._update(identity, status="preparing")
                request = GenerationRequest.model_validate(
                    get_job(self.library, identity)["request"]
                )
                previous_draft = None
                feedback: dict[str, Any] | None = None
                for revision in range(1, self.max_versions + 1):
                    snapshot = VocabularySnapshot.capture(self.library)
                    self._update(
                        identity,
                        snapshot_json=json.dumps(snapshot.to_dict()),
                        status="writing",
                        revision=revision,
                    )

                    async def scoped_call(
                        ctx: RunContext[Any],
                        call_tool: CallToolFunc,
                        name: str,
                        args: dict[str, Any],
                    ) -> ToolResult:
                        nonlocal attempts
                        attempts += 1
                        if attempts > self.tool_attempt_limit:
                            raise UsageLimitExceeded("Generation tool attempt limit exceeded")
                        if name not in TOOLS:
                            raise ModelRetry("Для создания текста этот инструмент недоступен.")
                        _row(self.library, identity)
                        args = dict(args)
                        if name in JOB_TOOLS:
                            args["job_id"] = identity
                        result = await call_tool(name, args)
                        encoded = (
                            result
                            if isinstance(result, str)
                            else json.dumps(
                                result,
                                ensure_ascii=False,
                            )
                        )
                        if len(encoded) > MAX_TOOL_CHARACTERS:
                            raise ModelRetry("Уменьши страницу учебного контекста.")
                        return result

                    tools = MCPToolset(
                        self.mcp_url,
                        headers=mcp_headers(),
                        process_tool_call=scoped_call,
                        read_timeout=10,
                        init_timeout=5,
                    )
                    async with tools:
                        attempts += 1
                        if attempts > self.tool_attempt_limit:
                            raise UsageLimitExceeded("Generation tool attempt limit exceeded")
                        raw_profile = await tools.direct_call_tool(
                            "get_generation_profile",
                            {"job_id": identity},
                        )
                        encoded = (
                            raw_profile
                            if isinstance(raw_profile, str)
                            else json.dumps(
                                raw_profile,
                                ensure_ascii=False,
                            )
                        )
                        if len(encoded) > MAX_TOOL_CHARACTERS:
                            raise ValueError("Generation profile exceeds the MCP limit")
                        profile = json.loads(encoded)
                        if profile.get("snapshot_id") != snapshot.snapshot_id:
                            raise ValueError("Generation profile does not match the dictionary")
                        if profile["profile_sufficiency"] == "insufficient":
                            self._update(
                                identity,
                                status="needs_changes",
                                error="Пройдите диагностику или уточните знакомые слова.",
                                feedback_json=json.dumps(
                                    {
                                        "blocking_issues": [
                                            {"criterion_id": "insufficient_profile"},
                                        ]
                                    }
                                ),
                            )
                            return
                        output = await self._operation(
                            settings,
                            lambda model: write_text(
                                model=model,
                                settings=request,
                                profile=profile,
                                previous_draft=previous_draft,
                                feedback=feedback,
                                toolsets=[tools.filtered(lambda ctx, tool: tool.name in TOOLS)],
                                reasoning_effort=settings.reasoning_effort,
                                usage=usage,
                                usage_limits=limits,
                            ),
                            timeout_seconds=90,
                            retry_budget=retry_budget,
                        )
                    if isinstance(output, CannotGenerate):
                        self._update(
                            identity,
                            status="needs_changes",
                            error=output.explanation,
                            feedback_json=json.dumps(
                                {
                                    "blocking_issues": [
                                        output.model_dump(),
                                    ]
                                },
                                ensure_ascii=False,
                            ),
                        )
                        return
                    analysis = assess_draft(
                        snapshot, output, request, revision=revision, draft_id=identity
                    )
                    self._save_version(identity, revision, output, analysis, None, None)
                    context, requirements, issues = await self._checks(
                        identity,
                        settings,
                        request,
                        analysis,
                        usage,
                        limits,
                        retry_budget,
                    )
                    if not issues:
                        current = VocabularySnapshot.capture(self.library)
                        if current.snapshot_id != snapshot.snapshot_id:
                            snapshot = current
                            self._update(identity, snapshot_json=json.dumps(snapshot.to_dict()))
                            analysis = assess_draft(
                                snapshot, output, request, revision=revision, draft_id=identity
                            )
                            context, requirements, issues = await self._checks(
                                identity,
                                settings,
                                request,
                                analysis,
                                usage,
                                limits,
                                retry_budget,
                            )
                    self._save_version(identity, revision, output, analysis, context, requirements)
                    earlier_semantic_issues: list[dict[str, Any]] = []
                    if context is None and requirements is None and feedback:
                        earlier_semantic_issues = [
                            *feedback.get("earlier_semantic_issues", []),
                            *[
                                {
                                    "from_revision": revision - 1,
                                    "criterion_id": issue["criterion_id"],
                                    "explanation": issue.get("explanation"),
                                    "repair_instructions": issue.get("repair_instructions"),
                                }
                                for issue in feedback.get("blocking_issues", [])
                                if issue["criterion_id"]
                                in {
                                    "context_support",
                                    "topic",
                                    "style",
                                    "coherence",
                                }
                            ],
                        ]
                    feedback = {
                        "constraints": request.constraints(),
                        "earlier_semantic_issues": earlier_semantic_issues,
                        "deterministic_report": analysis,
                        "context_review": context,
                        "requirements_review": requirements,
                        "blocking_issues": issues,
                        "remaining_budget": {
                            "draft_versions": self.max_versions - revision,
                            "model_requests": self.model_request_limit - usage.requests,
                        },
                    }
                    self._update(identity, feedback_json=json.dumps(feedback, ensure_ascii=False))
                    if not issues:
                        self._update(identity, status="saving")
                        try:
                            material = await asyncio.to_thread(
                                self.library.import_text,
                                title=output.title,
                                content="\n\n".join(p.text for p in output.paragraphs),
                                source="generated",
                                expected_generation=snapshot.storage_generation,
                                expected_profile_revision=snapshot.profile_revision,
                                generation_job_id=identity,
                            )
                        except MaterialError as error:
                            if self.library.get_profile()["storage_generation"] != (
                                snapshot.storage_generation
                            ):
                                raise JobStopped from error
                            feedback["blocking_issues"] = [
                                {
                                    "criterion_id": "profile_changed",
                                    "explanation": "Словарь изменился. Нужна новая проверка.",
                                }
                            ]
                            self._update(
                                identity,
                                status="revising",
                                feedback_json=json.dumps(feedback, ensure_ascii=False),
                            )
                        else:
                            self._update(
                                identity,
                                status="completed",
                                result_material_id=material["id"],
                                error=None,
                                usage_json=json.dumps(self._usage(usage)),
                            )
                            self._last_completed_id = identity
                            return
                    previous_draft = analysis["versioned_draft"]
                    self._update(
                        identity, status="revising", usage_json=json.dumps(self._usage(usage))
                    )
                self._update(
                    identity,
                    status="needs_changes",
                    error=f"Не удалось выполнить все условия за {self.max_versions} попыток.",
                )
        except (JobStopped, asyncio.CancelledError):
            self._fail(identity, "interrupted", "Генерация остановлена. Можно повторить.", usage)
        except TimeoutError:
            self._fail(
                identity, "needs_changes", "Истекло время создания текста. Можно повторить.", usage
            )
        except UsageLimitExceeded:
            self._fail(
                identity, "needs_changes", "Достигнут предел запросов AI. Можно повторить.", usage
            )
        except AIBudgetExceeded as error:
            self._fail(identity, "needs_changes", str(error.detail), usage)
        except Exception as error:
            logger.warning("text_generation_job=%s failed: %s", identity, type(error).__name__)
            message = (
                str(error.detail)
                if isinstance(error, HTTPException)
                else (
                    "AI или учебные инструменты не смогли завершить создание текста. "
                    "Можно повторить."
                )
            )
            self._fail(identity, "failed", message, usage)
        finally:
            self.tasks.pop(identity, None)
            self._remove_unsuccessful(identity)

    def _remove_unsuccessful(self, identity: str) -> None:
        with self.library.connect() as db:
            row = db.execute(
                "SELECT status,error,revision FROM text_generation_jobs WHERE id=?",
                (identity,),
            ).fetchone()
            if row and row["status"] not in ACTIVE and row["status"] != "completed":
                if row["status"] != "cancelled":
                    self._notifications["error"] = {
                        "id": identity,
                        "status": row["status"],
                        "revision": row["revision"],
                        "message": row["error"] or "Не удалось создать текст.",
                    }
                db.execute("DELETE FROM text_generation_jobs WHERE id=?", (identity,))

    @staticmethod
    def _usage(usage: RunUsage) -> dict[str, int]:
        return {
            "model_requests": usage.requests,
            "tool_calls": usage.tool_calls,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        }

    async def cancel(self, identity: str) -> dict[str, Any]:
        job = get_job(self.library, identity)
        if job["status"] in ACTIVE:
            with self.library.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT status FROM text_generation_jobs WHERE id=?",
                    (identity,),
                ).fetchone()
                if current is None:
                    raise HTTPException(404, "Задание больше недоступно.")
                if current["status"] in ACTIVE:
                    material = None
                    if "generation_job_id" in {
                        row["name"] for row in db.execute("PRAGMA table_info(materials)")
                    }:
                        material = db.execute(
                            "SELECT id FROM materials WHERE generation_job_id=?",
                            (identity,),
                        ).fetchone()
                    if material is not None:
                        db.execute(
                            "UPDATE text_generation_jobs SET status='completed',"
                            "result_material_id=?,error=NULL,updated_at=? WHERE id=?",
                            (material["id"], now(), identity),
                        )
                    else:
                        db.execute(
                            "UPDATE text_generation_jobs SET status='cancelled',error=?,"
                            "updated_at=? WHERE id=?",
                            ("Создание текста остановлено.", now(), identity),
                        )
            response = get_job(self.library, identity)
            task = self.tasks.get(identity)
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            self._remove_unsuccessful(identity)
            if response["status"] == "completed":
                self._last_completed_id = identity
            return response
        return job

    async def close(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def reset(self) -> None:
        await self.close()
        self._notifications.clear()
        self._last_completed_id = None
        with self.library.connect() as db:
            db.execute("DELETE FROM text_generation_drafts")
            db.execute("DELETE FROM text_generation_jobs")


def create_text_generation_router(service: TextGenerationService) -> APIRouter:
    router = APIRouter(prefix="/api/text-generation")

    @router.get("/settings")
    def settings() -> dict[str, Any]:
        return service.settings()

    @router.get("/jobs")
    def jobs(response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return {"jobs": service.list_jobs(), "error_event": service.take_error()}

    @router.post("/jobs", status_code=202)
    async def start(
        request: GenerationRequest,
        idempotency_key: str | None = Header(default=None, max_length=100),
    ) -> dict[str, Any]:
        return service.start(request, idempotency_key)

    @router.get("/jobs/{job_id}")
    def job(job_id: str) -> dict[str, Any]:
        return get_job(service.library, job_id)

    @router.delete("/jobs/{job_id}")
    async def cancel(job_id: str) -> dict[str, Any]:
        return await service.cancel(job_id)

    return router
