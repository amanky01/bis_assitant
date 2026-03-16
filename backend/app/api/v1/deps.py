"""
app/api/v1/deps.py
───────────────────
FastAPI dependency functions.

get_optional_user:
  - Reads Authorization: Bearer <token> header
  - If present and valid → returns user_id str
  - If absent or invalid → returns None (anonymous)
  - Never raises — auth is optional everywhere
"""
from __future__ import annotations

from fastapi import Header
from typing import Optional

from app.core.security import decode_token
from app.core.exceptions import AuthError
from app.core.logging import get_logger

logger = get_logger(__name__)


async def get_optional_user(
    authorization: Optional[str] = Header(default=None)
) -> str | None:
    """
    Returns user_id if a valid JWT is present, None otherwise.
    Anonymous requests pass through without error.
    """
    if not authorization:
        return None
    if not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = decode_token(token)
        return payload.get("sub")
    except AuthError:
        # Invalid token — treat as anonymous
        return None
