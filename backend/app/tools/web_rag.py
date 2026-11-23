"""
app/tools/web_rag.py
─────────────────────
Tool 2: Hybrid Web RAG — fallback when vector DB has no answer.

Pipeline:
  1. Web search (Tavily if API key set, else DuckDuckGo via ddgs) → discovers URLs
  2. Results re-ranked by URL depth so deep links (e.g. /hallmarking/faq) rank above domain root
  3. scrape_page → fetches actual page content (PDF via PyMuPDF, HTML via Trafilatura)

Improvements:
  - Re-rank by URL path depth to prefer specific pages over homepage.
  - Tavily: optional search_depth="advanced" for better relevance/URLs.
  - DuckDuckGo (ddgs): open-source fallback when TAVILY_API_KEY is not set.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import re
from urllib.parse import urlparse

import httpx
import trafilatura
from langchain_core.tools import tool

from app.core.config import get_settings
from app.core.evidence import register_evidence
from app.core.exceptions import DomainNotAllowedError, ScraperError, WebSearchError
from app.core.logging import get_logger
from app.core.official_http import fetch_official

logger = get_logger(__name__)
settings = get_settings()


def _url_depth(url: str) -> int:
    """Return path depth (segment count) so deeper URLs get higher score. Prefer /hallmarking/faq over /."""
    try:
        parsed = urlparse(url)
        path = (parsed.path or "").strip("/")
        return len([p for p in path.split("/") if p]) if path else 0
    except Exception:
        return 0


def _rerank_by_depth(results: list[dict], url_key: str = "url") -> list[dict]:
    """Sort results so deeper (more specific) URLs come first. Reduces domain-only / homepage results."""
    return sorted(
        results,
        key=lambda r: _url_depth(r.get(url_key) or r.get("href") or ""),
        reverse=True,
    )


def _extract_page(body: bytes, url: str) -> str:
    if body.startswith(b"%PDF-"):
        import fitz

        with fitz.open(stream=body, filetype="pdf") as document:
            text = "\n\n".join(document[i].get_text() for i in range(min(len(document), 15)))
            text = re.sub(r"\n{3,}", "\n\n", text)
    elif urlparse(url).path.lower().endswith(".pdf"):
        raise ScraperError("Official source did not return a PDF")
    else:
        text = trafilatura.extract(body, include_links=False, include_tables=True)
    if not text or not text.strip():
        raise ScraperError("No usable text extracted from official source")
    return text[:5000]


async def _fetch_page(url: str) -> tuple[str, str]:
    body, final_url = await fetch_official(url, allow_redirects=True, max_bytes=8_000_000)
    return await asyncio.to_thread(_extract_page, body, final_url), final_url


# ── Search backends ───────────────────────────────────────────────────────────

async def _search_tavily(query: str) -> list[dict]:
    """Return official search leads from the bounded Tavily API request."""
    depth = settings.web_search_depth
    async with asyncio.timeout(12), httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {settings.tavily_api_key}"},
            json={
                "query": query,
                "search_depth": depth if depth in {"basic", "advanced"} else "basic",
                "max_results": 10,
                "include_domains": settings.allowed_domains,
            },
        )
        response.raise_for_status()
        response = response.json()
    results = response.get("results", [])
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")}
        for r in results
    ]


def _search_duckduckgo_sync(query: str) -> list[dict]:
    """DuckDuckGo (ddgs) search; filter to allowed domains. Returns list of {title, url, content}. Open-source, no API key."""
    try:
        from ddgs import DDGS
    except ImportError:
        raise WebSearchError(
            "DuckDuckGo fallback requires: pip install ddgs. Or set TAVILY_API_KEY for Tavily."
        )
    raw = DDGS(timeout=5).text(query, max_results=15, region="in-en")
    out = []
    for r in raw:
        url = r.get("href") or r.get("url") or ""
        if not url or not settings.is_domain_allowed(url):
            continue
        out.append({
            "title": r.get("title", ""),
            "url": url,
            "content": r.get("body", "") or r.get("content", ""),
        })
    return out


# ── Tools ─────────────────────────────────────────────────────────────────────

@tool
async def web_search_bis(query: str) -> str:
    """
    Search BIS-related websites (bis.gov.in, manakonline.in, crsbis.in,
    huid.manakonline.in, bis.org.in, beeindia.gov.in) for information
    not found in the static knowledge base.
    Use when search_bis_knowledge returns no results, or when the user asks
    for something specific (e.g. startup guidelines, a specific document/circular/
    amendment) that the knowledge base results do not clearly cover. Do not treat
    this as fallback-only — use it whenever the knowledge base does not directly
    answer the question.
    Returns URLs and snippets; results are re-ranked to prefer specific page URLs over domain homepages.
    """
    if len(query) > 3000:
        return "[search_error] Query exceeds search limits."
    if settings.tavily_api_key:
        try:
            results = await _search_tavily(query)
            source = "Tavily"
        except Exception as exc:
            logger.warning("Tavily unavailable (%s)", type(exc).__name__)
            return "[search_error] Official web search temporarily unavailable."
    else:
        try:
            results = await asyncio.to_thread(_search_duckduckgo_sync, query)
            source = "DuckDuckGo"
        except Exception as exc:
            logger.warning("DuckDuckGo unavailable (%s)", type(exc).__name__)
            return "[search_error] Official web search temporarily unavailable."

    results = [r for r in results if settings.is_domain_allowed(r["url"])]
    if not results:
        return "[no_web_results] No results on allowed BIS domains."

    results = _rerank_by_depth(results, url_key="url")
    results = results[:8]

    logger.debug("Official search provider=%s result_count=%s", source, len(results))

    parts = []
    for r in results:
        parts.append(
            f"Title: {r.get('title', '')}\n"
            f"URL: {r.get('url', '')}\n"
            f"Snippet: {r.get('content', '')}"
        )
    return "\n\n---\n\n".join(parts)


@tool
async def scrape_page(url: str) -> str:
    """
    Fetch and extract the full text content of a specific BIS-related page or PDF.
    Works on both HTML pages and PDF documents.
    Only works on allowed domains: bis.gov.in, bis.org.in, manakonline.in,
    crsbis.in, huid.manakonline.in, beeindia.gov.in.
    Use this after web_search_bis to get full content from a promising URL.
    For PDF URLs (ending in .pdf), extracts text directly from the PDF document.
    """
    try:
        text, final_url = await _fetch_page(url)
        ref = register_evidence(
            final_url, urlparse(final_url).hostname or "Official BIS source",
            retrieved_at=datetime.now(timezone.utc).isoformat(),
        )
        return f"Untrusted reference [{ref}] from {final_url}\n\n{text}"
    except DomainNotAllowedError as exc:
        return f"[domain_blocked] {exc.message}"
    except ScraperError as exc:
        return f"[scrape_error] {exc.message}"
    except Exception as exc:
        logger.warning("Official fetch failed (%s)", type(exc).__name__)
        return "[scrape_error] Could not read official source"