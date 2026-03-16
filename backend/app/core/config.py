"""
app/core/config.py
──────────────────
All settings come from environment variables.
Import get_settings() everywhere — never os.environ directly.
"""
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ───────────────────────────────────────────────────────────────
    app_env: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"
    cors_origins: list[str] = ["http://localhost:3000"]

    # ── MongoDB ───────────────────────────────────────────────────────────
    mongodb_uri: str = Field(..., alias="MONGO_URI")
    mongodb_db_name: str = Field(default="bis_assistant", alias="MONGODB_DB_NAME")

    # collection names
    col_knowledge: str = "bis_knowledge"
    col_sessions: str = "sessions"
    col_messages: str = "messages"
    col_users: str = "users"
    col_standards: str = "is_standards"

    # ── Gemini ────────────────────────────────────────────────────────────
    gemini_api_key: str
    gemini_model: str = "gemini-2.0-flash"
    gemini_embedding_model: str = Field(default="models/gemini-embedding-001", alias="GEMINI_EMBEDDING_MODEL")
    embedding_dimensions: int = Field(default=3072, alias="EMBEDDING_DIMENSIONS")
    agent_temperature: float = Field(default=0.2, ge=0.0, le=1.0)
    # Embedding cache: TTL in seconds (0 = no expiry), max entries to avoid unbounded growth
    embedding_cache_ttl_sec: int = Field(default=60, ge=0, le=86400)
    embedding_cache_max_size: int = Field(default=500, ge=0, le=10_000)

    # ── Web RAG ───────────────────────────────────────────────────────────
    tavily_api_key: str = ""
    # Tavily: "advanced" = better relevance + deeper URLs (2 credits), "basic" = faster (1 credit)
    web_search_depth: str = "advanced"
    allowed_domains: list[str] = [
        "bis.gov.in",
        "bis.org.in",
        "manakonline.in",
        "crsbis.in",
        "huid.manakonline.in",
        "beeindia.gov.in",
    ]

    # ── Auth ──────────────────────────────────────────────────────────────
    jwt_secret_key: str = "dev-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 10080  # 7 days

    # ── Session ───────────────────────────────────────────────────────────
    session_ttl_hours: int = Field(default=2, ge=1, le=24)
    session_window_size: int = Field(default=12, ge=2, le=50)
    # When history has this many messages or more, we ask the user to start a new chat
    session_max_messages_before_new_chat: int = Field(default=8, ge=2, le=30)
    # Truncate assistant message when persisting (0 = no limit)
    session_assistant_save_max_chars: int = Field(default=4000, ge=0, le=100_000)
    # Truncate each assistant reply when building "Past conversation" for LLM context
    session_context_assistant_max_chars: int = Field(default=700, ge=0, le=5000)

    # ── Agent ─────────────────────────────────────────────────────────────
    agent_max_iterations: int = Field(default=8, ge=1, le=20)
    # Pre-inject RAG: run vector search before first LLM call and inject into prompt.
    # Web search is not pre-injected; the agent calls web_search_bis when RAG is insufficient.
    pre_inject_rag: bool = True
    pre_inject_rag_top_k: int = Field(default=5, ge=1, le=15)

    # ── Validators ────────────────────────────────────────────────────────
    @field_validator("cors_origins", "allowed_domains", mode="before")
    @classmethod
    def parse_list(cls, v: str | list) -> list[str]:
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    # ── Computed properties ───────────────────────────────────────────────
    @property
    def session_ttl_seconds(self) -> int:
        return self.session_ttl_hours * 3600

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    def is_domain_allowed(self, url: str) -> bool:
        """Check if a URL belongs to an allowed domain."""
        from urllib.parse import urlparse
        try:
            host = urlparse(url).hostname or ""
            return any(
                host == d or host.endswith(f".{d}")
                for d in self.allowed_domains
            )
        except Exception:
            return False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
