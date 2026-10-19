# Subscription connection acceptance

This note records the automated evidence for issue #13. It does not claim that the live subscription flow has passed.

## Automated evidence

Run from the repository root:

```sh
.venv/bin/pytest backend/tests/test_ai_connection.py backend/tests/test_ai_proof.py -q
.venv/bin/mypy backend/app/ai_connection backend/app/local_access.py
```

On 19 October 2026, the focused suite passed with 28 tests. Mypy reported no issues in 8 source files, and Ruff reported no issues in the two focused test files.

The tests cover OAuth denial, incorrect state, repeated callback rejection, cancellation, timeout and retry, occupied callback port, bounded failure categories without leaking provider or exception messages, credentials surviving an application restart, protected file and directory permissions, disconnection blocking further AI requests, refresh-token invalidation, and provider authorization/quota/connectivity errors. The concurrent-refresh case verifies eight simultaneous requests perform one token rotation and all receive the persisted replacement token. The SIWC proof test exercises a controlled subscription transport with a real local MCP server and structured Pydantic AI output. The local access tests cover allowed and foreign origins. `compose.yaml` publishes the web app on `127.0.0.1` and stores `/data` in the persistent `learning-data` volume; `compose.oauth.yaml` publishes the callback on loopback.

## Remaining checks

- Assert that profile, materials, familiarity, and chat data remain available after disconnect and reconnect.
- Add an explicit expired callback assertion; repeated callback rejection is covered.
- Complete a real host-browser OAuth callback through the final Docker Compose setup, then recreate the app container and verify a live subscription request still succeeds.

The current tests establish process-restart persistence against the same credential directory. They do not establish persistence through actual Docker container recreation or prove a live subscription call after that recreation.
