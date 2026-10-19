# Local subscription proof — ticket #1

Checked on 19 October 2026. The implementation supplies a runnable candidate and controlled evidence. **Ticket #1 remains blocked on a supported access contract and a live host-browser/Compose subscription run. A16 and A17 have not passed.** No real subscription credentials, authorization codes, or Codex CLI credential files were read during implementation.

## Access contract

The executable candidate uses Pydantic AI's documented `OpenAICodexOAuthFlow`, `OpenAICodexProvider`, and `OpenAICodexModel`. This is the library's Codex public-client flow. The browser redirect stays exactly `http://localhost:1455/auth/callback`; only the application's container listener binds `0.0.0.0`. The provider uses Codex subscription inference, and receives an explicit application credential source. [Pydantic's Codex guide](https://pydantic.dev/docs/ai/models/openai-codex/), [v2.57.0 provider source](https://raw.githubusercontent.com/pydantic/pydantic-ai/v2.57.0/pydantic_ai_slim/pydantic_ai/providers/openai_codex.py).

Official Sign in with ChatGPT for local open-source applications describes dynamic client registration, a stable host identifier, validated OIDC identity, and granted `chatgpt.tokens.use.direct` permission. It specifies a `127.0.0.1` callback and separate authorization/token endpoints. Those registration rules do not describe this Codex candidate. [Official registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in).

Official SIWC inference uses the Responses API at `api.openai.com/v1` and explicitly directs clients away from ChatGPT `backend-api` endpoints. Exchanging a Codex token and changing the API URL would not implement that contract. This spike does **not** claim SIWC support or establish that OpenAI supports this third-party application's use of the Codex public registration. Resolve that question before closing #1 or starting tickets that depend on successful subscription feasibility. An official SIWC adapter would need its own registration, validation, refresh and wire-format work. [Official models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference).

## Resolved dependency set

`pyproject.toml` pins the integration libraries, and `uv.lock` locks all transitive dependencies. The Pydantic AI package release is 2.57.0; the local agent skill's version is unrelated. Python 3.12 keeps the language adapter and the AI host on one runtime. The controlled tests ran with CPython 3.12.11 on macOS arm64.

| Component | Version |
| --- | --- |
| Python runtime series | 3.12 |
| pydantic-ai-slim, pydantic-graph | 2.57.0 |
| OpenAI SDK | 3.35.1 |
| Official MCP server SDK | 1.30.0 |
| FastMCP slim client used by Pydantic AI | 3.4.9 |
| Pydantic | 2.14.1 |
| FastAPI / Uvicorn | 0.148.0 / 0.54.0 |
| spaCy / en_core_web_sm | 3.8.11 / 3.8.0 |

The published Pydantic release supports Python 3.11+, requires OpenAI 3.26.0 or later, and supports the selected FastMCP client range. The installed 2.57.0 signatures and runtime tests, including its `MCPToolset`, were checked directly. This version uses `result.usage` as a property and has no `instrument` Agent constructor argument. [Published release metadata](https://pypi.org/pypi/pydantic-ai-slim/2.57.0/json).

## Runnable boundary

The application includes `create_ai_router(data_dir=..., mcp_url=..., model_name=...)`. The default model is `gpt-5.6-luna`, taken from Pydantic's Codex example; `AI_MODEL` can select another model in the app configuration. Availability to the learner's subscription remains unverified.

| Operation | Route |
| --- | --- |
| Inspect connection | `GET /api/ai/connection` |
| Start browser login | `POST /api/ai/connection/start` |
| Cancel pending login | `POST /api/ai/connection/cancel` |
| Disconnect and remove local credentials | `DELETE /api/ai/connection` |
| Run the authored proof card via subscription and MCP | `POST /api/ai/proof` |

Start returns an authorization URL only after the callback socket is listening. The host UI opens that URL. The callback checks state before errors or code exchange; the library supplies S256 PKCE. A completed, denied, cancelled or timed-out attempt releases the listener. The wait expires after five minutes. Callback access logging is disabled so authorization codes are not logged. If 1455 is already occupied inside the backend's network namespace, start returns `callback_port_in_use` with a Russian explanation.

The default Compose setup must publish the app and callback only on host loopback and leave MCP unpublished. Use the README's callback-enabled Compose command when attempting browser authorization. Docker rejects a published host-port conflict before Python starts; the backend cannot catch that error. During this implementation, an unrelated local development container already owned host `127.0.0.1:1455`. It was left running. The library's fixed redirect cannot use another port, and no host-browser OAuth success was recorded.

Credential files live under the configured persistent data directory in `credentials/subscription.json`, outside learning tables and Codex CLI storage. The directory is `0700` and temporary files are created as `0600`; a completed atomic replacement saves all credential fields together. Disconnect invalidates the credential source, removes its file and cancels active proof tasks. The same provider instance serves the one backend process, sharing Pydantic's refresh lock. The service closes its HTTP client at shutdown. Multiple backend worker processes are unsupported; use one worker.

The proof server starts with `uv run --frozen --no-dev python -m backend.mcp_server`. It serves Streamable HTTP on container port 8001 at `/mcp`; `MCP_HOST` and `MCP_PORT` configure test/runtime binding. Its single read-only `get_proof_context` tool validates the three related IDs and returns the authored sentence “She gave up smoking.” It is deliberately a small feasibility fixture, with no learner database or future chat tools.

The named `subscription_contextual_card_proof` Agent obtains the tool descriptions through Pydantic AI's real `MCPToolset`. The model selects the tool, the host executes the HTTP MCP call, and the result reaches the next model turn. Pydantic validates translation, explanation, expression and example with `output_type=ContextualCard`. A final output without a successful context tool is rejected. Runs use fresh selection dependencies, request IDs and tool traces, with a 30-second total timeout, at most four model turns and three ordinary tool calls. SDK request retries are disabled. The one required tool is loaded eagerly; deferred discovery would not help this small proof.

Responses expose request ID, model, route, elapsed time, validated card, tool names, tool success and tool duration. Local tool logs contain only request ID and those tool measurements. No prompt bodies, conversations, full subtitles or tokens are logged, and no hosted telemetry is configured, as required by SPEC §12.3. The generated example is an exercise, not an attributed video quote.

## Controlled evidence

Run `uv sync --frozen` then `uv run pytest backend/tests/test_ai_connection.py backend/tests/test_ai_proof.py -q`. The suite uses a controlled Pydantic `FunctionModel` and forbids live model requests in the agent tests. A separate official MCP server subprocess is reached over actual local TCP/HTTP. No direct Python call replaces that transport.

The checks cover disconnected access, ready callback and PKCE URL, wrong state, denial, occupied callback port, cancellation, timeout and retry, application storage across restart, atomic credential rotation and permissions, disconnect preventing further saves, model-selected HTTP MCP calls and consumption of the returned sentence, invalid output retry, and rejection of unrelated material IDs over MCP. A valid controlled run takes two model turns; the invalid-output test takes three. These are controlled results, not subscription acceptance evidence.

## Remaining live evidence

No live model name, real subscription completion, production latency or live MCP trace is recorded. The required human acceptance is to resolve the supported access route, free the fixed callback port without disrupting another task, run Compose with callback publication, open connection from the UI, finish browser consent, run the proof, and record only model/version/request ID/completion/tool timing metadata. Check host-loopback reachability and IPv4/IPv6 behavior on the target Docker platform.

Real refresh, revocation, quota errors, repeated callback behavior under actual browser timing, disconnect during live work, and concurrent refresh requests need live acceptance. Application-owned rotation storage is tested; the provider's remote refresh and backend error mapping are not established by those tests. Full diagnosis, contextual cards for imported materials, learner chat and their complete MCP tools belong to later tickets. They are not implemented by this fixture.
