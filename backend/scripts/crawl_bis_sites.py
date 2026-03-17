#!/usr/bin/env python3
"""
scripts/crawl_bis_sites.py
──────────────────────────
Crawls BIS and Indian standards websites (allowed_domains), extracts content,
chunks and enriches it, and writes data/crawled_knowledge.json for RAG.

Usage:
  python scripts/crawl_bis_sites.py
  python scripts/crawl_bis_sites.py --max-pages 100 --max-chunks 3000
  python scripts/crawl_bis_sites.py --delay 2 --output data/crawled_knowledge.json

Pipeline:
  crawl_bis_sites.py → data/crawled_knowledge.json
  prepare_data.py --include-crawled → data/bis_knowledge.json
  seed_vector_db.py --clear
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from app.core.config import get_settings
from app.data.chunking import (
    clean_text,
    chunk_text,
    detect_category,
    detect_tags,
    extract_is_number,
)

try:
    import httpx
except ImportError:
    print("ERROR: pip install httpx")
    sys.exit(1)
try:
    import trafilatura
except ImportError:
    print("ERROR: pip install trafilatura")
    sys.exit(1)

USER_AGENT = "BISAssistant-Crawler/1.0 (+https://github.com/bis-assistant)"
MIN_TEXT_LEN = 150
DEFAULT_DELAY_SEC = 2
DEFAULT_MAX_PAGES = 200
DEFAULT_MAX_CHUNKS = 5000
OUTPUT_FILE = PROJECT_ROOT / "data" / "crawled_knowledge.json"
SEEDS_FILE = PROJECT_ROOT / "data" / "crawl_seeds.json"

# Skip URL path patterns (login, search, etc.)
SKIP_PATH_PATTERNS = re.compile(
    r"login|logout|search\?|register|signin|signup|cart|ajax|#|\.zip$|\.docx?$|\.xlsx?$",
    re.I
)


def url_slug(url: str, length: int = 8) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:length]


def is_allowed_url(url: str, allowed_domains: list[str]) -> bool:
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.netloc:
            return False
        host = p.netloc.lower().lstrip("www.")
        if any(host == d or host.endswith("." + d) for d in allowed_domains):
            return not SKIP_PATH_PATTERNS.search(url)
    except Exception:
        pass
    return False


def normalize_url(url: str, current_base: str) -> str:
    u = urljoin(current_base, url)
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}{p.path}" if p.path else f"{p.scheme}://{p.netloc}/"


def get_robots_parser(domain: str, scheme: str = "https") -> RobotFileParser:
    rp = RobotFileParser()
    rp.set_url(f"{scheme}://{domain}/robots.txt")
    try:
        rp.read()
    except Exception:
        pass
    return rp


async def fetch_html(url: str) -> tuple[str, str]:
    """Fetch HTML with Playwright, return (raw_html, final_url)."""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(user_agent=USER_AGENT)
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(1500)
        html = await page.content()
        final_url = page.url
        await browser.close()
    return html, final_url


async def fetch_pdf(url: str) -> str:
    """Fetch PDF and extract text with PyMuPDF."""
    import fitz
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        if "pdf" not in (resp.headers.get("content-type") or "").lower() and not url.lower().endswith(".pdf"):
            raise ValueError("Not a PDF")
        doc = fitz.open(stream=io.BytesIO(resp.content), filetype="pdf")
        parts = []
        for i in range(min(len(doc), 15)):
            t = doc[i].get_text()
            if t.strip():
                parts.append(t)
        doc.close()
        text = "\n\n".join(parts)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text[:5000]


def extract_links(html: str, base_url: str, allowed_domains: list[str]) -> list[str]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        full = normalize_url(href, base_url)
        if is_allowed_url(full, allowed_domains):
            links.append(full)
    return list(dict.fromkeys(links))


def extract_title_and_text(html: str, url: str) -> tuple[str, str] | None:
    from bs4 import BeautifulSoup
    text = trafilatura.extract(html, include_links=False, include_tables=True, no_fallback=False)
    if not text or len(text) < MIN_TEXT_LEN:
        return None
    soup = BeautifulSoup(html, "lxml")
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()[:200]
    if not title and soup.find("h1"):
        h1 = soup.find("h1")
        if h1.get_text():
            title = h1.get_text().strip()[:200]
    if not title:
        title = urlparse(url).path.strip("/").replace("/", " — ") or urlparse(url).netloc
    text = clean_text(text)
    if len(text) < MIN_TEXT_LEN:
        return None
    return title, text


def page_to_chunks(title: str, body: str, source_url: str) -> list[dict]:
    """Turn a page (title, body, url) into RAG chunk dicts with unique titles."""
    chunks = chunk_text(body)
    if not chunks:
        return []
    slug = url_slug(source_url)
    doc_category = detect_category(body[:3000])
    records = []
    for i, chunk in enumerate(chunks):
        cat = detect_category(chunk) if i > 0 else doc_category
        chunk_title = f"{title} — Part {i + 1} [{slug}]" if len(chunks) > 1 else f"{title} [{slug}]"
        records.append({
            "title": chunk_title,
            "content": chunk,
            "source": source_url,
            "category": cat,
            "is_number": extract_is_number(chunk),
            "tags": detect_tags(chunk, cat),
        })
    return records


async def crawl(
    seed_urls: list[str],
    allowed_domains: list[str],
    delay_sec: float,
    max_pages: int,
    max_chunks: int,
    output_path: Path,
) -> None:
    visited: set[str] = set()
    queue: list[str] = list(seed_urls)
    all_records: list[dict] = []
    robots_cache: dict[str, RobotFileParser] = {}
    total_chunks = 0

    while queue and len(visited) < max_pages and total_chunks < max_chunks:
        url = queue.pop(0)
        url_norm = normalize_url(url, url)
        if url_norm in visited:
            continue
        visited.add(url_norm)

        domain = urlparse(url_norm).netloc
        if domain not in robots_cache:
            robots_cache[domain] = get_robots_parser(domain)
        if not robots_cache[domain].can_fetch(USER_AGENT, url_norm):
            continue

        await asyncio.sleep(delay_sec)

        is_pdf = url_norm.lower().endswith(".pdf")
        try:
            if is_pdf:
                text = await fetch_pdf(url_norm)
                title = Path(urlparse(url_norm).path).stem.replace("-", " ").replace("_", " ").title()
                body = clean_text(text)
                if len(body) < MIN_TEXT_LEN:
                    continue
                records = page_to_chunks(title, body, url_norm)
            else:
                html, final_url = await fetch_html(url_norm)
                if final_url != url_norm:
                    final_url = normalize_url(final_url, final_url)
                    if final_url in visited:
                        continue
                out = extract_title_and_text(html, final_url or url_norm)
                if not out:
                    continue
                title, body = out
                records = page_to_chunks(title, body, final_url or url_norm)
                if final_url and final_url != url_norm:
                    visited.add(final_url)
                links = extract_links(html, final_url or url_norm, allowed_domains)
                for link in links:
                    if link not in visited and link not in queue:
                        queue.append(link)
        except Exception as e:
            print(f"  [skip] {url_norm[:70]}… — {e}", file=sys.stderr)
            continue

        for r in records:
            if total_chunks >= max_chunks:
                break
            all_records.append(r)
            total_chunks += 1
        if records:
            print(f"  [ok] {url_norm[:65]}… → {len(records)} chunks (total {total_chunks})")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)
    print(f"\nWrote {len(all_records)} chunks to {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl BIS/standards sites for RAG")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--max-chunks", type=int, default=DEFAULT_MAX_CHUNKS)
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SEC)
    parser.add_argument("--output", type=Path, default=OUTPUT_FILE)
    parser.add_argument("--seeds", type=Path, default=SEEDS_FILE)
    args = parser.parse_args()

    settings = get_settings()
    allowed_domains = list(settings.allowed_domains)

    if args.seeds.exists():
        with open(args.seeds, encoding="utf-8") as f:
            data = json.load(f)
        seed_urls = data.get("seed_urls", [])
    else:
        seed_urls = [
            "https://www.bis.gov.in/",
            "https://www.bis.gov.in/hallmarking/",
            "https://www.bis.gov.in/crs/",
            "https://www.manakonline.in/",
            "https://crsbis.in/",
        ]
    seed_urls = [u for u in seed_urls if is_allowed_url(u, allowed_domains)]
    if not seed_urls:
        print("No seed URLs allowed by allowed_domains.", file=sys.stderr)
        sys.exit(1)

    print(f"Crawling up to {args.max_pages} pages, {args.max_chunks} chunks, delay={args.delay}s")
    print(f"Seeds: {len(seed_urls)} | Domains: {', '.join(allowed_domains[:4])}…")
    asyncio.run(crawl(seed_urls, allowed_domains, args.delay, args.max_pages, args.max_chunks, args.output))


if __name__ == "__main__":
    main()
