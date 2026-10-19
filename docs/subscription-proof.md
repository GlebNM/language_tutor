# Local subscription proof — ticket #1

Checked on 19 October 2026. The selected route is **official Sign in with ChatGPT (SIWC) for local open-source applications**. The application implements that public registration and inference contract. **Ticket #1's live feasibility result passed:** host-browser consent completed, and the named Agent returned a validated contextual card after a real model-selected HTTP MCP call. The same proof passed after app container recreation without another sign-in. Full MVP criteria A16 and A17 remain incomplete because they also cover later AI features and lifecycle scenarios. The agent did not inspect tokens, authorization codes or Codex CLI credential files.

## Supported access contract

SIWC public dynamic registration uses `client_id=dynamic_agent_client` on first authorization and requires no client secret or partner key. Language Tutor saves a stable host UUID, supplies its actual application name, receives the issued client ID from the callback, and retains that registration for later authorization. Fresh state, nonce and S256 PKCE values belong to each attempt. The callback uses `http://127.0.0.1:<AI_CALLBACK_PORT>/auth/callback`. The application verifies the ID token's JWKS signature, issuer, audience, expiration, nonce and returning subject before saving credentials. It separately checks the granted `chatgpt.tokens.use.direct` permission before inference. Identity without that permission cannot power AI. [Official registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in).

Inference uses only the official `https://api.openai.com/v1/responses` endpoint with `store:false` and `stream:true`. The authenticated account's `/v1/models` catalog supplies eligible slugs; with an empty `AI_MODEL`, the proof selects the first visible model in server order. A configured slug must appear in that catalog. Completion requires the terminal `response.completed` event. An interrupted, incomplete or late-failed stream cannot count as a successful card. [Official models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference).

SIWC requires function tools in namespaces or `additional_tools` input items. The application uses the latter: a dedicated HTTP transport moves Pydantic's local MCP and structured-output function definitions into a developer `additional_tools` input item. Tool names and returned call IDs stay intact. Unsupported request fields and hosted tools are excluded; server-side continuation settings are rejected before Pydantic could trim history. Pydantic's ordinary Responses adapter still owns message mapping, stream parsing, model/tool turns, MCP dispatch and Pydantic output validation. [Official preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations).

