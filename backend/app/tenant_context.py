"""Internal MCP identity propagated with each request and its background tasks."""

from contextvars import ContextVar

current_mcp_token: ContextVar[str | None] = ContextVar("current_mcp_token", default=None)


def mcp_headers() -> dict[str, str]:
    token = current_mcp_token.get()
    return {"Authorization": f"Bearer {token}"} if token else {}
