"""Application-owned subscription connection, independent of Codex CLI credentials."""

from .router import create_ai_router

__all__ = ["create_ai_router"]
