"""Public learning operations with persistent context caches and idempotent chat turns."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models import Model

from backend.app.agents.learning import PROMPT_VERSION, LearningRun, run_card, run_chat
from backend.app.ai_connection.service import ConnectionService
from backend.app.ai_connection.settings import AISettings
from backend.app.learning_context import LearningContext
from backend.app.materials import MaterialLibrary

RunOutput = TypeVar("RunOutput")


class SelectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    material_id: str = Field(min_length=1, max_length=100)
    cue_id: str = Field(min_length=1, max_length=100)
    token_id: str = Field(min_length=1, max_length=100)
    force_refresh: bool = False


class ChatInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(
        default_factory=lambda: str(uuid4()),
        min_length=8,
        max_length=100,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


def now() -> str:
    return datetime.now(UTC).isoformat()


def history_answer(response: dict[str, Any]) -> str:
    return json.dumps(
        {key: response.get(key) for key in ("answer", "educational_example", "citations")},
        ensure_ascii=False,
    )


class LearningService:
    def __init__(
        self,
        *,
        library: MaterialLibrary,
        connection: ConnectionService | None,
        mcp_url: str,
        model_factory: Callable[[], Model] | None = None,
    ):
        self.library = library
        self.context = LearningContext(library)
        self.connection = connection
        self.mcp_url = mcp_url
        self.model_factory = model_factory
        self.locks: dict[str, asyncio.Lock] = {}

    async def execute(
        self,
        operation: Callable[[Model], Awaitable[RunOutput]],
        *,
        settings: AISettings | None = None,
    ) -> RunOutput:
        if self.model_factory is None:
            if self.connection is None:
                raise HTTPException(409, "Сначала подключите подписку.")
            return await self.connection.execute(operation, settings=settings)
        # Explicit application construction injects controlled models only in automated tests.
        try:
            async with asyncio.timeout(30):
                return await operation(self.model_factory())
        except TimeoutError:
            raise HTTPException(504, "AI не ответил за 30 секунд. Повторите запрос.") from None
        except UsageLimitExceeded:
            raise HTTPException(422, "Достигнут предел трёх раундов инструментов.") from None

    def ensure_generation(self, profile: dict[str, Any], db: sqlite3.Connection) -> None:
        generation = db.execute(
            "SELECT value FROM storage_metadata WHERE key='generation'"
        ).fetchone()[0]
        if generation != profile.get("storage_generation"):
            raise HTTPException(409, "Учебные данные удалены. Откройте материал заново.")

    async def card(self, selection: SelectionInput) -> dict[str, Any]:
        started = time.monotonic()
        settings = self.connection.settings_store.read() if self.connection else AISettings(
            model="gpt-6-luna", reasoning_effort="low"
        )
        selected = self.context.selection(
            material_id=selection.material_id,
            cue_id=selection.cue_id,
            token_id=selection.token_id,
        )
        profile = self.library.get_profile()
        cache_key = hashlib.sha256(
            json.dumps(
                {
                    "selection": selected,
                    "profile": profile,
                    "prompt": PROMPT_VERSION,
                    "model": settings.model,
                    "reasoning_effort": settings.reasoning_effort,
                    "context": self.context.subtitle_context(
                        selection.material_id, selection.cue_id
                    ),
                },
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        lock = self.locks.setdefault("card:" + cache_key, asyncio.Lock())
        async with lock:
            with self.library.connect() as db:
                cached = db.execute(
                    "SELECT result_json FROM contextual_cards WHERE cache_key=?",
                    (cache_key,),
                ).fetchone()
            request_id = str(uuid4())
            if cached and not selection.force_refresh:
                result = json.loads(cached["result_json"])
                result.update(
                    request_id=request_id,
                    cached=True,
                    tools=[],
                    model_requests=0,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                return dict(result)
            deps = LearningRun(request_id=request_id, selection=selected, profile=profile)
            card, requests, model_name = await self.execute(
                lambda model: run_card(
                    model=model,
                    mcp_url=self.mcp_url,
                    deps=deps,
                    reasoning_effort=settings.reasoning_effort,
                ),
                settings=settings,
            )
            result = {
                "request_id": request_id,
                "model": model_name,
                "reasoning_effort": settings.reasoning_effort,
                "selection": selected,
                "card": card.model_dump(),
                "tools": [trace.model_dump() for trace in deps.traces],
                "model_requests": requests,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "cached": False,
            }
            with self.library.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self.ensure_generation(profile, db)
                db.execute(
                    "INSERT OR REPLACE INTO contextual_cards VALUES(?,?,?)",
                    (cache_key, json.dumps(result, ensure_ascii=False), now()),
                )
            return result

    def create_conversation(self, selection: SelectionInput) -> dict[str, Any]:
        identity = uuid4().hex
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.context.selection(**selection.model_dump(exclude={"force_refresh"}))
            db.execute(
                "INSERT INTO conversations VALUES(?,?,?,?,?)",
                (
                    identity,
                    selection.material_id,
                    selection.cue_id,
                    selection.token_id,
                    now(),
                ),
            )
        return self.conversation(identity)

    def list_conversations(self, material_id: str) -> list[dict[str, Any]]:
        with self.library.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM conversations WHERE material_id=? ORDER BY created_at DESC",
                    (material_id,),
                )
            ]

    def conversation(self, conversation_id: str) -> dict[str, Any]:
        with self.library.connect() as db:
            row = db.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if row is None:
                raise KeyError("Разговор не найден.")
            result = dict(row)
            result["messages"] = [
                {
                    "request_id": turn["request_id"],
                    "message": turn["user_text"],
                    "response": json.loads(turn["response_json"])
                    if turn["response_json"]
                    else None,
                    "created_at": turn["created_at"],
                }
                for turn in db.execute(
                    "SELECT * FROM learning_messages WHERE conversation_id=? ORDER BY created_at",
                    (conversation_id,),
                )
            ]
        return result

    async def message(self, conversation_id: str, incoming: ChatInput) -> dict[str, Any]:
        if not incoming.message.strip():
            raise HTTPException(422, "Введите сообщение.")
        settings = self.connection.settings_store.read() if self.connection else AISettings(
            model="gpt-6-luna", reasoning_effort="low"
        )
        lock = self.locks.setdefault("conversation:" + conversation_id, asyncio.Lock())
        async with lock:
            conversation = self.conversation(conversation_id)
            selected = self.context.selection(
                conversation["material_id"],
                conversation["cue_id"],
                conversation["token_id"],
            )
            profile = self.library.get_profile()
            with self.library.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self.ensure_generation(profile, db)
                existing = db.execute(
                    "SELECT * FROM learning_messages WHERE conversation_id=? AND request_id=?",
                    (conversation_id, incoming.request_id),
                ).fetchone()
                if existing:
                    if existing["user_text"] != incoming.message:
                        raise HTTPException(
                            409, "Этот request_id уже относится к другому сообщению."
                        )
                    if existing["response_json"]:
                        return dict(json.loads(existing["response_json"]))
                else:
                    db.execute(
                        "INSERT INTO learning_messages VALUES(?,?,?,?,?)",
                        (
                            conversation_id,
                            incoming.request_id,
                            incoming.message,
                            None,
                            now(),
                        ),
                    )
            # Rebuild a bounded history of successful turns. No old tool results or hidden secrets.
            history: list[ModelMessage] = []
            characters = 0
            recent: list[dict[str, Any]] = []
            for turn in reversed(conversation["messages"]):
                if not turn["response"]:
                    continue
                cost = len(turn["message"]) + len(history_answer(turn["response"]))
                if len(recent) >= 6 or characters + cost > 12000:
                    break
                recent.append(turn)
                characters += cost
            for turn in reversed(recent):
                history.extend(
                    [
                        ModelRequest(parts=[UserPromptPart(turn["message"])]),
                        ModelResponse(parts=[TextPart(history_answer(turn["response"]))]),
                    ]
                )
            started = time.monotonic()
            deps = LearningRun(
                request_id=incoming.request_id,
                selection=selected,
                profile=profile,
                conversation_id=conversation_id,
            )
            answer, requests, model_name = await self.execute(
                lambda model: run_chat(
                    model=model,
                    mcp_url=self.mcp_url,
                    deps=deps,
                    message=incoming.message,
                    history=history,
                    reasoning_effort=settings.reasoning_effort,
                ),
                settings=settings,
            )
            citations = self.context.verify_sources(
                [deps.sources[key] for key in answer.citation_ids]
            )
            answer_text = answer.answer
            if deps.searches and not any(deps.searches):
                answer_text += "\nВ сохранённых материалах точных совпадений не найдено."
            result = {
                "request_id": incoming.request_id,
                "model": model_name,
                "reasoning_effort": settings.reasoning_effort,
                "conversation_id": conversation_id,
                "answer": answer_text,
                "educational_example": answer.educational_example,
                "citations": citations,
                "tools": [trace.model_dump() for trace in deps.traces],
                "model_requests": requests,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
            with self.library.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self.ensure_generation(profile, db)
                db.execute(
                    "UPDATE learning_messages SET response_json=? "
                    "WHERE conversation_id=? AND request_id=?",
                    (json.dumps(result, ensure_ascii=False), conversation_id, incoming.request_id),
                )
            return result


def create_learning_router(
    *,
    library: MaterialLibrary,
    connection: ConnectionService | None,
    mcp_url: str,
    model_factory: Callable[[], Model] | None = None,
) -> APIRouter:
    service = LearningService(
        library=library, connection=connection, mcp_url=mcp_url, model_factory=model_factory
    )
    router = APIRouter(prefix="/api", tags=["Learning"])

    @router.post("/cards")
    async def card(selection: SelectionInput) -> dict[str, Any]:
        try:
            async with asyncio.timeout(30):
                return await service.card(selection)
        except TimeoutError:
            raise HTTPException(504, "AI не ответил за 30 секунд. Повторите запрос.") from None

    @router.post("/conversations", status_code=201)
    def create_conversation(selection: SelectionInput) -> dict[str, Any]:
        return service.create_conversation(selection)

    @router.get("/conversations")
    def conversations(material_id: str) -> list[dict[str, Any]]:
        return service.list_conversations(material_id)

    @router.get("/conversations/{conversation_id}")
    def conversation(conversation_id: str) -> dict[str, Any]:
        return service.conversation(conversation_id)

    @router.post("/conversations/{conversation_id}/messages")
    async def message(conversation_id: str, incoming: ChatInput) -> dict[str, Any]:
        try:
            async with asyncio.timeout(30):
                return await service.message(conversation_id, incoming)
        except TimeoutError:
            raise HTTPException(504, "AI не ответил за 30 секунд. Повторите запрос.") from None

    return router
