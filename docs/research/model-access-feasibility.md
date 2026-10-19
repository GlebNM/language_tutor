# Model access feasibility

Checked on 19 October 2026 for the model-access assumptions originating in SPEC.md §§9.4 and 12.2.

**Current scope:** the user has confirmed local Docker Compose for one learner, a shared learning profile across the standalone web app and Chrome extension, and subscription-only model access. Application login, profile switching, public hosting, API-key support, and AI spending tracking are outside MVP scope. This revision focuses on the subscription connection and the browser callback across Docker networking. Earlier public-hosting findings are retained briefly as superseded context.

This is documentation and source research. No credentials were read, dependencies installed, or containers or live model requests run. No subscription login, refresh, structured response, or MCP acceptance test has run for this project. The possible arrangements below are candidates for testing, not product decisions.

The named Pydantic AI Codex classes exist in a current release. Two details still need resolution: its default callback helper binds inside the process's network namespace, and its Codex integration differs from OpenAI's documented Sign in with ChatGPT (SIWC) integration for third-party apps.

## Verified facts

### Available library version

Pydantic AI **2.57.0**, released on 16 October 2026, is a published candidate for the technical spike. Its tagged source includes `OpenAICodexModel`, `OpenAICodexProvider`, `OpenAICodexOAuthFlow`, the MCP capability, and MCP toolsets. Package metadata requires Python 3.11+, `openai>=3.26.0` for its OpenAI extra, and `fastmcp-slim[client]>=3.3.0,<5` for its MCP extra. A focused dependency declaration can begin with `pydantic-ai-slim[openai,mcp]==2.57.0`; a resolved lock and runtime compatibility still need testing. [Release](https://github.com/pydantic/pydantic-ai/releases/tag/v2.57.0), [tagged dependency metadata](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/pydantic_ai_slim/pyproject.toml), [published package metadata](https://pypi.org/pypi/pydantic-ai-slim/2.57.0/json).

The repo currently has no Python dependency manifest or lock. The installed skill's `1.1.2` metadata is the skill version, so it does not pin the Pydantic AI library.

### What Pydantic's Codex integration implements

Pydantic documents the three named classes and the fixed default callback `http://localhost:1455/auth/callback`. Browser opening and credential persistence belong to the application. Its credential-source interface loads credentials and saves replacements after refresh. The default CLI credential loader reads the Codex credential file without persisting refreshes to that file. [Pydantic Codex guide](https://pydantic.dev/docs/ai/models/openai-codex/).

Tagged source confirms that this integration uses the official Codex CLI's public client registration, `https://auth.openai.com/oauth/authorize`, `https://auth.openai.com/oauth/token`, and `https://chatgpt.com/backend-api/codex`. The login requests `openid profile email offline_access`. The provider manages one credential set and refresh lock per instance; sharing an HTTP client with another authenticated provider is rejected. Its storage reread is best effort across processes, rather than a lock across processes. [Codex provider source](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/pydantic_ai_slim/pydantic_ai/providers/openai_codex.py).

State and S256 PKCE are implemented. The callback helper ignores a mismatched state and surfaces authorization errors. The caller supplies the overall timeout, for example through `anyio.fail_after`. [OAuth helper source](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/pydantic_ai_slim/pydantic_ai/providers/_oauth.py).

## Docker callback facts

The browser interprets a loopback redirect on the computer running that browser. OpenAI makes this explicit in its self-hosted guide. A browser on the user's computer therefore reaches the host's `localhost` or `127.0.0.1`; it does not directly address a container's loopback interface. [OpenAI self-hosted guide](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms).

Docker Desktop publishes ports by listening on a host port and forwarding to the container's internal network IP and port. A Compose mapping such as `127.0.0.1:1455:1455` controls the **host** listening address; it does not change the Python server's listening address inside the container. [Docker Desktop routing](https://docs.docker.com/desktop/features/networking/#how-exposed-ports-work), [Compose ports](https://docs.docker.com/reference/compose-file/services/#ports).

Pydantic's `exchange_code_from_callback()` has no separate bind-address argument. It derives both its listening address and callback path from `flow.redirect_uri`, then creates a Python `HTTPServer` there. **Inference from source and Docker's documented routing:** in a standard bridge-network container, its default container-loopback listener will not receive traffic forwarded to the container network IP. Publishing port 1455 alone does not resolve that mismatch. [Tagged callback implementation](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/pydantic_ai_slim/pydantic_ai/providers/_oauth.py), [Docker routing](https://docs.docker.com/desktop/features/networking/#how-exposed-ports-work).

The callback process is **not required to run on the host**. What must be reachable is the host loopback URL used by the browser. The following are distinct arrangements to investigate:

| Arrangement | What the sources establish; what remains to test |
| --- | --- |
| Application-owned callback inside the container | `flow.exchange_code(code)` supports calling the exchange from an application's own callback handler. This permits investigating a container listener on a reachable interface while publishing only host loopback and keeping the registered redirect URI unchanged. The application must validate the returned state and OAuth error before exchanging the code. Reachability and isolation need an end-to-end test. |
| Listener on the host | A host listener naturally receives the host-loopback redirect. Connecting its result to the container is additional application integration; the sources do not supply that bridge or select it for this project. |
| Docker host networking | Docker documents shared networking on Linux and an opt-in feature in Docker Desktop 4.34+. Published ports are ignored. Desktop has TCP/UDP and isolation limitations, so it is not a portable default that can be assumed without testing the intended platform. |

The first two rows are implementation inferences from the callback API and forwarding topology. The third is documented Docker functionality. None has been tried here. [Pydantic callback/code APIs](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/pydantic_ai_slim/pydantic_ai/providers/openai_codex.py#L350), [Docker host networking](https://docs.docker.com/engine/network/drivers/host/).

Docker supports publishing on host loopback, while the service may listen on its container network interface. These are different boundaries. Changing the **OAuth redirect** to `0.0.0.0`, a Compose service name, or `host.docker.internal` is not a documented substitute for the registered browser-loopback URI. [Docker port publishing](https://docs.docker.com/engine/network/port-publishing/), [Codex redirect contract](https://pydantic.dev/docs/ai/models/openai-codex/).

### Browser URL, manual code exchange, and device login

The library exposes `authorization_url()` so the application can present the login URL to the browser running on the host. Browser opening is application-owned; invoking a browser opener inside a container is not established by these sources as a way to open the user's host browser. `exchange_code(code)` accepts the code directly; there is no documented convenience method here for pasting and validating a full callback URL. A manual-paste fallback would be an application feature with its own state validation and usability checks. [Pydantic login guide](https://pydantic.dev/docs/ai/models/openai-codex/#logging-in-without-the-codex-cli), [code-exchange implementation](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/pydantic_ai_slim/pydantic_ai/providers/openai_codex.py#L397).

**Documentation discrepancy:** Pydantic's guide and v2.57.0 class describe no device flow for their Codex login. Current official Codex CLI documentation does offer `codex login --device-auth` in beta, subject to account/workspace settings. It also documents copying an auth cache into Docker or forwarding the callback over SSH. These are official **CLI** alternatives; the tagged Pydantic flow does not expose a device-login API. CLI support does not establish an existing Pydantic device implementation, an app-UI login, or compatibility with SIWC dynamic registrations. [Official Codex authentication](https://learn.chatgpt.com/docs/auth#login-on-headless-devices), [Pydantic limitations](https://pydantic.dev/docs/ai/models/openai-codex/#limitations).

### OpenAI's documented third-party subscription path differs

The current SIWC registration guide requires dynamic client registration for a new account, a stable `ext_agent_host_id`, an app name, an issued client ID saved per registration, an OIDC nonce, ID-token validation, and the granted `chatgpt.tokens.use.direct` permission. It uses `/api/accounts/authorize` and `/api/accounts/oauth/token`. Its loopback callback uses `127.0.0.1`, explicitly forbids substituting `localhost`, and permits changing the port while retaining the scheme, host, and path. [SIWC registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in).

For inference, OpenAI specifies `https://api.openai.com/v1/responses` and explicitly says to avoid ChatGPT `backend-api` endpoints for this flow. Requests use `store:false`, `stream:true`, and a model available to the selected account. A request is successful only after `response.completed`; a failure can arrive after text starts streaming. [SIWC models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference).

**Significant mismatch:** the named Pydantic Codex classes implement a different client registration, callback contract, token exchange, and inference endpoint. Their existence does not prove that they implement SIWC. Replacing `localhost` with `127.0.0.1` in the Codex flow would conflict with that Codex client's fixed redirect registration. Conversely, SIWC's allowed variable port does not apply to the fixed Codex registration. OpenAI's SIWC guide specifies the browser redirect and starting a listener before opening the browser; it does not specify a Docker bind address or publish configuration. [Pydantic Codex guide](https://pydantic.dev/docs/ai/models/openai-codex/), [SIWC sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in), [SIWC inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference).

## Structured output and real MCP

Pydantic's MCP client documentation demonstrates a real Streamable HTTP server, a model-selected tool call, execution through MCP, and a second model request with the result. The model in that example uses API credentials, so it does not establish the same complete scenario through a live subscription. [Pydantic MCP client](https://pydantic.dev/docs/ai/mcp/client/#streamable-http).

Pydantic's `output_type=SomeModel` normally uses an output tool whose parameters follow the output schema and are validated by Pydantic. Native JSON-schema output is a separate option. OpenAI documents schema-constrained output and function calling on compatible models. [Pydantic output modes](https://pydantic.dev/docs/ai/core-concepts/output/#output-modes), [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses).

For **Pydantic's existing Codex provider**, `OpenAICodexModel` inherits the Responses model implementation. The release includes recorded integration tests for an ordinary local function call, tool execution, and the response consuming its result. These tests are upstream evidence; they do not prove this project's structured schemas or MCP server work through a live subscription. [Codex model source](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/pydantic_ai_slim/pydantic_ai/models/openai_codex.py), [upstream recorded tool tests](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/tests/models/test_openai_codex.py).

For **official SIWC**, OpenAI supports function/custom tools supplied in namespaces or `additional_tools` input items. Hosted MCP/connectors and Responses `tool_search` are unsupported. A Pydantic host calling its own private MCP server fits the client execution pattern in principle: the model receives function descriptions, and the host performs MCP calls. This is an **inference**, with a remaining adapter question about SIWC's exact wire format and Pydantic output tools. A simple OAuth-token swap is insufficient evidence. [SIWC preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations), [Pydantic MCP capability](https://pydantic.dev/docs/ai/capabilities/mcp/).

## Availability and superseded public context

OpenAI documents ChatGPT plan usage for open-source tools and personal projects run locally, with eligible Plus and Pro users. Account eligibility and available models must be checked for the chosen connection. The sources do not determine contractual permission for this application's separate Codex CLI backend path. [OpenAI SIWC cookbook](https://developers.openai.com/cookbook/articles/sign-in-with-chatgpt).

Public hosted delivery and public provider selection are outside the confirmed MVP. For future reference only: OpenAI asks paid or remotely hosted apps to request access before offering SIWC; ordinary API credentials have a separate Pydantic provider. Neither a hosted approval task nor a public spending comparison follows from the current local-only scope. [OpenAI cookbook](https://developers.openai.com/cookbook/articles/sign-in-with-chatgpt), [Pydantic OpenAI provider](https://pydantic.dev/docs/ai/models/openai/#configure-the-provider).

## Questions the empirical spike must answer

1. Resolve the subscription integration contract: official SIWC or the distinct Codex provider path. A successful technical request alone cannot settle that distinction.
2. Verify a real host-browser redirect into the chosen local Compose arrangement. Establish listener readiness before opening login; check an occupied 1455 port, IPv4/IPv6 resolution, denial, wrong state, repeated callback, cancellation, and timeout cleanup. Verify host-loopback publication and that changing the container listener leaves the registered redirect unchanged.
3. Resolve and lock Pydantic AI, OpenAI SDK, FastMCP client, and the server SDK together. Complete a validated card/diagnostic schema and a model-selected real MCP round trip using the local subscription; record versions, model, timing, and completion evidence.
4. Verify credentials survive container recreation through protected application storage, refresh replacements persist atomically, disconnect stops further use, and expired/revoked connections return the expected state. If CLI credentials are imported, establish which process owns refresh rather than assuming multiple rotating copies stay valid.
5. Run concurrent requests from different tabs and conversations, plus refresh races for the single subscription connection. Verify each request retains its material and conversation context, and that refresh coordination matches the actual number of backend processes. Application account and profile isolation tests are outside the confirmed single-learner scope.
