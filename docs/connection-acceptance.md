# Subscription connection acceptance

This note records the automated evidence for issue #13 and the live continuation verified in this session. The earlier host OAuth callback is recorded in [subscription-proof.md](subscription-proof.md); the final learning journeys are recorded in [acceptance.md](acceptance.md).

## Automated evidence

Run from the repository root:

```sh
.venv/bin/pytest backend/tests/test_ai_connection.py backend/tests/test_ai_proof.py -q
.venv/bin/mypy backend/app/ai_connection backend/app/local_access.py
```

On 19 October 2026, the focused suite passed with 28 tests. Mypy reported no issues in 8 source files, and Ruff reported no issues in the two focused test files.

The tests cover OAuth denial, incorrect state, repeated callback rejection, cancellation, timeout and retry, occupied callback port, bounded failure categories without leaking provider or exception messages, credentials surviving an application restart, protected file and directory permissions, disconnection blocking further AI requests, refresh-token invalidation, and provider authorization/quota/connectivity errors. The concurrent-refresh case verifies eight simultaneous requests perform one token rotation and all receive the persisted replacement token. The SIWC proof test exercises a controlled subscription transport with a real local MCP server and structured Pydantic AI output. The local access tests cover allowed and foreign origins. `compose.yaml` publishes the web app on `127.0.0.1` and stores `/data` in the persistent `learning-data` volume; `compose.oauth.yaml` publishes the callback on loopback.

## Live continuation and limits

The public data-reset tests now assert that disconnect preserves profile, materials and manual familiarity while blocking new AI requests. Persistent conversations and history survive application restart in the learning API tests. Timeout and repeated callback tests cover expired and consumed attempts without exchanging a code again.

On 19 October 2026 the final Compose app and MCP containers were rebuilt and recreated repeatedly. The existing app-owned subscription remained connected, and new live cards, a diagnostic completion, and chat with model-selected real MCP calls succeeded. The browser also confirmed saved manual familiarity and imported materials after recreation. The actual refresh/revocation failure matrix uses controlled responses; no remote grant was deliberately revoked and no new host consent was requested during this session.
