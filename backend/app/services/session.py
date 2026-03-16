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

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.core.config import get_settings
from app.core.exceptions import SessionNotFoundError
from app.core.logging import get_logger
from app.db.mongo import MongoDB
from app.schemas.chat import MessageRole

logger = get_logger(__name__)
settings = get_settings()


class SessionService:

    # ── Session CRUD ──────────────────────────────────────────────────────────

    async def create_session(
        self,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """
        Create session. Anonymous → TTL expires_at set.
        Authenticated → expires_at None (persistent).
        """
        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        expires_at = (
            None if user_id
            else now + timedelta(hours=settings.session_ttl_hours)
        )

        await MongoDB.col(settings.col_sessions).insert_one({
            "session_id": session_id,
            "user_id": user_id,
            "created_at": now,
            "updated_at": now,
            "expires_at": expires_at,
            "message_count": 0,
            "metadata": metadata or {},
        })
        logger.debug(f"Session created: {session_id} (user={user_id})")
        return session_id

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

    async def save_message(
        self,
        session_id: str,
        user_id: str | None,
        role: MessageRole,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await MongoDB.col(settings.col_messages).insert_one({
            "session_id": session_id,
            "user_id": user_id,
            "role": role.value,
            "content": content,
            "metadata": metadata or {},
            "created_at": datetime.now(timezone.utc),
        })

    async def get_window(self, session_id: str) -> list[BaseMessage]:
        """
        Fetch last N messages as LangChain BaseMessage objects.
        Fetched latest-first, reversed to chronological.

        We drop the last full exchange (last user + last assistant) so that when
        we append the current user message in run_agent, the model sees:
          [ ... prior exchanges ..., HumanMessage(current) ]
        i.e. the last message is always the current query. This avoids the model
        echoing the previous reply or confusing which message to answer (no two
        consecutive user messages). Best practice: context ends with an assistant
        message (or empty); the only "to answer" message is the current user.
        """
        cursor = (
            MongoDB.col(settings.col_messages)
            .find(
                {"session_id": session_id},
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
        # Drop the last exchange (last assistant, then last user) so we don't send
        # the previous Q&A; run_agent will append only the current user message.
        if messages and isinstance(messages[-1], AIMessage):
            messages.pop()
        if messages and isinstance(messages[-1], HumanMessage):
            messages.pop()
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
