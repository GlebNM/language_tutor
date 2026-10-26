"""Public learning operations with persistent context caches and idempotent chat turns."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Literal, TypeVar
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models import Model

from backend.app.activity import ActivityService
from backend.app.agents.learning import PROMPT_VERSION, LearningRun, run_card, run_chat
from backend.app.ai_connection.service import ConnectionService
from backend.app.ai_connection.settings import AISettings
from backend.app.conversation_history import prepare_history, turn_history
from backend.app.learning_context import LearningContext
from backend.app.materials import MaterialLibrary
from backend.app.message_words import MessageWords

RunOutput = TypeVar("RunOutput")


class SelectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    material_id: str = Field(min_length=1, max_length=100)
    cue_id: str = Field(min_length=1, max_length=100)
    token_id: str = Field(min_length=1, max_length=100)
    force_refresh: bool = False


class MessageSelectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: Literal["message"]
    conversation_id: str = Field(min_length=1, max_length=100)
    message_id: str = Field(min_length=1, max_length=100)
    token_id: str = Field(min_length=1, max_length=100)
    force_refresh: bool = False


class MessageLexemeCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    lemma: str = Field(min_length=1, max_length=100)
    part_of_speech: str = Field(min_length=1, max_length=20)


class ConversationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(default="Новый разговор", min_length=1, max_length=120)
    material_ids: list[str] = Field(default_factory=list, max_length=20)
    material_id: str | None = Field(default=None, min_length=1, max_length=100)
    cue_id: str | None = Field(default=None, min_length=1, max_length=100)
    token_id: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def complete_selection(self) -> ConversationInput:
        if (self.cue_id or self.token_id) and not all(
            (self.material_id, self.cue_id, self.token_id)
        ):
            raise ValueError("Выберите материал, реплику и слово вместе.")
        return self


class ConversationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str | None = Field(default=None, min_length=1, max_length=120)
    material_ids: list[str] | None = Field(default=None, max_length=20)


class ChatInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(
        default_factory=lambda: str(uuid4()),
        min_length=8,
        max_length=100,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class TopicInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
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
        self.activity = ActivityService(library)
        self.connection = connection
        self.mcp_url = mcp_url
        self.model_factory = model_factory
        self.locks: dict[str, asyncio.Lock] = {}
        self.active: dict[tuple[str, str], tuple[str, bool, asyncio.Task[dict[str, Any]]]] = {}

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

    async def card(self, selection: SelectionInput | MessageSelectionInput) -> dict[str, Any]:
        started = time.monotonic()
        settings = (
            self.connection.settings_store.read()
            if self.connection
            else AISettings(model="gpt-6-luna", reasoning_effort="low")
        )
        if isinstance(selection, MessageSelectionInput):
            selected = MessageWords(self.library).selection(
                selection.conversation_id,
                selection.message_id,
                selection.token_id,
            )
            context = {"text": selected["text"], "message_id": selection.message_id}
        else:
            selected = self.context.selection(
                material_id=selection.material_id,
                cue_id=selection.cue_id,
                token_id=selection.token_id,
            )
            context = self.context.subtitle_context(selection.material_id, selection.cue_id)
        profile = self.library.get_profile()
        cache_key = hashlib.sha256(
            json.dumps(
                {
                    "selection": selected,
                    "profile": profile,
                    "prompt": PROMPT_VERSION,
                    "model": settings.model,
                    "reasoning_effort": settings.reasoning_effort,
                    "context": context,
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
                with self.library.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    self.ensure_generation(profile, db)
                    self.record_translation(db, selected, result)
                return dict(result)
            deps = LearningRun(
                request_id=request_id,
                selection=selected,
                profile=profile,
                conversation_id=selected.get("conversation_id"),
            )
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
                self.record_translation(db, selected, result)
            return result

    def record_translation(
        self, db: sqlite3.Connection, selected: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if selected.get("source") == "message":
            MessageWords(self.library).selection(
                selected["conversation_id"],
                selected["message_id"],
                selected["token_id"],
            )
            self.activity.record(
                db,
                "translation_received",
                lexeme_id=selected["lexeme_id"],
                conversation_id=selected["conversation_id"],
                message_id=selected["message_id"],
                message_token_id=selected["token_id"],
                request_id=result["request_id"],
                data={key: result[key] for key in ("card", "model", "reasoning_effort", "cached")},
            )
            return
        self.activity.record(
            db,
            "translation_received",
            lexeme_id=selected["lexeme_id"],
            material_id=selected["material_id"],
            cue_id=selected["cue_id"],
            token_id=selected["token_id"],
            request_id=result["request_id"],
            data={key: result[key] for key in ("card", "model", "reasoning_effort", "cached")},
        )

    def create_conversation(self, selection: ConversationInput | SelectionInput) -> dict[str, Any]:
        if isinstance(selection, SelectionInput):
            selection = ConversationInput(**selection.model_dump(exclude={"force_refresh"}))
        identity = uuid4().hex
        if not selection.title.strip():
            raise HTTPException(422, "Введите название разговора.")
        material_ids = set(selection.material_ids)
        if selection.material_id:
            material_ids.add(selection.material_id)
        for material_id in material_ids:
            self.library.get_material(material_id)
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if selection.cue_id and selection.token_id and selection.material_id:
                self.context.selection(selection.material_id, selection.cue_id, selection.token_id)
            elif selection.material_id:
                self.library.get_material(selection.material_id)
            db.execute(
                "INSERT INTO conversations(id,material_id,cue_id,token_id,created_at,title) "
                "VALUES(?,?,?,?,?,?)",
                (
                    identity,
                    selection.material_id,
                    selection.cue_id,
                    selection.token_id,
                    now(),
                    selection.title.strip(),
                ),
            )
            db.executemany(
                "INSERT INTO conversation_materials VALUES(?,?)",
                [(identity, material_id) for material_id in sorted(material_ids)],
            )
        return self.conversation(identity)

    def list_conversations(self, material_id: str | None = None) -> list[dict[str, Any]]:
        with self.library.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM conversations "
                    + ("WHERE material_id=? " if material_id else "")
                    + "ORDER BY created_at DESC",
                    (material_id,) if material_id else (),
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
            result["material_ids"] = [
                row[0]
                for row in db.execute(
                    "SELECT material_id FROM conversation_materials WHERE conversation_id=? "
                    "ORDER BY material_id",
                    (conversation_id,),
                )
            ]
            result["messages"] = [
                {
                    "request_id": turn["request_id"],
                    "message": turn["user_text"],
                    "response": json.loads(turn["response_json"])
                    if turn["response_json"]
                    else None,
                    "created_at": turn["created_at"],
                    "history_json": turn["history_json"],
                    "status": turn["status"],
                    "tokens": MessageWords(self.library).words(
                        conversation_id,
                        turn["request_id"],
                    )["tokens"]
                    if turn["status"] == "complete" and turn["response_json"]
                    else [],
                }
                for turn in db.execute(
                    "SELECT * FROM learning_messages WHERE conversation_id=? ORDER BY created_at",
                    (conversation_id,),
                )
            ]
        return result

    def update_conversation(
        self, conversation_id: str, incoming: ConversationUpdate
    ) -> dict[str, Any]:
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "SELECT 1 FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone():
                raise KeyError("Разговор не найден.")
            if incoming.title is not None:
                if not incoming.title.strip():
                    raise HTTPException(422, "Введите название разговора.")
                db.execute(
                    "UPDATE conversations SET title=? WHERE id=?",
                    (incoming.title.strip(), conversation_id),
                )
            if incoming.material_ids is not None:
                for material_id in incoming.material_ids:
                    self.library.get_material(material_id)
                db.execute(
                    "DELETE FROM conversation_materials WHERE conversation_id=?", (conversation_id,)
                )
                db.executemany(
                    "INSERT INTO conversation_materials VALUES(?,?)",
                    [
                        (conversation_id, material_id)
                        for material_id in sorted(set(incoming.material_ids))
                    ],
                )
        return self.conversation(conversation_id)

    async def delete_conversation(self, conversation_id: str) -> dict[str, str]:
        for (identity, _), (_, _, task) in list(self.active.items()):
            if identity == conversation_id:
                task.cancel()
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "SELECT 1 FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone():
                raise KeyError("Разговор не найден.")
            db.execute(
                "DELETE FROM learning_activity WHERE "
                "json_extract(data_json,'$.conversation_id')=? OR conversation_id=?",
                (conversation_id, conversation_id),
            )
            db.execute(
                "DELETE FROM contextual_cards WHERE "
                "json_extract(result_json,'$.selection.conversation_id')=?",
                (conversation_id,),
            )
            db.execute("DELETE FROM learning_messages WHERE conversation_id=?", (conversation_id,))
            db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
        return {"status": "deleted"}

    def set_turn_status(self, identity: str, request_id: str, status: str) -> None:
        with self.library.connect() as db:
            db.execute(
                "UPDATE learning_messages SET status=? WHERE conversation_id=? "
                "AND request_id=? AND response_json IS NULL",
                (status, identity, request_id),
            )

    async def stop(self, identity: str, request_id: str) -> dict[str, str]:
        self.conversation(identity)
        running = self.active.get((identity, request_id))
        if running and not running[2].done():
            running[2].cancel()
            with suppress(asyncio.CancelledError):
                await running[2]
            self.set_turn_status(identity, request_id, "cancelled")
            return {"status": "cancelled"}
        with self.library.connect() as db:
            row = db.execute(
                "SELECT status FROM learning_messages WHERE conversation_id=? AND request_id=?",
                (identity, request_id),
            ).fetchone()
        if row is None:
            raise KeyError("Сообщение не найдено.")
        return {"status": row["status"]}

    async def message(
        self,
        conversation_id: str,
        incoming: ChatInput,
        *,
        topics: bool = False,
        on_text: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        key = (conversation_id, incoming.request_id)
        running = self.active.get(key)
        owner = running is None
        if running:
            if (running[0], running[1]) != (incoming.message, topics):
                raise HTTPException(409, "Этот request_id уже относится к другому сообщению.")
            task = running[2]
        else:
            settings = (
                self.connection.settings_store.read()
                if self.connection
                else AISettings(
                    model="gpt-6-luna",
                    reasoning_effort="low",
                )
            )
            task = asyncio.create_task(
                self._message(
                    conversation_id,
                    incoming,
                    topics=topics,
                    on_text=on_text,
                    settings=settings,
                )
            )
            self.active[key] = (incoming.message, topics, task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self.set_turn_status(conversation_id, incoming.request_id, "cancelled")
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            raise HTTPException(409, "Генерация остановлена. Можно повторить запрос.") from None
        except Exception:
            self.set_turn_status(conversation_id, incoming.request_id, "failed")
            raise
        finally:
            if owner and self.active.get(key, (None, None, None))[2] is task:
                self.active.pop(key, None)

    async def _message(
        self,
        conversation_id: str,
        incoming: ChatInput,
        *,
        topics: bool = False,
        on_text: Callable[[str], Awaitable[None]] | None = None,
        settings: AISettings,
    ) -> dict[str, Any]:
        if not incoming.message.strip():
            raise HTTPException(422, "Введите сообщение.")
        lock = self.locks.setdefault("conversation:" + conversation_id, asyncio.Lock())
        async with lock:
            conversation = self.conversation(conversation_id)
            selected = (
                self.context.selection(
                    conversation["material_id"],
                    conversation["cue_id"],
                    conversation["token_id"],
                )
                if conversation["token_id"]
                else {}
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
                        "INSERT INTO learning_messages(conversation_id,request_id,"
                        "user_text,response_json,"
                        "created_at) VALUES(?,?,?,?,?)",
                        (
                            conversation_id,
                            incoming.request_id,
                            incoming.message,
                            None,
                            now(),
                        ),
                    )
                    self.activity.record(
                        db,
                        "message_sent",
                        lexeme_id=selected.get("lexeme_id"),
                        material_id=selected.get("material_id"),
                        cue_id=selected.get("cue_id"),
                        token_id=selected.get("token_id"),
                        request_id=incoming.request_id,
                        conversation_id=conversation_id,
                        data={"conversation_id": conversation_id},
                    )
            self.set_turn_status(conversation_id, incoming.request_id, "pending")
            started = time.monotonic()
            deps = LearningRun(
                request_id=incoming.request_id,
                selection=selected,
                profile=profile,
                conversation_id=conversation_id,
            )
            prepared: dict[str, Any] = {}

            async def respond(model: Model) -> Any:
                history, summary, covered, summary_requests = await prepare_history(
                    model,
                    conversation,
                    settings.reasoning_effort,
                )
                prepared.update(summary=summary, covered=covered)
                answer, requests, model_name = await run_chat(
                    model=model,
                    mcp_url=self.mcp_url,
                    deps=deps,
                    message=incoming.message,
                    history=history,
                    summary=summary,
                    request_limit=4 - summary_requests,
                    topics=topics,
                    on_text=on_text,
                    reasoning_effort=settings.reasoning_effort,
                )
                return answer, requests + summary_requests, model_name

            answer, requests, model_name = await self.execute(respond, settings=settings)
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
                "message_id": incoming.request_id,
                "answer": answer_text,
                "educational_example": answer.educational_example,
                "correction": answer.correction,
                "suggested_topics": answer.suggested_topics,
                "citations": citations,
                "tools": [trace.model_dump() for trace in deps.traces],
                "model_requests": requests,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
            with self.library.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self.ensure_generation(profile, db)
                if not db.execute(
                    "SELECT 1 FROM conversations WHERE id=?", (conversation_id,)
                ).fetchone():
                    raise HTTPException(409, "Разговор удалён.")
                db.execute(
                    "UPDATE learning_messages SET response_json=?,history_json=?,status='complete' "
                    "WHERE conversation_id=? AND request_id=?",
                    (
                        json.dumps(result, ensure_ascii=False),
                        ModelMessagesTypeAdapter.dump_json(
                            turn_history(incoming.message, result)
                        ).decode(),
                        conversation_id,
                        incoming.request_id,
                    ),
                )
                MessageWords(self.library).index(
                    db, conversation_id, incoming.request_id, answer_text
                )
                if prepared:
                    db.execute(
                        "UPDATE conversations SET summary=?,summary_count=? WHERE id=?",
                        (prepared["summary"], prepared["covered"], conversation_id),
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
    async def card(selection: SelectionInput | MessageSelectionInput) -> dict[str, Any]:
        try:
            async with asyncio.timeout(30):
                return await service.card(selection)
        except TimeoutError:
            raise HTTPException(504, "AI не ответил за 30 секунд. Повторите запрос.") from None

    @router.post("/conversations", status_code=201)
    def create_conversation(selection: ConversationInput) -> dict[str, Any]:
        return service.create_conversation(selection)

    @router.get("/conversations")
    def conversations(material_id: str | None = None) -> list[dict[str, Any]]:
        return service.list_conversations(material_id)

    @router.get("/conversations/{conversation_id}")
    def conversation(conversation_id: str) -> dict[str, Any]:
        return service.conversation(conversation_id)

    @router.patch("/conversations/{conversation_id}")
    def update_conversation(conversation_id: str, incoming: ConversationUpdate) -> dict[str, Any]:
        return service.update_conversation(conversation_id, incoming)

    @router.delete("/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str) -> dict[str, str]:
        return await service.delete_conversation(conversation_id)

    @router.put("/conversations/{conversation_id}/messages/{message_id}/words/{token_id}/lexeme")
    def correct_message_word(
        conversation_id: str,
        message_id: str,
        token_id: str,
        incoming: MessageLexemeCorrection,
    ) -> dict[str, Any]:
        return MessageWords(library).correct(
            conversation_id,
            message_id,
            token_id,
            incoming.lemma,
            incoming.part_of_speech,
        )

    @router.get("/conversations/{conversation_id}/messages/{message_id}/words")
    def message_words(conversation_id: str, message_id: str) -> dict[str, Any]:
        return MessageWords(library).words(conversation_id, message_id)

    @router.post(
        "/conversations/{conversation_id}/messages/{message_id}/words/{token_id}/selection"
    )
    def select_message_word(
        conversation_id: str,
        message_id: str,
        token_id: str,
        incoming: TopicInput,
    ) -> dict[str, Any]:
        selected = MessageWords(library).selection(conversation_id, message_id, token_id)
        with library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            key = "selection:" + incoming.request_id
            existing = db.execute(
                "SELECT * FROM learning_activity WHERE deduplication_key=?", (key,)
            ).fetchone()
            if existing:
                if (
                    existing["conversation_id"],
                    existing["message_id"],
                    existing["message_token_id"],
                ) != (
                    conversation_id,
                    message_id,
                    token_id,
                ):
                    raise HTTPException(409, "Этот request_id относится к другому выбору.")
                selected["activity_id"] = existing["id"]
            else:
                selected["activity_id"] = service.activity.record(
                    db,
                    "word_selected",
                    lexeme_id=selected["lexeme_id"],
                    conversation_id=conversation_id,
                    message_id=message_id,
                    message_token_id=token_id,
                    request_id=incoming.request_id,
                    deduplication_key=key,
                )
        return selected

    @router.post("/conversations/{conversation_id}/messages/{request_id}/cancel")
    async def stop(conversation_id: str, request_id: str) -> dict[str, str]:
        return await service.stop(conversation_id, request_id)

    @router.post("/conversations/{conversation_id}/messages/stream")
    async def stream(conversation_id: str, incoming: ChatInput) -> StreamingResponse:
        service.conversation(conversation_id)

        async def events() -> AsyncIterator[str]:
            queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

            async def text(value: str) -> None:
                await queue.put({"type": "text", "text": value})

            async def run() -> None:
                try:
                    async with asyncio.timeout(30):
                        result = await service.message(conversation_id, incoming, on_text=text)
                    await queue.put({"type": "complete", "response": result})
                except HTTPException as exc:
                    await queue.put(
                        {"type": "error", "status_code": exc.status_code, "detail": exc.detail}
                    )
                except TimeoutError:
                    await queue.put(
                        {
                            "type": "error",
                            "status_code": 504,
                            "detail": "AI не ответил за 30 секунд. Повторите запрос.",
                        }
                    )
                except asyncio.CancelledError:
                    await queue.put({"type": "cancelled"})
                    raise
                except Exception:
                    await queue.put(
                        {
                            "type": "error",
                            "status_code": 502,
                            "detail": "Не удалось получить ответ AI. Повторите запрос.",
                        }
                    )
                finally:
                    await queue.put(None)

            task = asyncio.create_task(run())
            try:
                yield 'data: {"type":"status","text":"Агент отвечает…"}\n\n'
                while (event := await queue.get()) is not None:
                    yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post("/conversations/{conversation_id}/topics")
    async def topics(conversation_id: str, incoming: TopicInput) -> dict[str, Any]:
        try:
            async with asyncio.timeout(30):
                return await service.message(
                    conversation_id,
                    ChatInput(
                        message="Предложи 2–3 темы для беседы.",
                        request_id=incoming.request_id,
                    ),
                    topics=True,
                )
        except TimeoutError:
            raise HTTPException(504, "AI не ответил за 30 секунд. Повторите запрос.") from None

    @router.post("/conversations/{conversation_id}/messages")
    async def message(conversation_id: str, incoming: ChatInput) -> dict[str, Any]:
        try:
            async with asyncio.timeout(30):
                return await service.message(conversation_id, incoming)
        except TimeoutError:
            raise HTTPException(504, "AI не ответил за 30 секунд. Повторите запрос.") from None

    return router
