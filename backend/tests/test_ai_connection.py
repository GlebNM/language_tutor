from pathlib import Path

import httpx
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.ai_connection import create_ai_router
from backend.app.ai_connection.siwc import SIWCOAuthFlow, SubscriptionError
from backend.app.ai_connection.siwc_credentials import SIWCCredentials


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
        assert values["redirect_uri"] == [f"http://127.0.0.1:{port}/auth/callback"]
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
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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

    from backend.app.ai_connection.siwc import SIWCOAuthFlow
    from backend.app.ai_connection.siwc_credentials import SIWCCredentials

    class ControlledOAuth(SIWCOAuthFlow):
        async def exchange_code(self, code: str, **kwargs: object) -> SIWCCredentials:
            assert code == "test-authorization-code"
            return SIWCCredentials(
                access_token="test-secret-access",
                refresh_token="test-secret-refresh",
                client_id="oaiapp_test",
                subject="test",
                id_token="test-id-token",
                scopes=["chatgpt.tokens.use.direct"],
                expires_at=9999999999,
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
    from backend.app.ai_connection.siwc_credentials import SIWCCredentials
    from backend.app.ai_connection.storage import CredentialStore

    source = CredentialStore(tmp_path / "credentials")
    await source.save(
        SIWCCredentials(
            access_token="first",
            refresh_token="refresh1",
            client_id="oaiapp_test",
            subject="x",
            id_token="test-id",
            scopes=["chatgpt.tokens.use.direct"],
            expires_at=9999999999,
        )
    )
    await source.save(
        SIWCCredentials(
            access_token="rotated",
            refresh_token="refresh2",
            client_id="oaiapp_test",
            subject="x",
            id_token="test-id",
            scopes=["chatgpt.tokens.use.direct"],
            expires_at=9999999999,
        )
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


def test_oauth_can_retry_same_port_immediately_after_completed_callback(tmp_path: Path) -> None:
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
    with TestClient(app) as client, httpx.Client() as browser:
        for _ in range(3):
            started = client.post("/api/ai/connection/start")
            assert started.status_code == 200
            state = parse_qs(urlparse(started.json()["authorization_url"]).query)["state"][0]
            denied = browser.get(
                f"http://127.0.0.1:{port}/auth/callback",
                params={
                    "state": state,
                    "error": "access_denied",
                },
            )
            assert denied.status_code == 400
            retried = client.post("/api/ai/connection/start")
            assert retried.status_code == 200, "Completed callback must permit immediate port reuse"
            assert client.post("/api/ai/connection/cancel").status_code == 200


def test_repeated_oauth_callback_cannot_reuse_completed_state(tmp_path: Path) -> None:
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
        started = client.post("/api/ai/connection/start")
        state = parse_qs(urlparse(started.json()["authorization_url"]).query)["state"][0]
        callback = f"http://127.0.0.1:{port}/auth/callback"
        first = httpx.get(callback, params={"state": state, "error": "access_denied"})
        repeated = httpx.get(callback, params={"state": state, "error": "access_denied"})

        assert first.status_code == 400
        assert repeated.status_code == 409
        assert client.get("/api/ai/connection").json()["status"] == "disconnected"


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (SubscriptionError("test-secret-provider-message", 400), "provider"),
        (SubscriptionError("invalid_nonce", 401), "identity"),
        (SubscriptionError("missing_issued_client_id", 401), "registration"),
        (jwt.InvalidSignatureError("test-secret-signature-message"), "identity"),
        (httpx.ConnectError("test-secret-network-message"), "network"),
        (TimeoutError("test-secret-timeout-message"), "timeout"),
        (PermissionError("test-secret-storage-message"), "storage"),
        (RuntimeError("test-secret-unexpected-message"), "unexpected"),
    ],
)
def test_oauth_failure_reports_only_a_bounded_safe_category(
    tmp_path: Path,
    failure: Exception,
    category: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import socket
    from urllib.parse import parse_qs, urlparse

    class FailedOAuth(SIWCOAuthFlow):
        async def exchange_code(self, code: str, **kwargs: object) -> SIWCCredentials:
            raise failure

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
            flow_factory=FailedOAuth,
        )
    )
    with TestClient(app) as client:
        started = client.post("/api/ai/connection/start")
        assert started.status_code == 200
        state = parse_qs(urlparse(started.json()["authorization_url"]).query)["state"][0]
        failed = httpx.get(
            f"http://127.0.0.1:{port}/auth/callback",
            params={
                "state": state,
                "code": "controlled-test-code",
            },
        )
        assert failed.status_code == 400
        status = client.get("/api/ai/connection").json()
        assert status["status"] == "reauth_required"
        assert status["error"] == "oauth_exchange_failed"
        assert status["error_category"] == category
        assert "test-secret" not in str(status) + failed.text + caplog.text
