"""
app/main.py
────────────
FastAPI application factory with lifespan.
"""
import warnings
from contextlib import asynccontextmanager
from typing import AsyncGenerator

# Suppress noisy third-party warnings (Gemini schema keys, LangChain deprecation)
warnings.filterwarnings(
    "ignore",
    message=".*Convert_system_message_to_human will be deprecated.*",
    category=UserWarning,
    module="langchain_google_genai",
)

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.core.config import get_settings
from app.core.exceptions import (
    AuthError,
    BISError,
    DatabaseError,
    SessionNotFoundError,
    UserExistsError,
)
from app.core.logging import get_logger, setup_logging
from app.db.mongo import MongoDB

settings = get_settings()
setup_logging(settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info(f"BIS Assistant starting [{settings.app_env}]")
    try:
        await MongoDB.connect()
        await MongoDB.ensure_indexes()
        logger.info("DB ready ✓")
    except Exception as e:
        logger.warning("MongoDB unavailable at startup (app will run; DB operations will fail): %s", e)
    yield
    await MongoDB.disconnect()
    logger.info("BIS Assistant stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="BIS Assistant API",
        description="Intelligent assistant for Bureau of Indian Standards — ReAct agent with Hybrid RAG",
        version="2.0.0",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # ── CORS ──────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Exception handlers ────────────────────────────────────────────────

    @app.exception_handler(SessionNotFoundError)
    async def _session_not_found(req: Request, exc: SessionNotFoundError):
        return JSONResponse(status_code=404, content={"detail": exc.message})

    @app.exception_handler(AuthError)
    async def _auth_error(req: Request, exc: AuthError):
        return JSONResponse(status_code=401, content={"detail": exc.message})

    @app.exception_handler(UserExistsError)
    async def _user_exists(req: Request, exc: UserExistsError):
        return JSONResponse(status_code=409, content={"detail": exc.message})

    @app.exception_handler(DatabaseError)
    async def _db_error(req: Request, exc: DatabaseError):
        logger.error(f"DB error: {exc.message}")
        return JSONResponse(status_code=503, content={"detail": "Database temporarily unavailable"})

    @app.exception_handler(ConnectionFailure)
    @app.exception_handler(ServerSelectionTimeoutError)
    async def _mongo_unreachable(req: Request, exc: Exception):
        logger.error("MongoDB unreachable: %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Database unreachable. Check network connectivity and MongoDB URI (e.g. DNS, firewall, VPN)."
            },
        )

    @app.exception_handler(BISError)
    async def _bis_error(req: Request, exc: BISError):
        return JSONResponse(status_code=500, content={"detail": exc.message})

    # ── Routers ───────────────────────────────────────────────────────────
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(chat_router, prefix="/api/v1")

    # ── Health ────────────────────────────────────────────────────────────
    @app.get("/api/v1/health", tags=["health"])
    async def health():
        try:
            await MongoDB.get_db().command("ping")
            db_status = "connected"
        except Exception:
            db_status = "disconnected"

        return {
            "status": "ok",
            "env": settings.app_env,
            "db": db_status,
            "llm": settings.gemini_model,
            "embedding": settings.gemini_embedding_model,
            "agent_max_iterations": settings.agent_max_iterations,
            "session_window": settings.session_window_size,
            "allowed_domains": settings.allowed_domains,
        }

    return app


app = create_app()
