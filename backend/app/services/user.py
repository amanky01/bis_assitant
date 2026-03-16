"""
app/services/user.py
─────────────────────
User registration and lookup.
No email verification — JWT issued immediately on register.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from app.core.config import get_settings
from app.core.exceptions import AuthError, UserExistsError
from app.core.security import create_access_token, hash_password, verify_password
from app.db.mongo import MongoDB

settings = get_settings()


class UserService:

    async def register(self, email: str, password: str) -> dict[str, Any]:
        """
        Create a new user. Raises UserExistsError if email taken.
        Returns {user_id, email, access_token}.
        """
        col = MongoDB.col(settings.col_users)

        if await col.find_one({"email": email.lower()}):
            raise UserExistsError(f"Email already registered: {email}")

        user_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        await col.insert_one({
            "user_id": user_id,
            "email": email.lower(),
            "password_hash": hash_password(password),
            "created_at": now,
        })

        token = create_access_token({"sub": user_id, "email": email.lower()})
        return {"user_id": user_id, "email": email.lower(), "access_token": token}

    async def login(self, email: str, password: str) -> dict[str, Any]:
        """
        Verify credentials. Raises AuthError on failure.
        Returns {user_id, email, access_token}.
        """
        col = MongoDB.col(settings.col_users)
        user = await col.find_one({"email": email.lower()})

        if not user or not verify_password(password, user["password_hash"]):
            raise AuthError("Invalid email or password")

        token = create_access_token(
            {"sub": user["user_id"], "email": user["email"]}
        )
        return {
            "user_id": user["user_id"],
            "email": user["email"],
            "access_token": token,
        }

    async def get_by_id(self, user_id: str) -> dict[str, Any] | None:
        col = MongoDB.col(settings.col_users)
        return await col.find_one({"user_id": user_id}, {"_id": 0, "password_hash": 0})
