"""Authenticated internal context for existing domain/MCP integration tests."""

from backend.app.accounts import AccountInput, AccountStore
from backend.app.tenant_context import current_mcp_token


def mcp_account(directory):
    store = AccountStore(directory)
    user = store.register(AccountInput(username="mcp-test", password="test-password-123"))
    with store.connect() as db:
        token = db.execute("SELECT mcp_token FROM accounts WHERE id=?", (user["id"],)).fetchone()[0]
    context = current_mcp_token.set(token)
    return context, store.learner_directory(user["id"]) / "learning.sqlite3"
