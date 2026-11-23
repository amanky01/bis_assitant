"""
app/services/session.py
────────────────────────
Session lifecycle and windowed message history.

Anonymous sessions:
  - expires_at set at creation → MongoDB TTL index auto-deletes
  - user_id = None

Authenticated sessions:
  - expires_at = None → TTL index partial filter skips them (persistent)
  - user_id = str UUID

Sliding window:
  - fetch last SESSION_WINDOW_SIZE messages for LLM context
  - messages stored separately (queryable, trimmable)
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.core.config import get_settings
from app.core.exceptions import SessionNotFoundError
from app.db.mongo import MongoDB
from app.schemas.chat import MessageRole

settings = get_settings()


class SessionService:

    # ── Session CRUD ──────────────────────────────────────────────────────────

    async def create_session(
        self,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[str, str | None]:
        """
        Create session. Anonymous → TTL expires_at set.
        Authenticated → expires_at None (persistent).
        """
        session_id = str(uuid.uuid4())
        guest_token = None if user_id else secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires_at = (
            None if user_id
            else now + timedelta(hours=settings.session_ttl_hours)
        )

        await MongoDB.col(settings.col_sessions).insert_one({
            "session_id": session_id,
            "user_id": user_id,
            "guest_token_hash": self._guest_hash(guest_token) if guest_token else None,
            "created_at": now,
            "updated_at": now,
            "expires_at": expires_at,
            "message_count": 0,
            "metadata": metadata or {},
        })
        return session_id, guest_token

    @staticmethod
    def _guest_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    async def get_authorized_session(
        self, session_id: str, user_id: str | None, guest_token: str | None
    ) -> dict[str, Any]:
        query: dict[str, Any] = {"session_id": session_id, "user_id": user_id}
        if user_id is None:
            if not guest_token or len(guest_token) > 128:
                raise SessionNotFoundError("Session not found or expired")
            query["guest_token_hash"] = self._guest_hash(guest_token)
            query["expires_at"] = {"$gt": datetime.now(timezone.utc)}
        doc = await MongoDB.col(settings.col_sessions).find_one(query)
        if doc is None:
            raise SessionNotFoundError("Session not found or expired")
        return doc

    async def get_session(self, session_id: str) -> dict[str, Any]:
        doc = await MongoDB.col(settings.col_sessions).find_one(
            {"session_id": session_id}
        )
        if not doc:
            raise SessionNotFoundError(f"Session not found: {session_id}")
        return doc

    async def set_session_title_if_empty(self, session_id: str, first_user_message: str) -> None:
        """Set session title from first user message (only when message_count is still 0)."""
        title = (first_user_message or "").strip()[:80]
        if not title:
            return
        await MongoDB.col(settings.col_sessions).update_one(
            {"session_id": session_id, "message_count": 0},
            {"$set": {"title": title}},
        )

    async def touch_session(self, session_id: str) -> None:
        """Reset updated_at and increment message count."""
        now = datetime.now(timezone.utc)
        await MongoDB.col(settings.col_sessions).update_one(
            {"session_id": session_id},
            {
                "$set": {"updated_at": now},
                "$inc": {"message_count": 1},
            },
        )

    async def end_session(self, session_id: str) -> None:
        await MongoDB.col(settings.col_sessions).delete_one(
            {"session_id": session_id}
        )
        await MongoDB.col(settings.col_messages).delete_many(
            {"session_id": session_id}
        )

    async def get_user_sessions(self, user_id: str) -> list[dict[str, Any]]:
        """Return all sessions for a logged-in user (for history UI). Includes title (first user message)."""
        cursor = (
            MongoDB.col(settings.col_sessions)
            .find(
                {"user_id": user_id},
                {"_id": 0, "session_id": 1, "created_at": 1, "message_count": 1, "title": 1}
            )
            .sort("created_at", -1)
            .limit(50)
        )
        return await cursor.to_list(length=50)

    # ── Messages ──────────────────────────────────────────────────────────────

    async def get_turn_message(self, message_id: str) -> dict | None:
        return await MongoDB.col(settings.col_messages).find_one({"_id": message_id})

    async def save_message(
        self,
        session_id: str,
        user_id: str | None,
        role: MessageRole,
        content: str,
        metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> None:
        session = await self.get_session(session_id)
        expires_at = session.get("expires_at")
        collection = MongoDB.col(settings.col_messages)
        document = {
            "session_id": session_id,
            "user_id": user_id,
            "expires_at": expires_at,
            "role": role.value,
            "content": content,
            "metadata": metadata or {},
            "created_at": datetime.now(timezone.utc),
        }
        if message_id is not None:
            document["_id"] = message_id
        inserted = await collection.insert_one(document)
        try:
            await self.get_session(session_id)
        except SessionNotFoundError:
            await collection.delete_one({"_id": inserted.inserted_id})
            raise

    async def get_window(self, session_id: str, exclude_id: str | None = None) -> list[BaseMessage]:
        """Fetch recent conversation messages in chronological order."""
        query = {"session_id": session_id}
        if exclude_id is not None:
            query["_id"] = {"$ne": exclude_id}
        cursor = (
            MongoDB.col(settings.col_messages)
            .find(
                query,
                {"_id": 0, "role": 1, "content": 1}
            )
            .sort("created_at", -1)
            .limit(settings.session_window_size)
        )
        raw = await cursor.to_list(length=settings.session_window_size)
        raw.reverse()

        messages: list[BaseMessage] = []
        for m in raw:
            content = m.get("content") or ""
            if m["role"] == MessageRole.USER.value:
                messages.append(HumanMessage(content=content))
            elif m["role"] == MessageRole.ASSISTANT.value:
                messages.append(AIMessage(content=content))
        return messages

    async def get_full_history(
        self, session_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Full message history for a session (for history UI)."""
        cursor = (
            MongoDB.col(settings.col_messages)
            .find(
                {"session_id": session_id},
                {"_id": 0, "role": 1, "content": 1, "created_at": 1, "metadata": 1}
            )
            .sort("created_at", 1)
            .limit(limit)
        )
        return await cursor.to_list(length=limit)
