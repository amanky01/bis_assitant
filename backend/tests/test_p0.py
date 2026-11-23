from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agents import react_agent
from app.api.v1 import auth, chat, limits
from app.core import official_http
from app.core.config import Settings
from app.core.exceptions import DomainNotAllowedError, ScraperError
from app.core.security import create_access_token
from app.db.mongo import MongoDB
from app.main import app
from app.services.session import SessionService
from app.tools import verification as registry


def _bearer(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token({'sub': user, 'email': 'owner@example.com'})}"}


@pytest.fixture
def session_store(monkeypatch):
    now = datetime.now(timezone.utc)
    account, guest = str(uuid4()), str(uuid4())
    sessions = [
        {"session_id": account, "user_id": "owner", "created_at": now, "expires_at": None},
        {"session_id": guest, "user_id": None, "created_at": now,
         "expires_at": now + timedelta(hours=1),
         "guest_token_hash": SessionService._guest_hash("test-guest-token")},
    ]

    async def find(query):
        for session in sessions:
            if session["session_id"] != query["session_id"] or session["user_id"] != query["user_id"]:
                continue
            if session["user_id"] is None and (
                session["guest_token_hash"] != query["guest_token_hash"]
                or session["expires_at"] <= query["expires_at"]["$gt"]
            ):
                continue
            return session
        return None

    collection = type("SessionCollection", (), {})()
    collection.find_one = AsyncMock(side_effect=find)
    monkeypatch.setattr(MongoDB, "col", lambda name: collection)
    monkeypatch.setattr(limits, "_limit", AsyncMock())
    monkeypatch.setattr(SessionService, "end_session", AsyncMock())
    monkeypatch.setattr(SessionService, "get_full_history", AsyncMock(return_value=[]))

    async def stream(*args):
        yield 'data: {"type":"done"}\n\n'

    monkeypatch.setattr(chat, "_stream", stream)
    return sessions


@pytest.mark.parametrize("method,suffix", [("GET", ""), ("DELETE", ""), ("POST", "/message"), ("GET", "/history")])
def test_account_session_owner(method, suffix, session_store):
    session_id = session_store[0]["session_id"]
    url = f"/api/v1/chat/sessions/{session_id}{suffix}"
    body = {"json": {"session_id": session_id, "message": "test"}} if method == "POST" else {}
    client = TestClient(app)
    assert client.request(method, url, **body).status_code == 404
    assert client.request(method, url, headers=_bearer("other"), **body).status_code == 404
    assert client.request(method, url, headers={"Authorization": "Bearer invalid"}, **body).status_code == 401
    expected = 204 if method == "DELETE" else 200
    assert client.request(method, url, headers=_bearer("owner"), **body).status_code == expected


def test_guest_requires_credential_and_expiry_is_enforced(session_store):
    session = session_store[1]
    session_id = session["session_id"]
    url = f"/api/v1/chat/sessions/{session_id}"
    client = TestClient(app)
    assert client.get(url).status_code == 404
    assert client.delete(url, headers={"X-Guest-Token": "wrong"}).status_code == 404
    headers = {"X-Guest-Token": "test-guest-token"}
    assert client.get(url, headers=headers).status_code == 200
    assert client.post(f"{url}/message", headers=headers, json={
        "session_id": session_id, "message": "test"
    }).status_code == 200
    assert client.get(f"{url}/history", headers=headers).status_code == 401
    session["expires_at"] -= timedelta(hours=2)
    assert client.get(url, headers=headers).status_code == 404
    assert client.delete(url, headers=headers).status_code == 404


def test_browser_auth_cookie_and_csrf(monkeypatch, session_store):
    token = create_access_token({"sub": "owner", "email": "owner@example.com"})
    monkeypatch.setattr(auth._svc, "login", AsyncMock(return_value={
        "user_id": "owner", "email": "owner@example.com", "access_token": token,
    }))
    client = TestClient(app, base_url="https://testserver")
    body = {"email": "owner@example.com", "password": "test-password"}
    headers = {"X-BIS-Client": "web"}
    response = client.post("/api/v1/auth/login", json=body, headers=headers)
    assert response.status_code == 200
    assert "access_token" not in response.json()
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=strict" in response.headers["set-cookie"].lower()
    assert client.get("/api/v1/auth/me").json() == response.json()
    assert client.post("/api/v1/auth/logout").status_code == 403
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401


def test_production_rejects_development_jwt_secret():
    with pytest.raises(ValidationError, match="JWT_SECRET_KEY"):
        Settings(app_env="production", jwt_secret_key="dev-secret-change-in-production")


@pytest.mark.asyncio
async def test_rate_limit_is_shared_and_returns_retry_after(monkeypatch):
    collection = type("LimitCollection", (), {})()
    collection.find_one_and_update = AsyncMock(return_value={"count": 2})
    monkeypatch.setattr(MongoDB, "col", lambda name: collection)
    with pytest.raises(HTTPException) as exc:
        await limits._limit("chat", "user:owner", 1)
    assert exc.value.status_code == 429
    assert int(exc.value.headers["Retry-After"]) > 0
    args = collection.find_one_and_update.call_args.args
    assert args[0]["_id"].startswith("chat:")
    assert "owner" not in args[0]["_id"]
    assert args[1]["$inc"] == {"count": 1}


