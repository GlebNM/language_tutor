"""Official public-client SIWC registration, OIDC validation and token refresh."""

import asyncio
import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from .siwc_credentials import SIWCCredentials
from .storage import CredentialStore, Registration

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
DIRECT_SCOPE = "chatgpt.tokens.use.direct"
SCOPE = f"openid profile email offline_access resource.invoke {DIRECT_SCOPE}"


class SubscriptionError(Exception):
    def __init__(self, code: str, status: int = 503) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class SIWCOAuthFlow:
    def __init__(self, *, store: CredentialStore, redirect_uri: str) -> None:
        self.store = store
        self.redirect_uri = redirect_uri
        self.registration = store.registration()
        self.state = secrets.token_urlsafe(32)
        self.nonce = secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(64)

    def authorization_url(self) -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest())
        params = {
            "client_id": self.registration.client_id
            if self.registration
            else "dynamic_agent_client",
            "ext_agent_host_id": self.store.host_id(),
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": SCOPE,
            "resource": RESOURCE,
            "state": self.state,
            "nonce": self.nonce,
            "code_challenge_method": "S256",
            "code_challenge": challenge.rstrip(b"=").decode(),
        }
        if self.registration is None:
            params["agent_name_hint"] = "Language Tutor"
        # Login hints are optional; omit id_token_hint to keep tokens out of UI responses.
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(
        self,
        code: str,
        *,
        client_id: str | None,
        client: httpx.AsyncClient | None = None,
    ) -> SIWCCredentials:
        if self.registration:
            if client_id and client_id != self.registration.client_id:
                raise SubscriptionError("client_identity_mismatch", 401)
            client_id = self.registration.client_id
        if not client_id or client_id == "dynamic_agent_client":
            raise SubscriptionError("missing_issued_client_id", 401)
        if self.registration is None:
            self.store.save_registration(Registration(client_id=client_id))
        if client is None:
            async with httpx.AsyncClient(timeout=10) as own_client:
                return await self.exchange_code(code, client_id=client_id, client=own_client)
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": self.verifier,
                "redirect_uri": self.redirect_uri,
                "resource": RESOURCE,
            },
        )
        data = token_response(response)
        discovery_response = await client.get(f"{ISSUER}/.well-known/openid-configuration")
        discovery_response.raise_for_status()
        discovery = discovery_response.json()
        if discovery.get("issuer") != ISSUER:
            raise SubscriptionError("invalid_issuer", 401)
        # Discovery is trusted only from the fixed issuer, and JWKS stays at that origin.
        if discovery.get("jwks_uri") != f"{ISSUER}/.well-known/jwks.json":
            raise SubscriptionError("invalid_jwks_location", 401)
        keys_response = await client.get(discovery["jwks_uri"])
        keys_response.raise_for_status()
        header = jwt.get_unverified_header(data["id_token"])
        if header.get("alg") not in ("RS256", "ES256"):
            raise SubscriptionError("invalid_signing_algorithm", 401)
        key = next(
            (key for key in keys_response.json()["keys"] if key.get("kid") == header.get("kid")),
            None,
        )
        if key is None:
            raise SubscriptionError("unknown_signing_key", 401)
        claims = jwt.decode(
            data["id_token"],
            jwt.PyJWK.from_dict(key).key,
            algorithms=[header["alg"]],
            issuer=ISSUER,
            audience=client_id,
            options={"require": ["iss", "aud", "exp", "sub", "nonce"]},
        )
        if not secrets.compare_digest(str(claims["nonce"]), self.nonce):
            raise SubscriptionError("invalid_nonce", 401)
        if self.registration and self.registration.subject:
            if claims["sub"] != self.registration.subject:
                raise SubscriptionError("subject_identity_mismatch", 401)
        return SIWCCredentials(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            id_token=data["id_token"],
            client_id=client_id,
            subject=claims["sub"],
            scopes=data["scope"].split(),
            expires_at=time.time() + data["expires_in"],
        )


def token_response(response: httpx.Response) -> dict[str, Any]:
    data = response.json()
    if response.is_error:
        error = data.get("error", "oauth_unavailable")
        code = error.get("code", "oauth_unavailable") if isinstance(error, dict) else str(error)
        raise SubscriptionError(code, response.status_code)
    if data.get("token_type", "").lower() != "bearer":
        raise SubscriptionError("invalid_token_type", 401)
    return data  # type: ignore[no-any-return]


class TokenManager:
    def __init__(self, store: CredentialStore) -> None:
        self.store = store
        self.lock = asyncio.Lock()

    async def access_token(self) -> str:
        async with self.lock:
            credentials = await self.store.load()
            if DIRECT_SCOPE not in credentials.scopes:
                raise SubscriptionError("plan_permission_missing", 401)
            if credentials.expires_at > time.time() + 30:
                return credentials.access_token
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "client_id": credentials.client_id,
                        "refresh_token": credentials.refresh_token,
                        "resource": RESOURCE,
                    },
                )
            try:
                data = token_response(response)
            except SubscriptionError as error:
                if error.code in {
                    "invalid_grant",
                    "invalid_refresh_token",
                    "token_expired",
                    "refresh_token_expired",
                    "refresh_token_invalidated",
                    "refresh_token_reused",
                }:
                    self.store.path.unlink(missing_ok=True)
                raise
            updated = SIWCCredentials.model_validate(
                {
                    **credentials.model_dump(),
                    "access_token": data["access_token"],
                    "refresh_token": data["refresh_token"],
                    "expires_at": time.time() + data["expires_in"],
                    "scopes": str(data.get("scope", " ".join(credentials.scopes))).split(),
                    "id_token": data.get("id_token", credentials.id_token),
                }
            )
            if DIRECT_SCOPE not in updated.scopes:
                raise SubscriptionError("plan_permission_missing", 401)
            await self.store.save(updated)
            return updated.access_token
