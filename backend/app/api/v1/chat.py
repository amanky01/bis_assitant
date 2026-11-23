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
import asyncio
import json
import time
from contextlib import aclosing
from datetime import datetime, timezone
from typing import AsyncGenerator
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from pymongo.errors import DuplicateKeyError

from app.agents.react_agent import run_agent
from app.api.v1.deps import get_optional_user
from app.api.v1.limits import limit_chat
from app.api.v1.streaming import frame, keep_alive
from app.core.config import get_settings
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
router = APIRouter(prefix="/chat", tags=["chat"], dependencies=[Depends(limit_chat)])


def _svc() -> SessionService:
    return SessionService()

async def _authorized_session(
    session_id: str,
    user_id: str | None = Depends(get_optional_user),
    guest_token: str | None = Header(default=None, alias="X-Guest-Token"),
    svc: SessionService = Depends(_svc),
) -> dict:
    return await svc.get_authorized_session(session_id, user_id, guest_token)



# ── POST /chat/sessions ───────────────────────────────────────────────────────

@router.post(
    "/sessions",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a chat session",
)
async def create_session(
    body: CreateSessionRequest,
    response: Response,
    user_id: str | None = Depends(get_optional_user),
    svc: SessionService = Depends(_svc),
) -> SessionResponse:
    if len(json.dumps(body.metadata)) > 4096:
        raise HTTPException(422, "Session metadata exceeds the size limit")
    response.headers["Cache-Control"] = "no-store"
    session_id, guest_token = await svc.create_session(user_id=user_id, metadata=body.metadata)
    session = await svc.get_session(session_id)
    return SessionResponse(
        session_id=session_id,
        created_at=session["created_at"],
        expires_at=session.get("expires_at"),
        guest_token=guest_token,
    )


# ── GET /chat/sessions/{session_id} ──────────────────────────────────────────

@router.get(
    "/sessions/{session_id}",
    response_model=SessionResponse,
    summary="Get session info",
)
async def get_session(
    session_id: str,
    session: dict = Depends(_authorized_session),
) -> SessionResponse:
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
    session: dict = Depends(_authorized_session),
    svc: SessionService = Depends(_svc),
) -> None:
    await svc.end_session(session_id)


# ── POST /chat/sessions/{session_id}/message ─────────────────────────────────

@router.post(
    "/sessions/{session_id}/message",
    summary="Send a message — returns SSE stream",
)
async def send_message(
    session_id: str,
    body: SendMessageRequest,
    idempotency_key: UUID | None = Header(default=None),
    session: dict = Depends(_authorized_session),
    svc: SessionService = Depends(_svc),
) -> StreamingResponse:
    if body.session_id != session_id:
        raise HTTPException(status_code=422, detail="session_id mismatch in URL vs body")

    user_id = session["user_id"]
    if idempotency_key:
        turn = f"{session_id}/{idempotency_key}"
        try:
            await svc.save_message(session_id, user_id, MessageRole.USER, body.message, message_id=f"{turn}/user")
        except DuplicateKeyError:
            previous = await svc.get_turn_message(f"{turn}/user")
            if previous is None or previous["content"] != body.message:
                raise HTTPException(409, "Idempotency key already used")
            completed = await svc.get_turn_message(f"{turn}/assistant")
            if completed is None:
                raise HTTPException(409, "This submission is pending or did not finish")
            stream = _replay(completed)
        else:
            stream = _stream(session_id, user_id, body.message, svc, turn=turn)

    else:
        stream = _stream(session_id, user_id, body.message, svc)

    return StreamingResponse(
        keep_alive(stream),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _replay(message: dict) -> AsyncGenerator[str, None]:
    yield frame("token", message["content"])
    metadata = StreamChunk(type="metadata", content="", metadata=ResponseMetadata(**message["metadata"]))
    yield f"data: {metadata.model_dump_json()}\n\n"
    yield frame("done")


async def _stream(
    session_id: str,
    user_id: str | None,
    user_message: str,
    svc: SessionService,
    *,
    turn: str | None = None,
) -> AsyncGenerator[str, None]:
    start = time.monotonic()

    try:
        if turn:
            history = await svc.get_window(session_id, exclude_id=f"{turn}/user")
        else:
            history = await svc.get_window(session_id)
            await svc.save_message(session_id, user_id, MessageRole.USER, user_message)
        await svc.set_session_title_if_empty(session_id, user_message)
    except Exception as exc:
        logger.warning("Chat initialization failed (%s)", type(exc).__name__)
        chunk = StreamChunk(type="error", content="Unable to start this response.")
        yield f"data: {chunk.model_dump_json()}\n\n"
        return

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
        async for event in _finish_stream(svc, session_id, user_id, new_chat_message, {}, turn):
            yield event
        return

    full_response_parts: list[str] = []
    final_metadata: ResponseMetadata | None = None

    try:
        async with (
            asyncio.timeout(settings.agent_timeout_seconds),
            aclosing(run_agent(session_id, user_message, history)) as agent_events,
        ):
            async for event in agent_events:
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

    except Exception as exc:
        logger.warning("Chat stream failed (%s)", type(exc).__name__)
        chunk = StreamChunk(type="error", content="Unable to complete this response.")
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
        async for event in _finish_stream(svc, session_id, user_id, to_save, meta_dict, turn):
            yield event
    else:
        chunk = StreamChunk(type="error", content="The assistant returned an empty response.")
        yield f"data: {chunk.model_dump_json()}\n\n"


async def _finish_stream(
    svc: SessionService, session_id: str, user_id: str | None, content: str, metadata: dict, turn: str | None
) -> AsyncGenerator[str, None]:
    try:
        await svc.save_message(
            session_id, user_id, MessageRole.ASSISTANT, content,
            metadata=metadata, message_id=f"{turn}/assistant" if turn else None,
        )
        await svc.touch_session(session_id)
    except Exception as exc:
        logger.warning("Chat persistence failed (%s)", type(exc).__name__)
        chunk = StreamChunk(type="error", content="Unable to save this response.")
    else:
        chunk = StreamChunk(type="done", content="")
    yield f"data: {chunk.model_dump_json()}\n\n"


# ── GET /chat/sessions/{session_id}/history ───────────────────────────────────

@router.get(
    "/sessions/{session_id}/history",
    summary="Full message history for a session (authenticated users only)",
)
async def get_history(
    session_id: str,
    session: dict = Depends(_authorized_session),
    user_id: str | None = Depends(get_optional_user),
    svc: SessionService = Depends(_svc),
) -> list[dict]:
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required to view history")

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
