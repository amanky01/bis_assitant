"""
app/api/v1/chat.py
───────────────────
Chat API endpoints.

POST   /chat/sessions                    — create session (anon or auth)
GET    /chat/sessions/{id}               — session info
DELETE /chat/sessions/{id}               — end session
POST   /chat/sessions/{id}/message       — send message (SSE stream)
GET    /chat/sessions/{id}/history       — full message history (auth only)
GET    /chat/history                     — all sessions for logged-in user
"""
import time
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.agents.react_agent import run_agent
from app.api.v1.deps import get_optional_user
from app.core.config import get_settings
from app.core.exceptions import SessionNotFoundError
from app.core.logging import get_logger
from app.schemas.chat import (
    CreateSessionRequest,
    ResponseMetadata,
    SendMessageRequest,
    MessageRole,
    SessionResponse,
    StreamChunk,
)
from app.services.session import SessionService

logger = get_logger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


def _svc() -> SessionService:
    return SessionService()


# ── POST /chat/sessions ───────────────────────────────────────────────────────

@router.post(
    "/sessions",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a chat session",
)
async def create_session(
    body: CreateSessionRequest,
    user_id: str | None = Depends(get_optional_user),
    svc: SessionService = Depends(_svc),
) -> SessionResponse:
    session_id = await svc.create_session(user_id=user_id, metadata=body.metadata)
    session = await svc.get_session(session_id)
    return SessionResponse(
        session_id=session_id,
        created_at=session["created_at"],
        expires_at=session.get("expires_at"),
    )


# ── GET /chat/sessions/{session_id} ──────────────────────────────────────────

@router.get(
    "/sessions/{session_id}",
    response_model=SessionResponse,
    summary="Get session info",
)
async def get_session(
    session_id: str,
    svc: SessionService = Depends(_svc),
) -> SessionResponse:
    try:
        session = await svc.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    return SessionResponse(
        session_id=session_id,
        created_at=session["created_at"],
        expires_at=session.get("expires_at"),
    )


# ── DELETE /chat/sessions/{session_id} ───────────────────────────────────────

@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a session and its messages (auth: own sessions only)",
)
async def end_session(
    session_id: str,
    user_id: str | None = Depends(get_optional_user),
    svc: SessionService = Depends(_svc),
) -> None:
    if user_id:
        try:
            session = await svc.get_session(session_id)
            if session.get("user_id") != user_id:
                raise HTTPException(status_code=403, detail="Access denied")
        except SessionNotFoundError:
            raise HTTPException(status_code=404, detail="Session not found")
    await svc.end_session(session_id)


# ── POST /chat/sessions/{session_id}/message ─────────────────────────────────

@router.post(
    "/sessions/{session_id}/message",
    summary="Send a message — returns SSE stream",
)
async def send_message(
    session_id: str,
    body: SendMessageRequest,
    user_id: str | None = Depends(get_optional_user),
    svc: SessionService = Depends(_svc),
) -> StreamingResponse:
    if body.session_id != session_id:
        raise HTTPException(status_code=422, detail="session_id mismatch in URL vs body")

    try:
        await svc.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    return StreamingResponse(
        _stream(session_id, user_id, body.message, svc),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _stream(
    session_id: str,
    user_id: str | None,
    user_message: str,
    svc: SessionService,
) -> AsyncGenerator[str, None]:
    start = time.monotonic()
    logger.info("message session_id=%s input=%s", session_id[:8], user_message[:100])

    # Persist user message
    await svc.save_message(
        session_id, user_id, MessageRole.USER, user_message
    )
    await svc.set_session_title_if_empty(session_id, user_message)

    # Fetch sliding window (exclude the message we just saved)
    history = await svc.get_window(session_id)
    if history and hasattr(history[-1], "content") and history[-1].content == user_message:
        history = history[:-1]

    settings = get_settings()
    new_chat_message = (
        "This conversation is quite long. Please start a new chat for the best experience."
    )

    # When context is too long, ask the user to start a new chat instead of calling the agent
    if len(history) >= settings.session_max_messages_before_new_chat:
        for word in new_chat_message.split():
            chunk = StreamChunk(type="token", content=word + " ")
            yield f"data: {chunk.model_dump_json()}\n\n"
        chunk = StreamChunk(
            type="metadata",
            content="",
            metadata=ResponseMetadata(processing_time_ms=int((time.monotonic() - start) * 1000)),
        )
        yield f"data: {chunk.model_dump_json()}\n\n"
        done_chunk = StreamChunk(type="done", content="")
        yield f"data: {done_chunk.model_dump_json()}\n\n"
        await svc.save_message(
            session_id, user_id, MessageRole.ASSISTANT,
            new_chat_message, metadata={},
        )
        await svc.touch_session(session_id)
        return

    full_response_parts: list[str] = []
    final_metadata: ResponseMetadata | None = None

    try:
        async for event in run_agent(session_id, user_message, history):
            etype = event.get("type")

            if etype == "token":
                token = event.get("content", "")
                full_response_parts.append(token)
                chunk = StreamChunk(type="token", content=token)
                yield f"data: {chunk.model_dump_json()}\n\n"

            elif etype == "tool_status":
                chunk = StreamChunk(
                    type="tool_status",
                    content=event.get("content", ""),
                    tool_status=event.get("tool_status"),
                )
                yield f"data: {chunk.model_dump_json()}\n\n"

            elif etype == "metadata":
                raw = event.get("metadata", {})
                raw["processing_time_ms"] = int((time.monotonic() - start) * 1000)
                final_metadata = ResponseMetadata(**raw)
                chunk = StreamChunk(
                    type="metadata",
                    content="",
                    metadata=final_metadata,
                )
                yield f"data: {chunk.model_dump_json()}\n\n"

            elif etype == "error":
                chunk = StreamChunk(type="error", content=event.get("content", "Unknown error"))
                yield f"data: {chunk.model_dump_json()}\n\n"
                return

            elif etype == "done":
                chunk = StreamChunk(type="done", content="")
                yield f"data: {chunk.model_dump_json()}\n\n"

    except Exception as exc:
        logger.exception(f"Stream error for session {session_id}: {exc}")
        chunk = StreamChunk(type="error", content=str(exc))
        yield f"data: {chunk.model_dump_json()}\n\n"
        return

    # Persist assistant response (optionally truncated to limit storage and context size)
    full_response = "".join(full_response_parts)
    if full_response:
        max_chars = settings.session_assistant_save_max_chars
        to_save = (
            (full_response[:max_chars] + "…")
            if max_chars > 0 and len(full_response) > max_chars
            else full_response
        )
        meta_dict = final_metadata.model_dump() if final_metadata else {}
        await svc.save_message(
            session_id, user_id, MessageRole.ASSISTANT,
            to_save, metadata=meta_dict
        )
        await svc.touch_session(session_id)


# ── GET /chat/sessions/{session_id}/history ───────────────────────────────────

@router.get(
    "/sessions/{session_id}/history",
    summary="Full message history for a session (authenticated users only)",
)
async def get_history(
    session_id: str,
    user_id: str | None = Depends(get_optional_user),
    svc: SessionService = Depends(_svc),
) -> list[dict]:
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required to view history")

    try:
        session = await svc.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")

    # Users can only view their own sessions
    if session.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    return await svc.get_full_history(session_id)


# ── GET /chat/history ─────────────────────────────────────────────────────────

@router.get(
    "/history",
    summary="All sessions for logged-in user",
)
async def get_user_history(
    user_id: str | None = Depends(get_optional_user),
    svc: SessionService = Depends(_svc),
) -> list[dict]:
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    return await svc.get_user_sessions(user_id)
