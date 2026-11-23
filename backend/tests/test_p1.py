import asyncio
from contextlib import aclosing
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

from app.api.v1 import chat, limits
from app.api.v1.streaming import frame, keep_alive
from app.core.evidence import evidence, register_evidence, render_grounded_answer
from app.db.mongo import MongoDB
from app.main import app
from app.schemas.chat import Citation
from app.services.session import SessionService


@pytest.mark.parametrize("draft", [
    "The product is certified.",
    "The product is certified. [S9]",
    "The product is certified. [S1]\nAn unsupported second statement.",
])
def test_unsupported_citation_formats_abstain(draft):
    source = Citation(url="https://www.bis.gov.in/", title="Official source")
    answer, citations = render_grounded_answer(draft, {"S1": source})
    assert "couldn’t verify" in answer
    assert citations == []


def test_retrieval_only_registers_allowed_current_sources():
    token = evidence.set({})
    try:
        assert register_evidence("https://example.com/", "Untrusted source") == ""
        assert register_evidence("https://www.bis.gov.in/", "Future order", effective_date="2999-01-01") == ""
        assert register_evidence("https://www.bis.gov.in/", "Official source", section="Section 2") == "S1"
        answer, citations = render_grounded_answer("Read the scheme. [S1]", evidence.get())
        assert "[S1](https://www.bis.gov.in/)" in answer
        assert citations[0].section == "Section 2"
    finally:
        evidence.reset(token)


def test_liveness_is_independent_and_readiness_fails_on_db_error(monkeypatch):
    def unavailable():
        raise RuntimeError("database credentials must stay private")

    monkeypatch.setattr(MongoDB, "get_db", unavailable)
    client = TestClient(app)
    assert client.get("/api/v1/health/live").status_code == 200
    ready = client.get("/api/v1/health/ready")
    assert ready.status_code == 503
    assert "credentials" not in ready.text
    assert len(ready.headers["X-Request-ID"]) == 32


@pytest.mark.asyncio
async def test_keep_alive_cancels_the_producer_on_disconnect():
    closed = asyncio.Event()

    async def pending():
        try:
            await asyncio.Event().wait()
            yield frame("done")
        finally:
            closed.set()

    async with aclosing(keep_alive(pending(), interval=0.01)) as stream:
        assert await anext(stream) == ": keep-alive\n\n"
    assert closed.is_set()


def test_idempotency_replays_completed_turn_and_rejects_payload_change(monkeypatch):
    session, key = str(uuid4()), str(uuid4())
    monkeypatch.setattr(limits, "_limit", AsyncMock())
    app.dependency_overrides[chat._authorized_session] = lambda: {"user_id": "owner"}
    documents = {}

    async def save(self, session_id, user_id, role, content, metadata=None, message_id=None):
        if message_id in documents:
            raise DuplicateKeyError("duplicate")
        documents[message_id] = {"content": content, "metadata": metadata or {}}

    async def stream(session_id, user_id, message, svc, *, turn):
        yield frame("token", "fixture answer")
        async for chunk in chat._finish_stream(svc, session_id, user_id, "fixture answer", {}, turn):
            yield chunk

    monkeypatch.setattr(SessionService, "save_message", save)
    monkeypatch.setattr(SessionService, "touch_session", AsyncMock())
    monkeypatch.setattr(SessionService, "get_turn_message", AsyncMock(side_effect=documents.get))
    monkeypatch.setattr(chat, "_stream", stream)
    try:
        client = TestClient(app)
        route = f"/api/v1/chat/sessions/{session}/message"
        headers = {"Idempotency-Key": key}
        payload = {"session_id": session, "message": "first submission"}
        for _ in range(2):
            result = client.post(route, headers=headers, json=payload)
            assert result.status_code == 200
            assert "fixture answer" in result.text
            assert '"type":"done"' in result.text
        assert len(documents) == 2
        result = client.post(route, headers=headers, json={**payload, "message": "different content"})
        assert result.status_code == 409
    finally:
        app.dependency_overrides.pop(chat._authorized_session)
