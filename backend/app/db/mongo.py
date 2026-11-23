"""
app/db/mongo.py
────────────────
Singleton async PyMongo client.
All indexes are created here — idempotent, safe to run on every startup.

Atlas vector search index for bis_knowledge must be created manually
in the Atlas UI.
"""
from __future__ import annotations

from pymongo import ASCENDING, DESCENDING, TEXT, AsyncMongoClient
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import ConfigurationError, ConnectionFailure

from app.core.config import get_settings
from app.core.exceptions import DatabaseError
from app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()


class MongoDB:
    _client: AsyncMongoClient | None = None
    _db: AsyncDatabase | None = None

    @classmethod
    async def connect(cls) -> None:
        if cls._client:
            return
        try:
            cls._client = AsyncMongoClient(
                settings.mongodb_uri,
                serverSelectionTimeoutMS=5000,
                maxPoolSize=20,
                minPoolSize=5,
            )
            await cls._client.admin.command("ping")
            cls._db = cls._client[settings.mongodb_db_name]
            logger.info(f"MongoDB connected → {settings.mongodb_db_name}")
        except (ConnectionFailure, ConfigurationError) as exc:
            await cls.disconnect()
            raise DatabaseError("MongoDB connection failed") from exc

    @classmethod
    async def disconnect(cls) -> None:
        if cls._client:
            await cls._client.close()
            cls._client = None
            cls._db = None
            logger.info("MongoDB disconnected")

    @classmethod
    def get_db(cls) -> AsyncDatabase:
        if cls._db is None:
            raise DatabaseError("MongoDB not connected. Call connect() first.")
        return cls._db

    @classmethod
    def col(cls, name: str) -> AsyncCollection:
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

        await db[settings.col_messages].create_index(
            "expires_at", expireAfterSeconds=0, name="idx_messages_ttl"
        )
        await db["request_limits"].create_index(
            "expires_at", expireAfterSeconds=0, name="idx_request_limits_ttl"
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
