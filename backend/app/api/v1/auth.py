from fastapi import APIRouter, Depends, HTTPException, Request, Response

from fastapi.responses import JSONResponse

from app.api.v1.deps import AUTH_COOKIE
from app.api.v1.limits import limit_auth
from app.core.config import get_settings
from app.core.exceptions import AuthError, UserExistsError
from app.core.security import decode_token
from app.schemas.auth import LoginRequest, RegisterRequest, UserResponse
from app.services.user import UserService

router = APIRouter(prefix="/auth", tags=["auth"])
_svc = UserService()
settings = get_settings()


def _set_session(response: Response, result: dict) -> UserResponse:
    response.set_cookie(
        AUTH_COOKIE, result["access_token"], httponly=True,
        secure=settings.app_env != "development", samesite="strict",
        max_age=settings.jwt_expire_minutes * 60,
    )
    return UserResponse(user_id=result["user_id"], email=result["email"])


@router.post("/register", status_code=201, dependencies=[Depends(limit_auth)])
async def register(body: RegisterRequest, response: Response) -> UserResponse:
    try:
        return _set_session(response, await _svc.register(body.email, body.password))
    except UserExistsError as exc:
        raise HTTPException(409, exc.message) from None


@router.post("/login", dependencies=[Depends(limit_auth)])
async def login(body: LoginRequest, response: Response) -> UserResponse:
    try:
        return _set_session(response, await _svc.login(body.email, body.password))
    except AuthError:
        raise HTTPException(401, "Invalid email or password") from None


def _clear_session(response: Response) -> None:
    response.delete_cookie(
        AUTH_COOKIE, httponly=True, secure=settings.app_env != "development", samesite="strict"
    )


@router.get("/me", response_model=UserResponse)
async def me(request: Request):
    token = request.cookies.get(AUTH_COOKIE)
    if not token:
        raise HTTPException(401, "Authentication required")
    try:
        payload = decode_token(token)
        return UserResponse(user_id=payload["sub"], email=payload.get("email", ""))
    except AuthError:
        response = JSONResponse(status_code=401, content={"detail": "Invalid or expired credentials"})
        _clear_session(response)
        return response


@router.post("/logout", status_code=204)
async def logout(response: Response) -> None:
    _clear_session(response)
