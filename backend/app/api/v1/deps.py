from fastapi import Header, HTTPException, Request

from app.core.exceptions import AuthError
from app.core.security import decode_token

AUTH_COOKIE = "bis_access"


async def get_optional_user(
    request: Request, authorization: str | None = Header(default=None)
) -> str | None:
    token = request.cookies.get(AUTH_COOKIE)
    if authorization is not None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(401, "Invalid credentials")
    if not token:
        return None
    try:
        subject = decode_token(token)["sub"]
        if not isinstance(subject, str) or not subject:
            raise ValueError("Missing subject")
        return subject
    except (AuthError, KeyError, ValueError):
        raise HTTPException(401, "Invalid or expired credentials") from None
