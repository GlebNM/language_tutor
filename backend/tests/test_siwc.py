"""Official SIWC at its OAuth and provider HTTP seams, with controlled remote responses."""

import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.app.ai_connection.siwc import SIWCOAuthFlow
from backend.app.ai_connection.storage import CredentialStore


@pytest.mark.parametrize(
    "failure", [None, "nonce", "audience", "signature", "expired", "permission"]
)
async def test_siwc_validates_nonce_identity_and_permission_before_saving(
    tmp_path: Path,
    failure: str | None,
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public["kid"] = "test-key"
    store = CredentialStore(tmp_path / "credentials")
    flow = SIWCOAuthFlow(store=store, redirect_uri="http://127.0.0.1:1456/auth/callback")
    query = parse_qs(urlparse(flow.authorization_url()).query)
    assert query["client_id"] == ["dynamic_agent_client"]
    assert query["ext_agent_host_id"][0].startswith("urn:uuid:")
    assert "chatgpt.tokens.use.direct" in query["scope"][0]
    assert query["nonce"][0] != query["state"][0]

    async def remote(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://auth.openai.com",
                    "jwks_uri": "https://auth.openai.com/.well-known/jwks.json",
                },
            )
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json={"keys": [public]})
        form = parse_qs(request.content.decode())
        assert form["client_id"] == ["oaiapp_test"]
        assert form["redirect_uri"] == ["http://127.0.0.1:1456/auth/callback"]
        token = jwt.encode(
            {
                "iss": "https://auth.openai.com",
                "aud": "wrong-client" if failure == "audience" else "oaiapp_test",
                "sub": "learner",
                "exp": int(time.time()) + (-3600 if failure == "expired" else 3600),
                "nonce": "wrong-nonce" if failure == "nonce" else query["nonce"][0],
            },
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            if failure == "signature"
            else key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        return httpx.Response(
            200,
            json={
                "access_token": "test-access",
                "refresh_token": "test-refresh",
                "id_token": token,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "openid"
                if failure == "permission"
                else (
                    "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
                ),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        from backend.app.ai_connection.siwc import SubscriptionError, TokenManager

        if failure in ("nonce", "audience", "signature", "expired"):
            with pytest.raises((jwt.PyJWTError, SubscriptionError)):
                await flow.exchange_code("test-code", client_id="oaiapp_test", client=client)
            assert not store.path.exists()
            return
        credentials = await flow.exchange_code("test-code", client_id="oaiapp_test", client=client)
    await store.save(credentials)
    assert (await store.load()).subject == "learner"
    if failure == "permission":
        with pytest.raises(SubscriptionError, match="plan_permission_missing"):
            await TokenManager(store).access_token()
        return
    returning = parse_qs(
        urlparse(
            SIWCOAuthFlow(
                store=store,
                redirect_uri="http://127.0.0.1:1456/auth/callback",
            ).authorization_url()
        ).query
    )
    assert returning["client_id"] == ["oaiapp_test"]
    assert returning["ext_agent_host_id"] == query["ext_agent_host_id"]
    assert "agent_name_hint" not in returning
    assert "id_token_hint" not in returning


async def test_siwc_provider_maps_tools_and_requires_completed_stream(tmp_path: Path) -> None:
    import httpx2
    from openai import AsyncOpenAI
    from pydantic_ai import Agent
    from pydantic_ai.providers.openai import OpenAIProvider

    from backend.app.ai_connection.siwc import TokenManager
    from backend.app.ai_connection.siwc_credentials import SIWCCredentials
    from backend.app.ai_connection.siwc_model import SIWCTransport, siwc_model

    store = CredentialStore(tmp_path / "credentials")
    await store.save(
        SIWCCredentials(
            access_token="test-access",
            refresh_token="test-refresh",
            id_token="test-id",
            client_id="oaiapp_test",
            subject="learner",
            scopes=["chatgpt.tokens.use.direct"],
            expires_at=time.time() + 3600,
        )
    )

    async def remote(request: httpx2.Request) -> httpx2.Response:
        assert request.url == "https://api.openai.com/v1/responses"
        assert request.headers["authorization"] == "Bearer test-access"
        body = json.loads(request.content)
        assert body["store"] is False and body["stream"] is True
        assert "tools" not in body
        tools = body["input"][0]
        assert tools["type"] == "additional_tools" and tools["role"] == "developer"
        assert tools["tools"][0]["type"] == "function"
        assert "max_output_tokens" not in body
        response = {
            "id": "resp_test",
            "object": "response",
            "created_at": time.time(),
            "model": "gpt-6.1-sol",
            "status": "completed",
            "output": [
                {
                    "id": "fc_test",
                    "type": "function_call",
                    "call_id": "call_test",
                    "name": tools["tools"][0]["name"],
                    "arguments": '{"answer":"Проверено"}',
                    "status": "completed",
                }
            ],
        }
        item = response["output"][0]
        events = [
            {
                "type": "response.output_item.added",
                "item": {**item, "arguments": ""},
                "output_index": 0,
                "sequence_number": 0,
            },
            {
                "type": "response.output_item.done",
                "item": item,
                "output_index": 0,
                "sequence_number": 1,
            },
            {"type": "response.completed", "response": response, "sequence_number": 2},
        ]
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(f"data: {json.dumps(event)}\n\n" for event in events).encode(),
        )

    client = httpx2.AsyncClient(
        transport=SIWCTransport(
            TokenManager(store),
            transport=httpx2.MockTransport(remote),
        )
    )
    sdk = AsyncOpenAI(api_key="subscription-oauth", http_client=client, max_retries=0)
    model = siwc_model("gpt-6.1-sol", OpenAIProvider(openai_client=sdk))
    from pydantic import BaseModel

    class Answer(BaseModel):
        answer: str

    async with client, Agent(model, output_type=Answer, name="siwc_wire_test") as agent:
        result = await agent.run("Проверка")
    assert result.output.answer == "Проверено"


@pytest.mark.parametrize("terminal", ["eof", "response.incomplete", "response.failed"])
async def test_siwc_never_accepts_partial_stream_as_completion(
    tmp_path: Path, terminal: str
) -> None:
    import httpx2
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    from backend.app.ai_connection.siwc import SubscriptionError, TokenManager
    from backend.app.ai_connection.siwc_credentials import SIWCCredentials
    from backend.app.ai_connection.siwc_model import SIWCTransport

    store = CredentialStore(tmp_path / "credentials")
    await store.save(
        SIWCCredentials(
            access_token="test-access",
            refresh_token="test-refresh",
            id_token="test-id",
            client_id="oaiapp_test",
            subject="learner",
            scopes=["chatgpt.tokens.use.direct"],
            expires_at=time.time() + 3600,
        )
    )
    events = [{"type": "response.output_text.delta", "delta": "A partial answer"}]
    if terminal != "eof":
        events.append(
            {
                "type": terminal,
                "response": {
                    "error": {
                        "code": "subscription_sharing_usage_limit_exceeded",
                    }
                },
            }
        )
    wire = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()

    async def remote(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=wire)

    async with httpx2.AsyncClient(
        transport=SIWCTransport(
            TokenManager(store),
            transport=httpx2.MockTransport(remote),
        )
    ) as client:
        with pytest.raises((UnexpectedModelBehavior, SubscriptionError)):
            await client.post(
                "https://api.openai.com/v1/responses", json={"input": [], "model": "test"}
            )


async def test_concurrent_siwc_requests_share_one_rotating_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from backend.app.ai_connection.siwc import TokenManager
    from backend.app.ai_connection.siwc_credentials import SIWCCredentials

    store = CredentialStore(tmp_path / "credentials")
    await store.save(
        SIWCCredentials(
            access_token="expired",
            refresh_token="old-refresh",
            id_token="test-id",
            client_id="oaiapp_test",
            subject="learner",
            scopes=["chatgpt.tokens.use.direct"],
            expires_at=0,
        )
    )
    requests = []

    async def remote(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["refresh_token"]
        assert form["client_id"] == ["oaiapp_test"]
        assert form["refresh_token"] == ["old-refresh"]
        assert "scope" not in form
        requests.append(request)
        await asyncio.sleep(0.01)
        return httpx.Response(
            200,
            json={
                "access_token": "rotated",
                "refresh_token": "rotated-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(remote))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
    manager = TokenManager(store)
    assert await asyncio.gather(manager.access_token(), manager.access_token()) == [
        "rotated",
        "rotated",
    ]
    assert len(requests) == 1
    assert (await store.load()).refresh_token == "rotated-refresh"
