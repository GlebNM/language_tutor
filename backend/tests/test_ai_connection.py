from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.ai_connection import create_ai_router


def test_local_learner_can_inspect_connection_without_credentials(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(create_ai_router(data_dir=tmp_path, mcp_url="http://mcp:8001/mcp"))
    with TestClient(app) as client:
        response = client.get("/api/ai/connection")
        assert response.status_code == 200
        assert response.json()["status"] == "disconnected"
        assert client.post("/api/ai/proof").status_code == 409


def test_oauth_listener_is_ready_and_rejects_wrong_state_before_denial(tmp_path: Path) -> None:
    import socket
    from urllib.parse import parse_qs, urlparse

    import httpx

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    app = FastAPI()
    app.include_router(
        create_ai_router(
            data_dir=tmp_path,
            mcp_url="http://mcp:8001/mcp",
            callback_host="127.0.0.1",
            callback_port=port,
        )
    )
    with TestClient(app) as client:
        start = client.post("/api/ai/connection/start")
        assert start.status_code == 200
        values = parse_qs(urlparse(start.json()["authorization_url"]).query)
        assert values["redirect_uri"] == ["http://localhost:1455/auth/callback"]
        assert values["code_challenge_method"] == ["S256"]
        callback = f"http://127.0.0.1:{port}/auth/callback"
        rejected = httpx.get(callback, params={"state": "wrong", "error": "access_denied"})
        assert rejected.status_code == 400
        assert client.get("/api/ai/connection").json()["status"] == "connecting"
        denied = httpx.get(callback, params={"state": values["state"][0], "error": "access_denied"})
        assert denied.status_code == 400
        state = client.get("/api/ai/connection").json()
        assert state["status"] == "disconnected"
        assert state["error"] == "oauth_denied"
        assert "access_token" not in str(state)


def test_occupied_callback_port_returns_actionable_error(tmp_path: Path) -> None:
    import socket

    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        app = FastAPI()
        app.include_router(
            create_ai_router(
                data_dir=tmp_path,
                mcp_url="http://mcp:8001/mcp",
                callback_host="127.0.0.1",
                callback_port=occupied.getsockname()[1],
            )
        )
        with TestClient(app) as client:
            response = client.post("/api/ai/connection/start")
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == "callback_port_in_use"
            assert client.get("/api/ai/connection").json()["status"] == "disconnected"


def test_subscription_survives_restart_and_disconnect_removes_access(tmp_path: Path) -> None:
    import socket
    from urllib.parse import parse_qs, urlparse

    import httpx
    from pydantic_ai.providers.openai_codex import OpenAICodexCredentials, OpenAICodexOAuthFlow

    class ControlledOAuth(OpenAICodexOAuthFlow):
        async def exchange_code(self, code: str) -> OpenAICodexCredentials:
            assert code == "test-authorization-code"
            return OpenAICodexCredentials(
                access_token="test-secret-access",
                refresh_token="test-secret-refresh",
                account_id="test",
            )

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    def make_app() -> FastAPI:
        app = FastAPI()
        app.include_router(
            create_ai_router(
                data_dir=tmp_path,
                mcp_url="http://mcp:8001/mcp",
                callback_host="127.0.0.1",
                callback_port=port,
                flow_factory=ControlledOAuth,
            )
        )
        return app

    with TestClient(make_app()) as client:
        start = client.post("/api/ai/connection/start").json()
        state = parse_qs(urlparse(start["authorization_url"]).query)["state"][0]
        response = httpx.get(
            f"http://127.0.0.1:{port}/auth/callback",
            params={
                "state": state,
                "code": "test-authorization-code",
            },
        )
        assert response.status_code == 200
        assert "test-secret" not in response.text
        assert client.get("/api/ai/connection").json()["status"] == "connected"
    with TestClient(make_app()) as client:
        status = client.get("/api/ai/connection").json()
        assert status["status"] == "connected"
        assert "test-secret" not in str(status)
        assert status["live_verified"] is False
        assert client.delete("/api/ai/connection").json()["status"] == "disconnected"
    with TestClient(make_app()) as client:
        assert client.get("/api/ai/connection").json()["status"] == "disconnected"
        assert client.post("/api/ai/proof").status_code == 409


def test_cancelled_oauth_callback_cannot_reconnect(tmp_path: Path) -> None:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    app = FastAPI()
    app.include_router(
        create_ai_router(
            data_dir=tmp_path,
            mcp_url="http://mcp:8001/mcp",
            callback_host="127.0.0.1",
            callback_port=port,
        )
    )
    with TestClient(app) as client:
        assert client.post("/api/ai/connection/start").status_code == 200
        assert client.post("/api/ai/connection/cancel").json()["status"] == "disconnected"
        assert client.get("/api/ai/connection").json()["live_verified"] is False
        assert client.post("/api/ai/proof").status_code == 409


async def test_credential_source_rotations_remain_available_after_reopening(tmp_path: Path) -> None:
    from pydantic_ai.providers.openai_codex import OpenAICodexCredentials

    from backend.app.ai_connection.storage import CredentialStore

    source = CredentialStore(tmp_path / "credentials")
    await source.save(
        OpenAICodexCredentials(access_token="first", refresh_token="refresh1", account_id="x")
    )
    await source.save(
        OpenAICodexCredentials(access_token="rotated", refresh_token="refresh2", account_id="x")
    )
    restored = await CredentialStore(tmp_path / "credentials").load()
    assert restored.access_token == "rotated"
    assert restored.refresh_token == "refresh2"
    assert source.path.stat().st_mode & 0o777 == 0o600
    assert source.directory.stat().st_mode & 0o777 == 0o700
    source.disconnect()
    import pytest

    with pytest.raises(ValueError, match="disconnected"):
        await source.save(restored)


def test_oauth_timeout_releases_listener_and_allows_retry(tmp_path: Path) -> None:
    import socket
    import time

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    app = FastAPI()
    app.include_router(
        create_ai_router(
            data_dir=tmp_path,
            mcp_url="http://mcp:8001/mcp",
            callback_host="127.0.0.1",
            callback_port=port,
            oauth_timeout=0.05,
        )
    )
    with TestClient(app) as client:
        assert client.post("/api/ai/connection/start").status_code == 200
        for _ in range(100):
            state = client.get("/api/ai/connection").json()
            if state["status"] == "disconnected":
                break
            time.sleep(0.01)
        assert state["error"] == "oauth_timeout"
        assert client.post("/api/ai/connection/start").status_code == 200
