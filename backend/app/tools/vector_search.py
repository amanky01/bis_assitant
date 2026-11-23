"""
app/tools/vector_search.py
───────────────────────────
Tool 1: Static RAG — MongoDB Atlas $vectorSearch.

FIX NOTES (v2):
  - Removed $match on vectorSearchScore from the pipeline.
    $vectorSearch + $match on vectorSearchScore is NOT supported in Atlas —
    it silently returns 0 results. Scores are now filtered in Python.
  - task_type="retrieval_query" for query embeddings (correct pairing
    with "retrieval_document" used during seeding).
  - Added detailed logging so you can see exactly what Atlas returns.
"""
from __future__ import annotations

from langchain_core.tools import tool

from app.core.config import get_settings
from app.core.evidence import register_evidence
from app.core.logging import get_logger
from app.db.mongo import MongoDB
from app.services.gemini import get_embedding_service

logger = get_logger(__name__)
settings = get_settings()

MIN_SCORE = 0.30   # Atlas cosine scores often lower; filter in Python


@tool
async def search_bis_knowledge(query: str, top_k: int = 6) -> str:
    """
    Search the BIS knowledge base for information about Indian Standards,
    ISI marks, CRS, hallmarking, certification processes, BEE star ratings,
    and product compliance. Always call this first before web search.
    If the returned chunks do not directly address the query (e.g. startup-specific
    or niche topic), also call web_search_bis to find official BIS pages.
    Returns relevant document chunks with source URLs.
    """
    # Step 1: embed the query
    if not 1 <= top_k <= 15 or len(query) > 3000:
        return "[vector_search_error] Query exceeds retrieval limits."
    try:
        embedding_svc = get_embedding_service()
        query_vec = await embedding_svc.embed(query)
    except Exception as exc:
        logger.warning("Search embedding failed (%s)", type(exc).__name__)
        return "[vector_search_error] Embedding service temporarily unavailable. Try web_search_bis."

    logger.debug(f"[vector_search] Embedded query dim={len(query_vec)}")

    # Step 2: Atlas $vectorSearch
    # CRITICAL: Do NOT add $match on vectorSearchScore inside this pipeline.
    # Atlas silently returns 0 results if you do. Filter in Python (Step 3).
    pipeline = [
        {
            "$vectorSearch": {
                "index": "bis_knowledge_vector_index",
                "path": "embedding",
                "queryVector": query_vec,
                "numCandidates": top_k * 20,
                "limit": top_k,
            }
        },
        {
            "$project": {
                "_id": 0,
                "title": 1,
                "content": 1,
                "source": 1,
                "section": 1,
                "effective_date": 1,
                "retrieved_at": 1,
                "superseded": 1,
                "category": 1,
                "is_number": 1,
                "score": {"$meta": "vectorSearchScore"},
            }
        },
    ]

    try:
        cursor = await MongoDB.col(settings.col_knowledge).aggregate(pipeline)
        results = await cursor.to_list(top_k)
    except Exception as exc:
        logger.warning("Atlas search failed (%s)", type(exc).__name__)
        return "[vector_search_error] Knowledge index temporarily unavailable. Try web_search_bis."

    if results:
        scores = [round(r.get("score", 0), 3) for r in results]
        logger.info(f"[vector_search] Atlas returned {len(results)} raw, scores: {scores[:5]}{'...' if len(scores)>5 else ''}")
    else:
        logger.warning("[vector_search] Atlas returned 0 documents — check index name and dimensions")

    # Step 3: filter by score in Python
    results = [r for r in results if r.get("score", 0) >= MIN_SCORE]

    if not results:
        logger.info("Vector search returned no results above threshold")
        return "[no_results] No relevant documents found in BIS knowledge base. Try web_search_bis."

    # Step 4: format for LLM
    parts = []
    for record in results:
        if record.get("superseded") is True:
            continue
        title = record.get("title", "BIS Document")
        ref = register_evidence(
            record.get("source") or "", title, section=record.get("section"),
            effective_date=record.get("effective_date"), retrieved_at=record.get("retrieved_at"),
        )
        if not ref:
            continue
        r = record
        parts.append(
            f"[{ref}] {title}\n"
            f"URL: {r.get('source', '')}\n"
            f"Relevance: {r.get('score', 0):.2f}\n"
            f"{r.get('content', '')}"
        )

    logger.info("Vector search result_count=%s", len(results))
    return "\n\n---\n\n".join(parts) or "[no_results] No citable source was found."