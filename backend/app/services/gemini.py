"""
app/services/gemini.py
───────────────────────
Gemini embeddings + configurable chat LLM (Gemini or Groq via env).

  - task_type="retrieval_query" for search; seed uses task_type="retrieval_document".
  - output_dimensionality must match stored docs and Atlas index (e.g. 3072).
    Set EMBEDDING_DIMENSIONS=3072 and create Atlas vector index with dimensions: 3072.
  - Optional in-memory embedding cache with TTL and max size (see config).
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from functools import lru_cache
from typing import Literal

import google.generativeai as genai
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq

from app.core.config import get_settings
from app.core.exceptions import EmbeddingError
from app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()


@lru_cache(maxsize=8)
def _build_llm(provider: Literal["gemini", "groq"], model: str) -> BaseChatModel:
    s = get_settings()
    if provider == "gemini":
        llm = ChatGoogleGenerativeAI(
            model=model,
            google_api_key=s.gemini_api_key,
            temperature=s.agent_temperature,
            max_output_tokens=2048,
            streaming=True,
            convert_system_message_to_human=True,
        )
        logger.info(f"LLM ready (gemini): {model}")
        return llm
    if provider == "groq":
        if not (s.groq_api_key or "").strip():
            raise ValueError("GROQ_API_KEY is required when LLM_PROVIDER=groq")
        llm = ChatGroq(
            model=model,
            groq_api_key=s.groq_api_key,
            temperature=s.agent_temperature,
            max_tokens=2048,
            streaming=True,
        )
        logger.info(f"LLM ready (groq): {model}")
        return llm
    raise ValueError(f"Unknown LLM_PROVIDER: {provider}")


def get_llm() -> BaseChatModel:
    s = get_settings()
    model = s.gemini_model if s.llm_provider == "gemini" else s.groq_model
    return _build_llm(s.llm_provider, model)


class GeminiEmbeddingService:
    """
    Async wrapper around google-generativeai embed_content.

    Two task types:
      retrieval_query    → used at search time (this class, embed/embed_batch)
      retrieval_document → used at seed time (seed_vector_db.py)

    Both must use the same model. Recommended: models/text-embedding-004
    """

    def __init__(self) -> None:
        genai.configure(api_key=settings.gemini_api_key)
        self._model = settings.gemini_embedding_model
        # (expiry_time, vector); dict keeps insertion order for eviction
        self._cache: dict[str, tuple[float, list[float]]] = {}
        self._ttl = settings.embedding_cache_ttl_sec
        self._max_size = settings.embedding_cache_max_size
        logger.info(f"GeminiEmbeddingService ready: {self._model} (cache ttl={self._ttl}s max={self._max_size})")

    def _key(self, text: str) -> str:
        return hashlib.sha256(
            f"{self._model}::{settings.embedding_dimensions}::query::{text}".encode()
        ).hexdigest()

    def _evict_if_needed(self) -> None:
        if self._max_size <= 0 or len(self._cache) <= self._max_size:
            return
        now = time.monotonic()
        # Remove expired first
        expired = [k for k, (exp,) in self._cache.items() if self._ttl > 0 and now >= exp]
        for k in expired:
            del self._cache[k]
        # If still over max, drop oldest (first keys in insertion order)
        while len(self._cache) > self._max_size:
            first = next(iter(self._cache))
            del self._cache[first]

    async def embed(self, text: str) -> list[float]:
        """
        Embed a query string for retrieval. Uses optional TTL cache (same query within TTL reused).
        task_type='retrieval_query' — paired with 'retrieval_document' at seed time.
        """
        k = self._key(text)
        now = time.monotonic()
        if k in self._cache:
            exp, vec = self._cache[k]
            if self._ttl == 0 or now < exp:
                return vec
            del self._cache[k]
        try:
            result = await asyncio.to_thread(
                genai.embed_content,
                model=self._model,
                content=text,
                task_type="retrieval_query",
                output_dimensionality=settings.embedding_dimensions,
            )
            vec = result["embedding"]
            exp = (now + self._ttl) if self._ttl > 0 else 0.0
            self._cache[k] = (exp, vec)
            self._evict_if_needed()
            return vec
        except Exception as exc:
            raise EmbeddingError(f"Gemini embed failed for model={self._model}: {exc}") from exc

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple query strings concurrently with cache."""
        now = time.monotonic()
        results: list[list[float] | None] = [None] * len(texts)
        miss_indices = []
        for i, t in enumerate(texts):
            k = self._key(t)
            if k in self._cache:
                exp, vec = self._cache[k]
                if self._ttl == 0 or now < exp:
                    results[i] = vec
                    continue
            miss_indices.append(i)

        if miss_indices:
            vectors = await asyncio.gather(*[self.embed(texts[i]) for i in miss_indices])
            for idx, vec in zip(miss_indices, vectors):
                results[idx] = vec

        for i, text in enumerate(texts):
            if results[i] is None:
                k = self._key(text)
                _, results[i] = self._cache[k]

        return [r for r in results if r is not None]


@lru_cache(maxsize=1)
def get_embedding_service() -> GeminiEmbeddingService:
    return GeminiEmbeddingService()