"""Public multi-user workflows and ownership boundaries (F19 / A44–A48)."""

import asyncio
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from fastmcp import Client
from fastmcp.exceptions import ToolError

from backend.app.accounts import COOKIE, AccountStore
from backend.app.ai_connection.settings import AISettings
from backend.app.main import create_app, create_learner_app
from backend.app.tenant_context import current_mcp_token

PASSWORD = "a-long-test-password"


def register(client, username, **extra):
    response = client.post(
        "/api/auth/register",
        json={
            "username": username,
            "password": PASSWORD,
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    session = response.json()
    client.headers["X-Session-Key"] = session["session_key"]
    return session


def login(client, username, password=PASSWORD):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    client.headers["X-Session-Key"] = response.json()["session_key"]
    return response.json()


def material(client, text="She reads a book."):
    response = client.post(
        "/api/materials",
        json={
            "youtube_url": "https://youtu.be/TLkA0RELQ1g",
            "title": text,
            "format": "srt",
            "content": f"1\n00:00:00,000 --> 00:00:03,000\n{text}\n",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_all_learning_routes_require_session_and_csrf_binding(tmp_path):
    app = create_app(tmp_path / "learning.sqlite3", seed_demo=False)
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/auth/session").json()["user"] is None
        for path in [
            "profile",
            "materials",
            "dictionary",
            "conversations",
            "activity",
            "progress",
            "ai/connection",
            "ai/settings",
            "diagnostics",
            "viewing/settings",
            "discovery/jobs",
            "materials/fake/improvement",
        ]:
            assert client.get("/api/" + path).status_code == 401
            assert client.post("/api/" + path, json={}).status_code == 401
        created = register(client, "Alice")
        assert client.get("/api/profile").status_code == 200
        client.headers.pop("X-Session-Key")
        assert client.get("/api/profile").status_code == 401
        client.headers["X-Session-Key"] = created["session_key"]
        assert (
            client.get("/api/materials", headers={"Origin": "https://evil.example"}).status_code
            == 403
        )
        assert client.post("/api/auth/logout", content="").status_code == 415
        assert client.get("/api/profile").headers["cache-control"] == "no-store"


def test_login_validation_cookies_revocation_expiry_and_restart(tmp_path):
    path = tmp_path / "learning.sqlite3"
    with TestClient(create_app(path, seed_demo=False), base_url="http://localhost") as client:
        bad = client.post("/api/auth/register", json={"username": "../../x", "password": PASSWORD})
        assert bad.status_code == 422
        bad = client.post("/api/auth/register", json={"username": "alice", "password": "short"})
        assert bad.status_code == 422
        response = client.post(
            "/api/auth/register", json={"username": "Alice", "password": PASSWORD}
        )
        assert response.status_code == 201
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        assert "password" not in response.text and "mcp_token" not in response.text
        client.headers["X-Session-Key"] = response.json()["session_key"]
        duplicate = client.post(
            "/api/auth/register", json={"username": "ALICE", "password": PASSWORD}
        )
        assert duplicate.status_code == 409
        for username in ["Alice", "missing"]:
            bad = client.post("/api/auth/login", json={"username": username, "password": "wrong"})
            assert bad.status_code == 401
            assert bad.json()["detail"] == "Неверное имя пользователя или пароль."
        old = client.cookies.get(COOKIE)
        assert client.post("/api/auth/logout", json={}).status_code == 200
        client.cookies.set(COOKIE, old)
        assert client.get("/api/profile").status_code == 401
        client.cookies.clear()
        session = login(client, "aLiCe")
        cookie = client.cookies.get(COOKIE)
    with TestClient(create_app(path, seed_demo=False), base_url="http://localhost") as client:
        client.cookies.set(COOKIE, cookie)
        client.headers["X-Session-Key"] = session["session_key"]
        assert client.get("/api/profile").status_code == 200
        with client.app.state.accounts.connect() as db:
            assert db.execute("SELECT password_hash FROM accounts").fetchone()[0] != PASSWORD
            assert db.execute("SELECT token_hash FROM sessions").fetchone()[0] != cookie
            db.execute("UPDATE sessions SET expires_at=0")
        assert client.get("/api/profile").status_code == 401


def test_password_change_revokes_all_sessions_and_throttles(tmp_path):
    app = create_app(tmp_path / "learning.sqlite3", seed_demo=False)
    with TestClient(app, base_url="http://localhost") as alice:
        register(alice, "alice")
        second = TestClient(app, base_url="http://localhost")
        second.portal = alice.portal  # Separate cookies, same deployed application event loop.
        login(second, "alice")
        assert (
            alice.post(
                "/api/auth/password",
                json={
                    "current_password": "wrong",
                    "new_password": PASSWORD + "new",
                },
            ).status_code
            == 401
        )
        assert (
            alice.post(
                "/api/auth/password",
                json={
                    "current_password": PASSWORD,
                    "new_password": PASSWORD + "new",
                },
            ).status_code
            == 200
        )
        assert second.get("/api/profile").status_code == 401
        assert alice.get("/api/profile").status_code == 401
        login(alice, "alice", PASSWORD + "new")
        for _ in range(10):
            response = second.post(
                "/api/auth/login", json={"username": "ghost", "password": "wrong"}
            )
            assert response.status_code == 401
        assert (
            second.post(
                "/api/auth/login", json={"username": "ghost", "password": "wrong"}
            ).status_code
            == 429
        )
        second.close()


def test_learning_data_settings_reset_and_stale_tab_are_private(tmp_path):
    app = create_app(tmp_path / "learning.sqlite3", seed_demo=False)
    with TestClient(app, base_url="http://localhost") as alice:
        alice_session = register(alice, "alice")
        first = material(alice)
        token = next(t for t in first["cues"][0]["tokens"] if t["lemma"] == "book")
        assert (
            alice.put(f"/api/lexemes/{token['lexeme_id']}/knowledge", json={"score": 3}).status_code
            == 200
        )
        conversation = alice.post("/api/conversations", json={"title": "Alice private"}).json()
        diagnostic = alice.post("/api/diagnostics", json={}).json()
        assert alice.put("/api/settings/learning", json={"enabled": True}).status_code == 200
        viewing = alice.post(f"/api/materials/{first['id']}/viewing", json={}).json()["viewing_id"]
        assert alice.get("/api/progress").json()["counts"]["known"] == 1
        before = {
            path: alice.get(path).json()
            for path in [
                "/api/materials",
                "/api/dictionary",
                "/api/activity",
                "/api/profile",
            ]
        }
        alice_app = app.state.learners.apps[alice_session["user"]["id"]]
        alice_app.state.connection.settings_store.write(AISettings("gpt-6-sol", "high"))
        bob = TestClient(app, base_url="http://localhost")
        bob.portal = alice.portal  # Separate browser profile, same server event loop.
        bob_session = register(bob, "bob")
        assert bob.get("/api/materials").json() == []
        assert bob.get("/api/dictionary").json() == []
        assert bob.get("/api/conversations").json() == []
        assert bob.get("/api/activity").json()["events"] == []
        assert bob.get(f"/api/materials/{first['id']}").status_code == 404
        assert bob.get(f"/api/conversations/{conversation['id']}").status_code == 404
        assert (
            bob.post(
                f"/api/diagnostics/{diagnostic['id']}/answers",
                json={
                    "item_id": "another-users-question",
                    "action": "known",
                },
            ).status_code
            == 404
        )
        assert alice.get(f"/api/viewings/{viewing}").status_code == 200
        assert bob.get(f"/api/viewings/{viewing}").status_code == 409
        assert bob.post(f"/api/viewings/{viewing}/confirm", json={}).status_code == 409
        assert bob.get("/api/settings/learning").json()["enabled"] is False
        assert bob.get("/api/progress").json()["counts"]["known"] == 0
        assert (
            bob.put(f"/api/lexemes/{token['lexeme_id']}/knowledge", json={"score": 0}).status_code
            == 404
        )
        second = material(bob)  # Same source text produces private storage and knowledge.
        assert second["id"] != first["id"]
        assert bob.get("/api/ai/settings").json()["model"] == "gpt-6-luna"
        bob_app = app.state.learners.apps[bob_session["user"]["id"]]
        assert (
            alice_app.state.connection.store.directory != bob_app.state.connection.store.directory
        )
        assert (
            bob.post("/api/data/reset", json={"confirmation": "Удалить мои данные"}).status_code
            == 200
        )
        for path, expected in before.items():
            assert alice.get(path).json() == expected
        assert alice.get("/api/ai/settings").json()["model"] == "gpt-6-sol"
        assert bob.get("/api/auth/session").json()["user"]["username"] == "bob"
        # A stale tab has Alice's request key while the browser cookie has changed to Bob.
        bob.headers["X-Session-Key"] = alice_session["session_key"]
        assert bob.get("/api/profile").status_code == 401
        assert (
            bob.post("/api/data/reset", json={"confirmation": "Удалить мои данные"}).status_code
            == 401
        )
        bob.close()


def test_first_registration_adopts_legacy_once_and_keeps_original(tmp_path):
    path = tmp_path / "learning.sqlite3"
    with TestClient(
        create_learner_app(path, seed_demo=False), base_url="http://localhost"
    ) as legacy:
        saved = material(legacy)
        legacy.app.state.connection.settings_store.write(AISettings("gpt-6-sol", "high"))
        credential_dir = legacy.app.state.connection.store.directory
        credential_dir.mkdir()
        (credential_dir / "host-id").write_text("legacy-host")
    with TestClient(create_app(path), base_url="http://localhost") as alice:
        assert alice.get("/api/auth/session").json()["legacy_profile"]
        assert (
            alice.post(
                "/api/auth/register", json={"username": "alice", "password": PASSWORD}
            ).status_code
            == 409
        )
        session = register(alice, "alice", adopt_existing=True)
        assert alice.get(f"/api/materials/{saved['id']}").status_code == 200
        assert alice.get("/api/ai/settings").json()["model"] == "gpt-6-sol"
        copied = app_dir = alice.app.state.accounts.learner_directory(session["user"]["id"])
        assert (copied / "credentials/host-id").read_text() == "legacy-host"
        assert path.exists() and (credential_dir / "host-id").exists()
        register(alice, "bob")
        assert alice.get(f"/api/materials/{saved['id']}").status_code == 404
        assert alice.get("/api/auth/session").json()["legacy_profile"] is False
    with TestClient(create_app(path), base_url="http://localhost") as alice:
        login(alice, "alice")
        assert alice.get(f"/api/materials/{saved['id']}").status_code == 200
        assert app_dir.is_dir()


def test_concurrent_registration_has_one_winner(tmp_path):
    app = create_app(tmp_path / "learning.sqlite3", seed_demo=False, include_ai=False)

    def register_same(_):
        with TestClient(app, base_url="http://localhost") as client:
            return client.post(
                "/api/auth/register", json={"username": "same", "password": PASSWORD}
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(register_same, range(2))) == [201, 409]


@pytest.fixture
def multi_mcp(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "backend.mcp_server"],
        env={
            **os.environ,
            "DATA_DIR": str(tmp_path),
            "MCP_HOST": "127.0.0.1",
            "MCP_PORT": str(port),
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        for _ in range(150):
            assert process.poll() is None
            try:
                if httpx.get(url, timeout=0.1).status_code == 401:
                    break
            except httpx.TransportError:
                time.sleep(0.02)
        else:
            pytest.fail("MCP did not start")
        yield url
    finally:
        process.terminate()
        process.wait(timeout=5)


async def test_real_mcp_keeps_concurrent_users_and_agent_context_separate(tmp_path, multi_mcp):
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from backend.app.learning import create_learning_router

    app = create_app(tmp_path / "learning.sqlite3", seed_demo=False, include_ai=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as alice,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as bob,
    ):
        users, materials = [], []
        for client, name, content in [
            (alice, "alice", "She reads books."),
            (bob, "bob", "He plays music."),
        ]:
            response = await client.post(
                "/api/auth/register", json={"username": name, "password": PASSWORD}
            )
            assert response.status_code == 201
            user = response.json()["user"]
            users.append(user)
            client.headers["X-Session-Key"] = response.json()["session_key"]
            response = await client.post(
                "/api/materials",
                json={
                    "youtube_url": "https://youtu.be/TLkA0RELQ1g",
                    "format": "srt",
                    "content": f"1\n00:00:00,000 --> 00:00:03,000\n{content}\n",
                },
            )
            materials.append(response.json())
        store: AccountStore = app.state.accounts
        with store.connect() as db:
            tokens = [
                db.execute("SELECT mcp_token FROM accounts WHERE id=?", (u["id"],)).fetchone()[0]
                for u in users
            ]
        async with httpx.AsyncClient() as outsider:
            assert (
                await outsider.get(multi_mcp, headers={"Authorization": "Bearer forged"})
            ).status_code == 401

        async def own_context(index):
            async with Client(multi_mcp, auth=tokens[index]) as client:
                own, other = materials[index], materials[1 - index]
                result = await client.call_tool(
                    "get_subtitle_context",
                    {"material_id": own["id"], "cue_id": own["cues"][0]["id"]},
                )
                assert own["cues"][0]["text"] in str(result)
                with pytest.raises(ToolError):
                    await client.call_tool(
                        "get_subtitle_context",
                        {"material_id": other["id"], "cue_id": other["cues"][0]["id"]},
                    )
                forged = await client.call_tool(
                    "get_subtitle_context",
                    {
                        "material_id": own["id"],
                        "cue_id": own["cues"][0]["id"],
                        "user_id": users[1 - index]["id"],
                    },
                )
                assert own["cues"][0]["text"] in str(forged)

        await asyncio.gather(own_context(0), own_context(1))
        observed = []

        def model(messages, info):
            observed.append(current_mcp_token.get())
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        info.output_tools[0].name,
                        {
                            "translation": "читает",
                            "explanation": "Действие в настоящем времени.",
                            "example": "She reads.",
                            "expression": "reads",
                        },
                    )
                ]
            )

        for user in users:
            learner = app.state.learners.apps[user["id"]]
            learner.include_router(
                create_learning_router(
                    library=learner.state.materials,
                    connection=None,
                    mcp_url=multi_mcp,
                    model_factory=lambda: FunctionModel(model),
                )
            )

        async def card(client, item):
            cue = item["cues"][0]
            return await client.post(
                "/api/cards",
                json={
                    "material_id": item["id"],
                    "cue_id": cue["id"],
                    "token_id": cue["tokens"][1]["id"],
                },
            )

        responses = await asyncio.gather(card(alice, materials[0]), card(bob, materials[1]))
        assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
        assert sorted(observed) == sorted(tokens)
        assert current_mcp_token.get() is None
    await app.state.learners.close()


def test_password_change_prevents_late_session_creation(tmp_path):
    from fastapi import HTTPException

    from backend.app.accounts import AccountInput, LoginInput, PasswordInput

    store = AccountStore(tmp_path)
    user = store.register(AccountInput(username="alice", password=PASSWORD))
    verified = store.login(LoginInput(username="alice", password=PASSWORD))
    store.change_password(
        user["id"], PasswordInput(current_password=PASSWORD, new_password=PASSWORD + "new")
    )
    with pytest.raises(HTTPException) as error:
        store.new_session(user["id"], "", verified["verified_hash"])
    assert error.value.status_code == 401
