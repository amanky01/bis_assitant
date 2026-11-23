"""
app/main.py
────────────
FastAPI application factory with lifespan.
"""
import asyncio
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
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

from app.api.v1.deps import AUTH_COOKIE
from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.core.config import get_settings, merge_cors_origins_with_defaults
from app.core.exceptions import (
    AuthError,
    BISError,
    DatabaseError,
    SessionNotFoundError,
    UserExistsError,
)
from app.core.logging import get_logger, setup_logging
from app.db.mongo import MongoDB
from app.core.telemetry import RequestTelemetry

settings = get_settings()
setup_logging(settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info(f"BIS Assistant starting [{settings.app_env}]")
    try:
        await MongoDB.connect()
        await MongoDB.ensure_indexes()
        if settings.col_knowledge.startswith("bis_knowledge_"):
            manifest = await MongoDB.col("corpus_releases").find_one({
                "collection": settings.col_knowledge, "status": "seeded",
                "checked_at": {"$type": "date"},
                "embedding_model": settings.gemini_embedding_model,
                "dimensions": settings.embedding_dimensions,
            })
            if manifest is None:
                raise RuntimeError("The selected knowledge release has not passed validation")
        logger.info("DB ready ✓")
    except Exception as exc:
        if settings.app_env != "development":
            await MongoDB.disconnect()
            raise RuntimeError("MongoDB initialization failed") from None
        logger.warning("MongoDB initialization failed (%s)", type(exc).__name__)
    try:
        yield
    finally:
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
    cors_allow = settings.cors_origins if settings.app_env != "development" else merge_cors_origins_with_defaults(settings.cors_origins)
    logger.info("CORS allow_origins: %d host(s) configured", len(cors_allow))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_allow,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def protect_browser_requests(request: Request, call_next):
        protected = request.url.path.startswith(("/api/v1/auth/", "/api/v1/chat/"))
        mutation = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        browser_request = (
            AUTH_COOKIE in request.cookies
            or "origin" in request.headers
            or "sec-fetch-site" in request.headers
        )
        if protected and mutation and browser_request and request.headers.get("x-bis-client") != "web":
            return JSONResponse(status_code=403, content={"detail": "Browser request rejected"})
        response = await call_next(request)
        if protected:
            response.headers["Cache-Control"] = "no-store"
        return response

    app.add_middleware(RequestTelemetry)

    # ── Exception handlers ────────────────────────────────────────────────

    @app.exception_handler(RequestValidationError)
    async def _request_validation(req: Request, exc: RequestValidationError):
        errors = [
            {key: error[key] for key in ("type", "loc", "msg") if key in error}
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

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
        logger.warning("Database operation failed")
        return JSONResponse(status_code=503, content={"detail": "Database temporarily unavailable"})

    @app.exception_handler(ConnectionFailure)
    @app.exception_handler(ServerSelectionTimeoutError)
    async def _mongo_unreachable(req: Request, exc: Exception):
        logger.warning("MongoDB unreachable (%s)", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Database temporarily unavailable"
            },
        )

    @app.exception_handler(BISError)
    async def _bis_error(req: Request, exc: BISError):
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    # ── Routers ───────────────────────────────────────────────────────────
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(chat_router, prefix="/api/v1")

    # ── Health ────────────────────────────────────────────────────────────
    @app.get("/api/v1/health/live", tags=["health"])
    async def live():
        return {"status": "ok"}

    @app.get("/api/v1/health", tags=["health"])
    @app.get("/api/v1/health/ready", tags=["health"])
    async def ready():
        try:
            async with asyncio.timeout(2):
                await MongoDB.get_db().command("ping")
        except Exception:
            return JSONResponse(status_code=503, content={"status": "unavailable", "db": "disconnected"})
        return {"status": "ok", "db": "connected"}

    @app.get("/")
    async def root():
        return {"message": "Welome to BIS Assistant" }

    return app


app = create_app()