@pytest.mark.parametrize(("body", "outcome"), [
    (b"<table><tr><td>Registration No.</td><td>R-12345678</td></tr>"
     b"<tr><td>Status</td><td>INVALID</td></tr></table>", registry.Outcome.INACTIVE),
    (b"<table><tr><td>Registration No.</td><td>R-12345678</td></tr>"
     b"<tr><td>Status</td><td>VALID</td></tr></table>", registry.Outcome.ACTIVE),
    (b"<table><tr><td>Registration No.</td><td>R-87654321</td></tr>"
     b"<tr><td>Status</td><td>VALID</td></tr></table>", registry.Outcome.UNRECOGNIZED),
    (b"<h1>Access denied</h1>", registry.Outcome.UNRECOGNIZED),
    (b"<p>No record found</p>", registry.Outcome.NOT_FOUND),
])
def test_crs_verdicts(body, outcome):
    result = registry._parse_table(body, "R-12345678", registry.CRS_URL)
    assert result.outcome == outcome
    assert registry.CRS_URL in result.render()


@pytest.mark.parametrize(("body", "outcome"), [
    (b'{"ok": true}', registry.Outcome.UNRECOGNIZED),
    (b'{"huid": "AB12CD", "purity": "22K"}', registry.Outcome.UNRECOGNIZED),
    (b'{"huid": "AB12CD", "purity": "22K", "ahcName": "Test centre"}', registry.Outcome.RECORD_FOUND),
])
def test_huid_requires_matching_record_fields(body, outcome):
    assert registry._parse_huid(body, "AB12CD").outcome == outcome


@pytest.mark.asyncio
async def test_portal_outage_bypasses_model(monkeypatch):
    monkeypatch.setattr(registry, "fetch_official", AsyncMock(side_effect=httpx.ConnectError("offline")))
    monkeypatch.setattr(react_agent, "_get_graph", lambda: pytest.fail("model should not run"))
    events = [event async for event in react_agent.run_agent("test", "Check R-12345678", [])]
    assert "Verification is inconclusive" in events[1]["content"]
    assert events[2]["metadata"]["unverified"][0]["reason"] == "unavailable"


@pytest.mark.parametrize("url", [
    "http://www.bis.gov.in/doc", "https://account@bis.gov.in/doc",
    "https://bis.gov.in:8443/doc", "https://example.com/doc",
])
@pytest.mark.asyncio
async def test_official_fetch_rejects_unsafe_destination(url):
    with pytest.raises(DomainNotAllowedError):
        await official_http.fetch_official(url)


@pytest.mark.asyncio
async def test_official_fetch_rejects_non_public_resolution(monkeypatch):
    import asyncio

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(
        return_value=[(None, None, None, None, ("10.0.0.1", 443))]
    ))
    with pytest.raises(DomainNotAllowedError):
        await official_http.fetch_official("https://bis.gov.in/doc")


class _Stream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"one"
        yield b"two"


@pytest.mark.asyncio
async def test_official_fetch_pins_destination_checks_redirect_and_caps_bytes(monkeypatch):
    real_client = httpx.AsyncClient
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "https://example.com/doc"})
        return httpx.Response(200, stream=_Stream())

    monkeypatch.setattr(
        official_http.httpx, "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(official_http, "_public_address", AsyncMock(return_value="1.1.1.1"))
    with pytest.raises(DomainNotAllowedError):
        await official_http.fetch_official("https://bis.gov.in/redirect", allow_redirects=True)
    assert len(requests) == 1
    assert requests[0].url.host == "1.1.1.1"
    assert requests[0].headers["Host"] == "bis.gov.in"
    assert requests[0].extensions["sni_hostname"] == "bis.gov.in"
    with pytest.raises(ScraperError, match="size limit"):
        await official_http.fetch_official("https://bis.gov.in/doc", max_bytes=4)


@pytest.mark.asyncio
async def test_guest_message_inherits_session_expiry(monkeypatch):
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    svc = SessionService()
    monkeypatch.setattr(svc, "get_session", AsyncMock(return_value={"expires_at": expires_at}))
    collection = type("MessageCollection", (), {})()
    collection.insert_one = AsyncMock()
    monkeypatch.setattr(MongoDB, "col", lambda name: collection)
    await svc.save_message(str(uuid4()), None, chat.MessageRole.USER, "hello")
    assert collection.insert_one.call_args.args[0]["expires_at"] == expires_at


@pytest.mark.asyncio
async def test_ordinary_chat_streams_from_upgraded_graph(monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    class FakeBISModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    monkeypatch.setattr(react_agent, "_graph", None)
    monkeypatch.setattr(react_agent, "get_llm", lambda: FakeBISModel(
        responses=[AIMessage(content="BIS publishes Indian Standards. [S1]")]
    ))
    from app.core.evidence import register_evidence

    async def retrieve(query):
        register_evidence("https://www.bis.gov.in/", "Bureau of Indian Standards")
        return "[S1] BIS publishes Indian Standards."

    monkeypatch.setattr(react_agent.settings, "pre_inject_rag", True)
    monkeypatch.setattr(react_agent, "_run_pre_inject_retrieval", retrieve)
    events = [event async for event in react_agent.run_agent("test", "What does BIS do?", [])]
    answer = "".join(event["content"] for event in events if event["type"] == "token")
    assert answer == "BIS publishes Indian Standards. [S1](https://www.bis.gov.in/)"
    assert events[-1]["type"] == "done"
