"""
app/tools/web_rag.py
─────────────────────
Tool 2: Hybrid Web RAG — fallback when vector DB has no answer.

Pipeline:
  1. Web search (Tavily if API key set, else DuckDuckGo via ddgs) → discovers URLs
  2. Results re-ranked by URL depth so deep links (e.g. /hallmarking/faq) rank above domain root
  3. scrape_page → fetches actual page content (PDF via PyMuPDF, HTML via Playwright + Trafilatura)

Improvements:
  - Re-rank by URL path depth to prefer specific pages over homepage.
  - Tavily: optional search_depth="advanced" for better relevance/URLs.
  - DuckDuckGo (ddgs): open-source fallback when TAVILY_API_KEY is not set.
"""
from __future__ import annotations

import asyncio
import io
from urllib.parse import urlparse

import trafilatura
from langchain_core.tools import tool

from app.core.config import get_settings
from app.core.exceptions import DomainNotAllowedError, ScraperError, WebSearchError
from app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

_tavily = None


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


def _get_tavily():
    global _tavily
    if _tavily is None:
        from tavily import TavilyClient
        if not settings.tavily_api_key:
            raise WebSearchError("TAVILY_API_KEY not configured")
        _tavily = TavilyClient(api_key=settings.tavily_api_key)
    return _tavily


def _assert_allowed(url: str) -> None:
    if not settings.is_domain_allowed(url):
        raise DomainNotAllowedError(
            f"Domain not in whitelist: {urlparse(url).hostname}"
        )


def _is_pdf_url(url: str) -> bool:
    """Detect PDF by URL path extension."""
    return urlparse(url).path.lower().endswith(".pdf")


async def _fetch_pdf(url: str) -> str:
    """
    Download a PDF with httpx and extract text with PyMuPDF.
    Falls back to a short error message if fitz is not installed.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise ScraperError(
            "PyMuPDF not installed — cannot read PDFs. "
            "Run: pip install pymupdf"
        )

    import httpx
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; BISAssistant/1.0)"},
            )
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "html" in content_type and b"%PDF" not in resp.content[:10]:
                raise ScraperError(f"URL returned HTML not PDF (Cloudflare block?): {url}")
            pdf_bytes = resp.content
    except httpx.HTTPError as exc:
        raise ScraperError(f"HTTP error fetching PDF {url}: {exc}") from exc

    try:
        doc = fitz.open(stream=io.BytesIO(pdf_bytes), filetype="pdf")
        pages_text = []
        for page_num in range(min(len(doc), 15)):   # cap at 15 pages
            text = doc[page_num].get_text()
            if text.strip():
                pages_text.append(text)
        doc.close()
        full_text = "\n\n".join(pages_text)
    except Exception as exc:
        raise ScraperError(f"PyMuPDF failed to parse {url}: {exc}") from exc

    if not full_text.strip():
        raise ScraperError(f"No text extracted from PDF: {url}")

    # Clean and cap
    import re
    full_text = re.sub(r"\n{3,}", "\n\n", full_text)
    full_text = re.sub(r"[ \t]{2,}", " ", full_text)
    return full_text[:5000]


async def _fetch_html(url: str) -> str:
    """Fetch an HTML page with Playwright and extract clean text with Trafilatura."""
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            )
            await page.goto(url, wait_until="networkidle", timeout=20000)
            await page.wait_for_timeout(1000)
            html = await page.content()
            await browser.close()
    except Exception as exc:
        raise ScraperError(f"Playwright failed for {url}: {exc}") from exc

    text = trafilatura.extract(
        html,
        include_links=False,
        include_tables=True,
        no_fallback=False,
    )

    if not text or len(text) < 100:
        raise ScraperError(f"No usable text extracted from {url}")

    return text[:5000]


async def _fetch_page(url: str) -> str:
    """Route to PDF or HTML fetcher based on URL."""
    _assert_allowed(url)
    if _is_pdf_url(url):
        logger.debug(f"[web_rag] Detected PDF URL: {url}")
        return await _fetch_pdf(url)
    else:
        return await _fetch_html(url)


# ── Search backends ───────────────────────────────────────────────────────────

def _search_tavily_sync(query: str) -> list[dict]:
    """Tavily search restricted to allowed domains. Returns list of {title, url, content}."""
    client = _get_tavily()
    depth = getattr(settings, "web_search_depth", "advanced") or "advanced"
    if depth not in ("basic", "advanced"):
        depth = "advanced"
    response = client.search(
        query=query,
        search_depth=depth,
        max_results=10,
        include_domains=settings.allowed_domains,
    )
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
    raw = DDGS().text(query, max_results=15, region="in-en")
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
    if settings.tavily_api_key:
        try:
            results = await asyncio.to_thread(_search_tavily_sync, query)
            source = "Tavily"
        except WebSearchError:
            raise
        except Exception as exc:
            raise WebSearchError(f"Tavily search failed: {exc}") from exc
    else:
        try:
            results = await asyncio.to_thread(_search_duckduckgo_sync, query)
            source = "DuckDuckGo"
        except WebSearchError:
            raise
        except Exception as exc:
            raise WebSearchError(f"DuckDuckGo search failed: {exc}") from exc

    if not results:
        return "[no_web_results] No results on allowed BIS domains."

    results = _rerank_by_depth(results, url_key="url")
    results = results[:8]

    logger.debug(f"[web_rag] {source} '{query[:50]}' → {len(results)} results (deep links first)")

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
        text = await _fetch_page(url)
        logger.debug(f"[web_rag] Scraped {url} → {len(text)} chars")
        return f"[Content from {url}]\n\n{text}"
    except DomainNotAllowedError as exc:
        return f"[domain_blocked] {exc.message}"
    except ScraperError as exc:
        return f"[scrape_error] {exc.message}"
    except Exception as exc:
        return f"[scrape_error] Unexpected error fetching {url}: {exc}"