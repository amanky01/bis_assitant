"""
app/db/mongo.py
────────────────
Singleton async MongoDB client (Motor).
All indexes are created here — idempotent, safe to run on every startup.

Atlas vector search index for bis_knowledge must be created manually
in the Atlas UI or via the seed script (cannot be done via Motor).
"""
from __future__ import annotations

import motor.motor_asyncio
from pymongo import ASCENDING, DESCENDING, TEXT
from pymongo.errors import ConfigurationError, ConnectionFailure

from app.core.config import get_settings
from app.core.exceptions import DatabaseError
from app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()


class MongoDB:
    _client: motor.motor_asyncio.AsyncIOMotorClient | None = None
    _db: motor.motor_asyncio.AsyncIOMotorDatabase | None = None

    @classmethod
    async def connect(cls) -> None:
        if cls._client:
            return
        try:
            cls._client = motor.motor_asyncio.AsyncIOMotorClient(
                settings.mongodb_uri,
                serverSelectionTimeoutMS=5000,
                maxPoolSize=20,
                minPoolSize=5,
            )
            await cls._client.admin.command("ping")
            cls._db = cls._client[settings.mongodb_db_name]
            logger.info(f"MongoDB connected → {settings.mongodb_db_name}")
        except (ConnectionFailure, ConfigurationError) as exc:
            raise DatabaseError(f"MongoDB connection failed: {exc}") from exc

    @classmethod
    async def disconnect(cls) -> None:
        if cls._client:
            cls._client.close()
            cls._client = None
            cls._db = None
            logger.info("MongoDB disconnected")

    @classmethod
    def get_db(cls) -> motor.motor_asyncio.AsyncIOMotorDatabase:
        if cls._db is None:
            raise DatabaseError("MongoDB not connected. Call connect() first.")
        return cls._db

    @classmethod
    def col(cls, name: str) -> motor.motor_asyncio.AsyncIOMotorCollection:
        return cls.get_db()[name]

    @classmethod
    async def ensure_indexes(cls) -> None:
        """Create all required indexes. Idempotent."""
        db = cls.get_db()

        # ── users ──────────────────────────────────────────────────────
        await db[settings.col_users].create_index(
            [("email", ASCENDING)], unique=True, name="idx_email"
        )

        # ── sessions ───────────────────────────────────────────────────
        await db[settings.col_sessions].create_index(
            [("session_id", ASCENDING)], unique=True, name="idx_session_id"
        )
        # TTL index — MongoDB auto-expires anonymous sessions
        # Authenticated sessions have no TTL (updated_at not indexed for TTL)
        await db[settings.col_sessions].create_index(
            [("expires_at", ASCENDING)],
            expireAfterSeconds=0,          # expire AT the expires_at timestamp
            name="idx_session_ttl",
            partialFilterExpression={"user_id": None},  # only anon sessions
        )

        # ── messages ───────────────────────────────────────────────────
        await db[settings.col_messages].create_index(
            [("session_id", ASCENDING), ("created_at", ASCENDING)],
            name="idx_messages_session_time"
        )
        await db[settings.col_messages].create_index(
            [("user_id", ASCENDING), ("created_at", DESCENDING)],
            name="idx_messages_user",
            sparse=True,
        )

        # ── is_standards ───────────────────────────────────────────────
        await db[settings.col_standards].create_index(
            [("is_number", ASCENDING)], unique=True, name="idx_is_number"
        )
        await db[settings.col_standards].create_index(
            [("categories", TEXT)], name="idx_categories_text"
        )

        # ── bis_knowledge (vector index) ───────────────────────────────
        # Atlas vector search index must be created via Atlas UI:
        #   Collection : bis_knowledge
        #   Index name : bis_knowledge_vector_index
        #   Field      : embedding  |  Dimensions: 3072 (see EMBEDDING_DIMENSIONS)  |  Similarity: cosine

        logger.info("MongoDB indexes ensured")