An earlier implementation investigated Pydantic's Codex public-client provider. That candidate has been removed from product code. Its fixed `localhost:1455` registration and ChatGPT backend endpoint are different from SIWC and are not used by this application. The official loopback URI permits another port, so the default is now 1456. [Pydantic's distinct Codex integration](https://pydantic.dev/docs/ai/models/openai-codex/).

## Resolved dependencies

`pyproject.toml` pins the integration libraries and `uv.lock` locks all transitive packages. Controlled checks ran with CPython 3.12.11 on macOS arm64. Python 3.12 also supports the selected language-processing dependencies. The Pydantic agent skill's own version is unrelated to the library pin.

| Component | Version |
| --- | --- |
| Python runtime series | 3.12 |
| pydantic-ai-slim / pydantic-graph | 2.57.0 |
| OpenAI SDK | 3.35.1 |
| Official MCP SDK | 1.30.0 |
| FastMCP slim client | 3.4.9 |
| Pydantic | 2.14.1 |
| HTTPX / HTTPX2 | 0.28.1 / 2.14.0 |
| PyJWT with cryptography | 2.15.1 |
| FastAPI / Uvicorn | 0.148.0 / 0.54.0 |
| spaCy / en_core_web_sm | 3.8.11 / 3.8.0 |

The published Pydantic release supports Python 3.11+, OpenAI SDK 3.26.0 or later, and the selected MCP client range. Installed signatures and runtime behavior were checked against 2.57.0, including `MCPToolset`, model profiles and the Responses implementation. [Release metadata](https://pypi.org/pypi/pydantic-ai-slim/2.57.0/json).

## Runnable boundary

The backend includes `create_ai_router(data_dir=..., mcp_url=..., model_name=..., callback_port=...)`. Keep `AI_MODEL` empty to use the first eligible catalog entry, or set an account-supported slug explicitly.

| Operation | Route |
| --- | --- |
| Inspect connection | `GET /api/ai/connection` |
| Start browser authorization | `POST /api/ai/connection/start` |
| Cancel pending authorization | `POST /api/ai/connection/cancel` |
| Disconnect and remove local tokens | `DELETE /api/ai/connection` |
| Run authored proof card via subscription and MCP | `POST /api/ai/proof` |

Use the callback-enabled Compose command in the README. The host app and callback ports publish only on loopback; MCP has no published port. `AI_CALLBACK_PORT` defaults to 1456. Its host publication, container listener and redirect URI must use the same port. The listener binds `0.0.0.0` inside the container while the browser sees `127.0.0.1` on the host. The existing unrelated service on 1455 is unaffected.

The UI's **Continue with ChatGPT** action obtains an authorization URL only after the listener is ready. State is checked before OAuth error handling or code exchange. Authorization is consumed once; denial, cancellation and a five-minute timeout release the listener. Callback access logging is disabled, so authorization codes are not logged. A callback port occupied inside the container returns `callback_port_in_use`; a published host-port conflict is reported by Docker before Python starts. Choose another free `AI_CALLBACK_PORT` in that case.

Protected application storage lives in `data_dir/credentials/`: the token set, a separate registration mapping, and a stable host identifier. The directory is `0700`; atomic replacement files start as `0600`, flush and fsync before replacement. Tokens remain outside learning tables and browser storage. Access, refresh, retained ID token, grant scopes and expiry are saved together. The optional `id_token_hint` is deliberately omitted from authorization URLs returned to the UI, so returning authorization uses the account selector.

One token manager serializes refresh for the single backend process, rereads the latest saved token set and atomically persists each rotation. Confirmed unusable refresh grants remove the unusable token file while retaining the registration. Temporary failures preserve credentials. A missing plan permission is visible as `reauth_required` with `plan_permission_missing`. Disconnect invalidates the local token source, removes tokens and cancels active proof tasks; it retains the issued client/account mapping and host identifier for later sign-in. Multiple backend workers are unsupported. Remote session revocation is deferred; local disconnect does not claim that OpenAI's remote session was revoked. [Official refresh and session lifecycle](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions).

## Agent and actual MCP

`uv run --frozen --no-dev python -m backend.mcp_server` serves Streamable HTTP on container port 8001 at `/mcp`. Its single read-only `get_proof_context` validates the related material/cue/token IDs and returns the authored sentence “She gave up smoking.” This is a feasibility fixture; later learner/database tools remain separate tickets.

The named `subscription_contextual_card_proof` Agent obtains tool descriptions through Pydantic AI's actual `MCPToolset`. The model selects the tool, the host executes its HTTP MCP call, and the result reaches the next model turn. `output_type=ContextualCard` validates translation, explanation, expression and example. A final output without a successful context call is rejected. Each run has fresh selection dependencies, a request ID and tool traces. Catalog lookup, refresh and inference share a 30-second bound. At most four model turns and three ordinary tool calls are permitted; SDK retries are disabled.

Responses expose only the validated card and request/model/tool timing metadata. Logs record request ID, tool name, success and duration; they do not record prompt bodies, full subtitles, conversations or tokens. No hosted telemetry is configured, as SPEC §12.3 requires. The example is an exercise, not an attributed video quote.

## Controlled evidence

Run `uv sync --frozen`, then `uv run pytest backend/tests/test_ai_connection.py backend/tests/test_siwc.py backend/tests/test_ai_proof.py -q`. These are controlled remote-model responses. No test uses a real subscription. The MCP tests launch a separate official SDK server process and communicate over local HTTP/TCP.

The checks cover callback readiness/state/PKCE/denial/cancellation/timeout/port conflict, restart/disconnect and protected atomic storage, signed JWT identity/nonce/audience/expiry/signature rejection, declined plan permission, reuse of the issued client ID and stable host identity, two concurrent requests sharing one refresh, the actual OpenAI SDK's supported SIWC wire request and final output parsing, rejection of interrupted/incomplete/late-failed streams, MCP ID validation and output-schema retry.

One combined test exercises the complete controlled path: **official SDK request → SIWC tool declaration → model-selected real HTTP MCP call → returned cue in the next SDK request → validated output function**. This verifies the adapter and Agent/MCP integration together. It does not establish account eligibility or live provider success.

## Host sign-in retry and network evidence

The learner attempted sign-in and reported “failed to authorize” on the OpenAI sign-in page. That provider-page failure has no confirmed cause. Two independently reproduced local faults were corrected before requesting a fresh attempt:

- A completed callback left its fixed listener port in TIME_WAIT. The original public sequence returned start 200, controlled denial 400, immediate retry 409. Setting `SO_REUSEADDR` before bind changed the sequence to 200/400/200 on two actual Compose runs, while an occupied listener still returns 409. Callback failures now expose only a bounded category, never exception text or provider payloads.
- The host could reach OpenAI through its existing network, while the Colima container could not resolve/reach the OpenAI origins. The optional `compose.network.yaml` routes HTTPS through `scripts/openai-connect-relay.py`, restricted to `auth.openai.com:443` and `api.openai.com:443`, and mounts the host's existing public CA bundle read-only. The learner explicitly approved using that trust for OAuth. TLS verification remains enabled; the relay forwards encrypted bytes and does not log requests. MCP remains direct and internal.

From the final configured app container, public OIDC discovery returned HTTP 200 with the expected issuer. A `/v1/models` request using the actual `SIWCTransport` and a public dummy bearer returned the expected unauthenticated HTTP 401. These checks used no account credentials and establish routing and certificate verification only. The explicit SIWC transport now honors `HTTPS_PROXY`, `NO_PROXY` and `SSL_CERT_FILE`; default Compose still uses direct networking.

After these fixes, the full suite passed 55 tests in 12.15 seconds; strict mypy passed for 15 sources and Ruff passed for backend and relay helper. Independent Standards and Spec reviews of `ee8a394..78b8291` found no actionable issues. The learner's fresh sign-in succeeded. The successful retry does not establish which fault caused the original sign-in-page message.

## Live subscription and persistence evidence

The learner completed **Continue with ChatGPT** from the Compose UI at `http://127.0.0.1:18765`, returning through host loopback callback port 1456. The backend reported `connected`; no API key was supplied. Two explicit `POST /api/ai/proof` requests returned HTTP 200 through SIWC with catalog-selected `gpt-6.1-sol` and Pydantic AI 2.57.0/OpenAI SDK 3.35.1. The provider exposed that model slug; a separate immutable model snapshot was not reported.

| Run | Request ID | Model requests | Real `get_proof_context` call | Total Agent time |
| --- | --- | --- | --- | --- |
| Connected application | `7de7e619-d380-4421-8b09-5d50d1df31c9` | 2 | success, 19 ms | 8,288 ms |
| After app container recreation | `637d3181-b848-4432-a68b-3214994ace2f` | 2 | success, 7 ms | 10,210 ms |

Both runs returned nonempty translation, explanation, English expression and English example validated by `ContextualCard`. The first model turn selected the MCP context tool; the second produced the structured card from its result. The MCP service ran separately inside the Compose network without a published port. The fixture was the authored sentence documented above, not an imported learner material. These timings exceed the full card feature's five-second target; they are recorded feasibility measurements, not a claim that the later performance criterion passed.

The app container was force-recreated between the two requests. On startup it reported `connected` with `live_verified:false`; the next real proof succeeded and set `live_verified:true` without another browser authorization. Only the public connection status and proof result metadata were inspected. Persistent credential contents were not opened, and the learner's connection remains available in the running application.

## Remaining acceptance

The host-to-container OAuth callback, account serving eligibility, live structured output, real model-selected MCP call and connection persistence now have evidence for ticket #1. Live refresh/revocation/quota behavior, browser callback replay timing and disconnect during live work still need acceptance under the later lifecycle ticket. Remote sign-out revocation is not implemented. Full diagnosis, contextual cards for imported materials, chat and their complete MCP toolsets belong to later tickets; the authored fixture does not satisfy those full MVP scenarios or all of A16/A17.
