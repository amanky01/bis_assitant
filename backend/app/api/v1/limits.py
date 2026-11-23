import hmac
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.api.v1.deps import get_optional_user
from app.core.config import get_settings
from app.db.mongo import MongoDB

COLLECTION = "request_limits"
settings = get_settings()


async def _limit(scope: str, identity: str, limit: int) -> None:
    now = datetime.now(timezone.utc)
    minute = int(now.timestamp()) // 60
    expires = datetime.fromtimestamp((minute + 1) * 60, timezone.utc)
    collection = MongoDB.col(COLLECTION)
    identity_hash = hmac.digest(settings.jwt_secret_key.encode(), identity.encode(), "sha256").hex()
    key = {"_id": f"{scope}:{identity_hash}:{minute}"}
    update = {"$inc": {"count": 1}, "$setOnInsert": {"expires_at": expires + timedelta(minutes=1)}}
    try:
        bucket = await collection.find_one_and_update(
            key, update, upsert=True, return_document=ReturnDocument.AFTER
        )
    except DuplicateKeyError:
        bucket = await collection.find_one_and_update(
            key, update, return_document=ReturnDocument.AFTER
        )
    if bucket["count"] > limit:
        raise HTTPException(429, "Too many requests", headers={
            "Retry-After": str(60 - now.second)
        })


def _peer(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def limit_auth(request: Request) -> None:
    await _limit("auth", _peer(request), settings.auth_requests_per_minute)


async def limit_chat(request: Request, user_id: str | None = Depends(get_optional_user)) -> None:
    identity = f"user:{user_id}" if user_id else f"ip:{_peer(request)}"
    await _limit("chat", identity, settings.chat_requests_per_minute)
