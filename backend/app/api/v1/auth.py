"""
app/api/v1/auth.py
───────────────────
Auth endpoints.

POST /auth/register  — create account, get JWT immediately
POST /auth/login     — verify credentials, get JWT
"""
from fastapi import APIRouter, HTTPException, status

from app.core.exceptions import AuthError, UserExistsError
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse
from app.services.user import UserService

router = APIRouter(prefix="/auth", tags=["auth"])
_svc = UserService()


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new account",
)
async def register(body: RegisterRequest) -> TokenResponse:
    try:
        result = await _svc.register(body.email, body.password)
    except UserExistsError as exc:
        raise HTTPException(status_code=409, detail=exc.message)
    return TokenResponse(
        access_token=result["access_token"],
        user_id=result["user_id"],
        email=result["email"],
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Login and get access token",
)
async def login(body: LoginRequest) -> TokenResponse:
    try:
        result = await _svc.login(body.email, body.password)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=exc.message)
    return TokenResponse(
        access_token=result["access_token"],
        user_id=result["user_id"],
        email=result["email"],
    )
