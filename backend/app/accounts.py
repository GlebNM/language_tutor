"""Local accounts, revocable sessions, and private learner application dispatch."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import shutil
import sqlite3
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import Headers
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.app.local_access import LocalAccessMiddleware
from backend.app.tenant_context import current_mcp_token

COOKIE = "lt_session"
SESSION_SECONDS = 30 * 24 * 3600


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password: str, salt: str) -> str:
    return hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=3, maxmem=64 * 1024**2
    ).hex()


class AccountInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    username: str = Field(pattern=r"^[A-Za-z0-9_.-]{3,32}$")
    password: str = Field(min_length=12, max_length=128)
    adopt_existing: bool = False


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class PasswordInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class AccountStore:
    def __init__(self, directory: Path, legacy_path: Path | None = None):
        self.directory = directory
        self.legacy_path = legacy_path or directory / "learning.sqlite3"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "accounts.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    salt TEXT NOT NULL, password_hash TEXT NOT NULL,
                    mcp_token TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(id)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, expires_at REAL NOT NULL
                );
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def learner_directory(self, account_id: str) -> Path:
        # IDs originate exclusively from the account registry, never a request path.
        if len(account_id) != 32 or any(c not in "0123456789abcdef" for c in account_id):
            raise ValueError("Invalid account ID")
        return self.directory / "users" / account_id

    def setup(self) -> dict[str, bool]:
        with self.connect() as db:
            first = db.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is None
        return {"first_account": first, "legacy_profile": first and self.legacy_path.exists()}

    def throttle(self, address: str, username: str) -> None:
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE expires_at <= ?", (now,))
            buckets = [("ip:" + address, 30), ("name:" + digest(username.lower()), 10)]
            for bucket, limit in buckets:
                row = db.execute("SELECT count FROM attempts WHERE bucket=?", (bucket,)).fetchone()
                if row and row["count"] >= limit:
                    raise HTTPException(429, "Слишком много попыток. Повторите через 5 минут.")
            for bucket, _ in buckets:
                db.execute(
                    "INSERT INTO attempts VALUES (?,1,?) ON CONFLICT(bucket) "
                    "DO UPDATE SET count=count+1",
                    (bucket, now + 300),
                )

    def register(self, incoming: AccountInput) -> dict[str, Any]:
        salt = secrets.token_hex(16)
        hashed = password_hash(incoming.password, salt)
        account_id = uuid4().hex
        directory = self.learner_directory(account_id)
        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute(
                    "SELECT 1 FROM accounts WHERE username=?", (incoming.username,)
                ).fetchone():
                    raise HTTPException(409, "Это имя пользователя уже занято.")
                first = db.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is None
                legacy = first and self.legacy_path.exists()
                if legacy and not incoming.adopt_existing:
                    raise HTTPException(409, "Подтвердите перенос существующего учебного профиля.")
                if incoming.adopt_existing and not legacy:
                    raise HTTPException(409, "Перенос доступен только первой учётной записи.")
                directory.mkdir(parents=True, mode=0o700)
                if legacy:
                    # SQLite backup includes committed WAL data; originals remain for recovery.
                    source = sqlite3.connect(self.legacy_path)
                    target = sqlite3.connect(directory / "learning.sqlite3")
                    try:
                        source.backup(target)
                    finally:
                        source.close()
                        target.close()
                    for name in ("credentials", "ai-settings.json"):
                        source_path = self.directory / name
                        if source_path.is_dir():
                            shutil.copytree(source_path, directory / name)
                        elif source_path.exists():
                            shutil.copy2(source_path, directory / name)
                db.execute(
                    "INSERT INTO accounts VALUES (?,?,?,?,?)",
                    (
                        account_id,
                        incoming.username,
                        salt,
                        hashed,
                        secrets.token_urlsafe(32),
                    ),
                )
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return {"id": account_id, "username": incoming.username, "verified_hash": hashed}

    def login(self, incoming: LoginInput) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM accounts WHERE username=?", (incoming.username,)
            ).fetchone()
        # Perform the same expensive work for unknown names.
        hashed = password_hash(incoming.password, row["salt"] if row else "00" * 16)
        if row is None or not hmac.compare_digest(hashed, row["password_hash"]):
            raise HTTPException(401, "Неверное имя пользователя или пароль.")
        return {"id": row["id"], "username": row["username"], "verified_hash": hashed}

    def session(self, token: str) -> dict[str, Any] | None:
        if not token or len(token) > 100:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT accounts.*,sessions.token_hash FROM sessions JOIN accounts "
                "ON accounts.id=sessions.account_id WHERE token_hash=? AND expires_at>?",
                (digest(token), time.time()),
            ).fetchone()
        return dict(row) if row else None

    def new_session(self, account_id: str, old_token: str, verified_hash: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT password_hash FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            if row is None or not hmac.compare_digest(row["password_hash"], verified_hash):
                raise HTTPException(401, "Пароль изменился. Войдите заново.")
            db.execute(
                "DELETE FROM sessions WHERE expires_at<=? OR token_hash=?",
                (time.time(), digest(old_token)),
            )
            db.execute(
                "INSERT INTO sessions VALUES (?,?,?)",
                (digest(token), account_id, time.time() + SESSION_SECONDS),
            )
        return token

    def revoke(self, token: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(token),))

    def change_password(self, account_id: str, incoming: PasswordInput) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not hmac.compare_digest(
                password_hash(incoming.current_password, row["salt"]), row["password_hash"]
            ):
                raise HTTPException(401, "Текущий пароль неверен.")
            salt = secrets.token_hex(16)
            db.execute(
                "UPDATE accounts SET salt=?,password_hash=? WHERE id=?",
                (salt, password_hash(incoming.new_password, salt), account_id),
            )
            db.execute("DELETE FROM sessions WHERE account_id=?", (account_id,))


class LearnerApps:
    def __init__(self, store: AccountStore, *, seed_demo: bool, include_ai: bool):
        self.store = store
        self.seed_demo = seed_demo
        self.include_ai = include_ai
        self.apps: dict[str, FastAPI] = {}
        self.lock = asyncio.Lock()
        self.lifespans = AsyncExitStack()

    async def get(self, account_id: str) -> FastAPI:
        async with self.lock:
            if account_id not in self.apps:
                from backend.app.main import create_learner_app

                learner = await asyncio.to_thread(
                    create_learner_app,
                    self.store.learner_directory(account_id) / "learning.sqlite3",
                    seed_demo=self.seed_demo,
                    include_ai=self.include_ai,
                )
                await self.lifespans.enter_async_context(learner.router.lifespan_context(learner))
                self.apps[account_id] = learner
            return self.apps[account_id]

    async def close(self) -> None:
        await self.lifespans.aclose()
        self.apps.clear()


class AccountDispatch:
    def __init__(self, app: ASGIApp, store: AccountStore, learners: LearnerApps):
        self.app, self.store, self.learners = app, store, learners

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return

        async def private_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"
                ] + [(b"cache-control", b"no-store")]
            await send(message)

        if scope["path"].startswith("/api/auth/") or scope["path"] == "/api/health":
            await self.app(scope, receive, private_send)
            return
        request = Request(scope)
        account = await asyncio.to_thread(self.store.session, request.cookies.get(COOKIE, ""))
        if account is None or not hmac.compare_digest(
            Headers(scope=scope).get("x-session-key", ""), account["token_hash"]
        ):
            await JSONResponse({"detail": "Войдите в приложение заново."}, status_code=401)(
                scope,
                receive,
                private_send,
            )
            return
        token = current_mcp_token.set(account["mcp_token"])
        try:
            learner = await self.learners.get(account["id"])
            await learner(scope, receive, private_send)
        finally:
            current_mcp_token.reset(token)


def create_account_app(
    database_path: Path | None = None,
    *,
    seed_demo: bool = True,
    frontend_dist: Path | None = None,
    include_ai: bool = True,
) -> FastAPI:
    path = database_path or Path(os.getenv("DATA_DIR", "data/local")) / "learning.sqlite3"
    store = AccountStore(path.parent, path)
    learners = LearnerApps(store, seed_demo=seed_demo, include_ai=include_ai)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await learners.close()

    app = FastAPI(title="Language Tutor", lifespan=lifespan)
    app.state.accounts, app.state.learners = store, learners
    app.add_middleware(AccountDispatch, store=store, learners=learners)
    app.add_middleware(
        LocalAccessMiddleware,
        allowed_origins=[
            value.strip()
            for value in os.getenv(
                "ALLOWED_ORIGINS", "http://localhost:8765,http://127.0.0.1:8765"
            ).split(",")
            if value.strip()
        ],
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])

    def signed_in(request: Request) -> dict[str, Any]:
        account = store.session(request.cookies.get(COOKIE, ""))
        if account is None or not hmac.compare_digest(
            request.headers.get("x-session-key", ""), account["token_hash"]
        ):
            raise HTTPException(401, "Войдите в приложение заново.")
        return account

    def start_session(user: dict[str, Any], request: Request, response: Response) -> dict[str, Any]:
        token = store.new_session(
            user["id"], request.cookies.get(COOKIE, ""), user["verified_hash"]
        )
        response.set_cookie(
            COOKIE,
            token,
            max_age=SESSION_SECONDS,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )
        return {
            "user": {"id": user["id"], "username": user["username"]},
            "session_key": digest(token),
        }

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {
                "detail": (
                    "Проверьте данные: имя — 3–32 латинских буквы, цифры, точка, дефис или _; "
                    "новый пароль — от 12 до 128 символов."
                )
            },
            status_code=422,
        )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/auth/session")
    def session(request: Request) -> dict[str, Any]:
        account = store.session(request.cookies.get(COOKIE, ""))
        return {
            "user": {"id": account["id"], "username": account["username"]} if account else None,
            "session_key": account["token_hash"] if account else None,
            **store.setup(),
        }

    @app.post("/api/auth/register", status_code=201)
    def register(incoming: AccountInput, request: Request, response: Response) -> dict[str, Any]:
        store.throttle(request.client.host if request.client else "local", incoming.username)
        return start_session(store.register(incoming), request, response)

    @app.post("/api/auth/login")
    def login(incoming: LoginInput, request: Request, response: Response) -> dict[str, Any]:
        store.throttle(request.client.host if request.client else "local", incoming.username)
        return start_session(store.login(incoming), request, response)

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response) -> dict[str, str]:
        signed_in(request)
        store.revoke(request.cookies.get(COOKIE, ""))
        response.delete_cookie(COOKIE, path="/")
        return {"status": "signed_out"}

    @app.post("/api/auth/password")
    def change_password(
        incoming: PasswordInput, request: Request, response: Response
    ) -> dict[str, str]:
        account = signed_in(request)
        store.throttle(request.client.host if request.client else "local", account["username"])
        store.change_password(account["id"], incoming)
        response.delete_cookie(COOKIE, path="/")
        return {"status": "signed_out"}

    distribution = frontend_dist or Path(__file__).resolve().parents[2] / "apps/web/dist"
    if distribution.is_dir():
        app.mount("/", StaticFiles(directory=distribution, html=True), name="web")
    return app
