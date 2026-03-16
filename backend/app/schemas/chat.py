"""
app/schemas/chat.py
────────────────────
Pydantic v2 schemas for the Chat API.
These are the frontend ↔ backend contract — keep stable.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class VerificationStatus(str, Enum):
    GENUINE = "genuine"
    FAKE = "fake"
    EXPIRED = "expired"
    SUSPENDED = "suspended"
    CATEGORY_MISMATCH = "category_mismatch"
    NOT_FOUND = "not_found"
    UNVERIFIED = "unverified"


class ToolCallRecord(BaseModel):
    """Record of a single tool call the agent made — for transparency."""
    tool_name: str
    input_summary: str       # short human-readable description
    outcome: str             # "success" | "empty" | "error"
    result_preview: str = "" # first 120 chars of result


class UnverifiedItem(BaseModel):
    """Something the agent tried to check but couldn't confirm."""
    item: str
    reason: str


class ResponseMetadata(BaseModel):
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    unverified: list[UnverifiedItem] = Field(default_factory=list)
    iterations_used: int = 0
    hit_max_iterations: bool = False
    processing_time_ms: int = 0


# ── Session schemas ───────────────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionResponse(BaseModel):
    session_id: str
    created_at: datetime
    expires_at: datetime | None   # None for authenticated sessions


# ── Message schemas ───────────────────────────────────────────────────────────

class SendMessageRequest(BaseModel):
    session_id: str
    message: str = Field(..., min_length=1, max_length=3000)

    @field_validator("session_id")
    @classmethod
    def validate_uuid(cls, v: str) -> str:
        try:
            uuid.UUID(v)
        except ValueError as exc:
            raise ValueError("session_id must be a valid UUID") from exc
        return v

    @field_validator("message")
    @classmethod
    def strip_message(cls, v: str) -> str:
        return v.strip()


# ── SSE stream chunk (frontend reads these) ───────────────────────────────────

class StreamChunk(BaseModel):
    """
    SSE event payload.
    type: "token" | "metadata" | "error" | "done" | "tool_status"
    tool_status: when type=="tool_status" — {"tool": str, "status": "running"|"done", "message": str}
    """
    type: str
    content: str = ""
    metadata: ResponseMetadata | None = None
    tool_status: dict | None = None
