"""
app/core/exceptions.py
───────────────────────
Domain exception hierarchy.
All exceptions map to HTTP responses in main.py handlers.
"""
from typing import Any


class BISError(Exception):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class AuthError(BISError):
    """Invalid credentials or expired token."""

class UserExistsError(BISError):
    """Email already registered."""

class SessionNotFoundError(BISError):
    """Session missing or TTL-expired."""

class DatabaseError(BISError):
    """MongoDB operation failed."""

class EmbeddingError(BISError):
    """Gemini embedding call failed."""

class VectorSearchError(BISError):
    """Atlas vector search failed."""

class WebSearchError(BISError):
    """Tavily search failed."""

class ScraperError(BISError):
    """Playwright page scrape failed."""

class DomainNotAllowedError(BISError):
    """URL domain not in the allowed whitelist."""

class AgentError(BISError):
    """LangGraph agent execution failed."""

class LLMError(BISError):
    """Gemini generation call failed."""
