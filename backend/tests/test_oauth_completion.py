from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.ai_connection import create_ai_router
from backend.app.ai_connection.siwc import SIWCOAuthFlow
from backend.app.ai_connection.siwc_credentials import SIWCCredentials


def credentials() -> SIWCCredentials:
    return SIWCCredentials(
        access_token="synthetic-access-secret",
        refresh_token="synthetic-refresh-secret",
        id_token="synthetic-id-secret",
        client_id="oaiapp_test",
        subject="test-learner",
        scopes=["chatgpt.tokens.use.direct"],
        expires_at=9999999999,
    )


def app(tmp_path: Path) -> FastAPI:
    class ControlledOAuth(SIWCOAuthFlow):
        async def exchange_code(self, code: str, **kwargs: object) -> SIWCCredentials:
            assert code == "synthetic-authorization-code"
            assert kwargs["client_id"] == "oaiapp_test"
            assert self.verifier and self.nonce
            return credentials()

    application = FastAPI()
    application.include_router(create_ai_router(
        data_dir=tmp_path, mcp_url="http://mcp:8001/mcp",
        callback_host="127.0.0.1", callback_port=0, flow_factory=ControlledOAuth,
    ))
    return application


def returned_url(client: TestClient, **params: str) -> str:
    started = client.post("/api/ai/connection/start")
    assert started.status_code == 200, started.text
    query = parse_qs(urlsplit(started.json()["authorization_url"]).query)
    return query["redirect_uri"][0] + "?" + urlencode({
        "state": query["state"][0], "code": "synthetic-authorization-code",
        "client_id": "oaiapp_test", **params,
    })


def test_complete_through_api_without_browser_callback(tmp_path):
    with TestClient(app(tmp_path)) as client:
        url = returned_url(client)
        response = client.post("/api/ai/connection/complete", json={"callback_url": url})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "connected"
        assert "synthetic-" not in response.text
        assert client.post(
            "/api/ai/connection/complete", json={"callback_url": url}
        ).status_code == 409
    with TestClient(app(tmp_path)) as client:
        assert client.get("/api/ai/connection").json()["status"] == "connected"


@pytest.mark.parametrize("replace", [
    lambda url: url.replace("127.0.0.1", "example.com"),
    lambda url: url.replace("http:", "https:"),
    lambda url: url.replace("127.0.0.1:0", "127.0.0.1:1234"),
    lambda url: url.replace("127.0.0.1", "user@127.0.0.1"),
    lambda url: url.replace("/auth/callback", "/other"),
    lambda url: url + "#fragment",
    lambda url: url + "&state=duplicate",
    lambda url: url + "&code=duplicate",
    lambda url: url + "&client_id=duplicate",
    lambda url: url + "\nextra",
])
def test_bad_returned_address_does_not_consume_attempt(tmp_path, replace):
    with TestClient(app(tmp_path)) as client:
        url = returned_url(client)
        rejected = client.post(
            "/api/ai/connection/complete", json={"callback_url": replace(url)}
        )
        assert rejected.status_code == 400
        assert "synthetic-" not in rejected.text
        assert client.get("/api/ai/connection").json()["status"] == "connecting"
        assert client.post(
            "/api/ai/connection/complete", json={"callback_url": url}
        ).json()["status"] == "connected"


def test_wrong_state_denial_cancel_and_retry(tmp_path):
    with TestClient(app(tmp_path)) as client:
        url = returned_url(client)
        query = parse_qs(urlsplit(url).query)
        wrong = url.replace(query["state"][0], "wrong-ю-state")
        assert client.post(
            "/api/ai/connection/complete", json={"callback_url": wrong}
        ).status_code == 400
        assert client.get("/api/ai/connection").json()["status"] == "connecting"
        denied = client.post(
            "/api/ai/connection/complete", json={"callback_url": url + "&error=access_denied"}
        )
        assert denied.status_code == 400
        assert client.get("/api/ai/connection").json()["error"] == "oauth_denied"
        retried = returned_url(client)
        client.post("/api/ai/connection/cancel")
        assert client.post(
            "/api/ai/connection/complete", json={"callback_url": retried}
        ).status_code == 409


def test_returned_address_cannot_complete_another_account(tmp_path, monkeypatch):
    from backend.app.main import create_app

    async def exchange(self, code, **kwargs):
        return credentials()

    monkeypatch.setattr(SIWCOAuthFlow, "exchange_code", exchange)
    monkeypatch.setenv("AI_CALLBACK_PORT", "0")
    application = create_app(tmp_path / "learning.sqlite3", seed_demo=False)
    with TestClient(application, base_url="http://localhost") as alice:
        r = alice.post("/api/auth/register", json={
            "username": "alice", "password": "synthetic-long-password",
        })
        alice.headers["X-Session-Key"] = r.json()["session_key"]
        url = returned_url(alice)
        bob = TestClient(application, base_url="http://localhost")
        bob.portal = alice.portal
        r = bob.post("/api/auth/register", json={
            "username": "bob", "password": "synthetic-long-password",
        })
        bob.headers["X-Session-Key"] = r.json()["session_key"]
        assert bob.post(
            "/api/ai/connection/complete", json={"callback_url": url}
        ).status_code == 409
        assert alice.post(
            "/api/ai/connection/complete", json={"callback_url": url}
        ).json()["status"] == "connected"
        assert bob.get("/api/ai/connection").json()["status"] == "disconnected"
