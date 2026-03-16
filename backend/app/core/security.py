"""
app/core/security.py
─────────────────────
Password hashing (bcrypt) and JWT creation / verification.
No email verification — issue token immediately on register.

Uses bcrypt directly (not passlib) to avoid passlib/bcrypt 4.1+ compatibility
issues. Bcrypt has a 72-byte limit; we truncate to 72 bytes before hashing.
"""
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.core.config import get_settings
from app.core.exceptions import AuthError

settings = get_settings()

# Bcrypt limit; we truncate to avoid ValueError from the library
_BCRYPT_MAX_PASSWORD_BYTES = 72


# ── Passwords ─────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    raw = plain.encode("utf-8")
    if len(raw) > _BCRYPT_MAX_PASSWORD_BYTES:
        raw = raw[:_BCRYPT_MAX_PASSWORD_BYTES]
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    raw = plain.encode("utf-8")
    if len(raw) > _BCRYPT_MAX_PASSWORD_BYTES:
        raw = raw[:_BCRYPT_MAX_PASSWORD_BYTES]
    return bcrypt.checkpw(raw, hashed.encode("utf-8"))


# ── JWT ───────────────────────────────────────────────────────────────────────

def create_access_token(payload: dict[str, Any]) -> str:
    """Create a signed JWT. 'sub' should be the user_id string."""
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.jwt_expire_minutes
    )
    return jwt.encode(
        {**payload, "exp": expire},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def decode_token(token: str) -> dict[str, Any]:
    """Decode and verify a JWT. Raises AuthError on any failure."""
    try:
        return jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as exc:
        raise AuthError(f"Invalid or expired token: {exc}") from exc
