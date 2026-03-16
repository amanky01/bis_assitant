#!/usr/bin/env python3
"""
scripts/download_bis_docs.py
─────────────────────────────
Downloads BIS-related PDFs and saves HTML pages directly
into data/raw/pdfs/ and data/raw/html/.

Usage:
    python scripts/download_bis_docs.py
    python scripts/download_bis_docs.py --dry-run    # just print what would be downloaded
    python scripts/download_bis_docs.py --skip-existing  # skip already downloaded files

After this runs, proceed with:
    python scripts/prepare_data.py
    python scripts/seed_vector_db.py
"""
import argparse
import asyncio
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

PDF_DIR = PROJECT_ROOT / "data" / "raw" / "pdfs"
HTML_DIR = PROJECT_ROOT / "data" / "raw" / "html"

try:
    import httpx
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TransferSpeedColumn,
    )
    from rich.table import Table
except ImportError:
    print("Missing deps. Run: pip install httpx rich")
    sys.exit(1)

console = Console()

# ── Documents to download ─────────────────────────────────────────────────────
# Format: (filename_to_save_as, url, type)
# type: "pdf" → saved to data/raw/pdfs/
#       "html" → saved to data/raw/html/

DOCUMENTS = [
    # ── BIS Certification Scheme documents ────────────────────────────────
    (
        "bis_product_certification_scheme1.pdf",
        "https://www.bis.gov.in/wp-content/uploads/2020/03/Product-Certification-Scheme-I.pdf",
        "pdf",
    ),
    (
        "bis_crs_scheme.pdf",
        "https://www.bis.gov.in/wp-content/uploads/2020/03/CRS-Scheme.pdf",
        "pdf",
    ),
    (
        "bis_hallmarking_scheme.pdf",
        "https://www.bis.gov.in/wp-content/uploads/2020/09/Hallmarking-Scheme.pdf",
        "pdf",
    ),
    (
        "bis_fmcs_scheme.pdf",
        "https://www.bis.gov.in/wp-content/uploads/2022/01/FMCS-Scheme.pdf",
        "pdf",
    ),
    (
        "bis_eco_mark_scheme.pdf",
        "https://www.bis.gov.in/wp-content/uploads/2020/03/Eco-Mark-Scheme.pdf",
        "pdf",
    ),
    # ── Hallmarking specific ──────────────────────────────────────────────
    (
        "bis_huid_faq.pdf",
        "https://www.bis.gov.in/wp-content/uploads/2021/08/HUID-FAQ.pdf",
        "pdf",
    ),
    (
        "bis_hallmarking_regulations_2018.pdf",
        "https://www.bis.gov.in/wp-content/uploads/2020/05/BIS-Hallmarking-Regulation-2018.pdf",
        "pdf",
    ),
    # ── Standards related ─────────────────────────────────────────────────
    (
        "bis_compulsory_registration_order.pdf",
        "https://www.bis.gov.in/wp-content/uploads/2020/03/Compulsory-Registration-Order.pdf",
        "pdf",
    ),
    # ── BIS Website HTML pages (saved as HTML for richer content) ─────────
    (
        "bis_product_certification_faq.html",
        "https://www.bis.gov.in/product-certification/faq/",
        "html",
    ),
    (
        "bis_hallmarking_faq.html",
        "https://www.bis.gov.in/hallmarking/faq/",
        "html",
    ),
    (
        "bis_crs_faq.html",
        "https://www.bis.gov.in/crs/faq/",
        "html",
    ),
    (
        "bis_mandatory_products_list.html",
        "https://www.bis.gov.in/product-certification/products-under-compulsory-certification/",
        "html",
    ),
    (
        "manakonline_about.html",
        "https://www.manakonline.in/MANAK/home",
        "html",
    ),
    (
        "crsbis_about.html",
        "https://www.crsbis.in/BIS/home.do",
        "html",
    ),
    (
        "beeindia_star_rating.html",
        "https://beeindia.gov.in/en/what-we-do/star-labelling",
        "html",
    ),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
    "Accept-Language": "en-IN,en;q=0.9",
}


def _extract_pdf_links_from_html(html_bytes: bytes, base_url: str) -> list[str]:
    """Find links to .pdf on the same host. Returns absolute URLs."""
    try:
        text = html_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return []
    # Match href="...something.pdf" or href='...pdf'
    pattern = re.compile(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', re.I)
    base = urlparse(base_url)
    out = []
    for match in pattern.finditer(text):
        raw = match.group(1).strip()
        if not raw or "pdf" not in raw.lower():
            continue
        full = urljoin(base_url, raw)
        p = urlparse(full)
        if p.netloc == base.netloc and full not in out:
            out.append(full)
    return out[:5]  # limit retries


async def download_one(
    client: httpx.AsyncClient,
    filename: str,
    url: str,
    doc_type: str,
    skip_existing: bool,
    dry_run: bool,
) -> dict:
    dest_dir = PDF_DIR if doc_type == "pdf" else HTML_DIR
    dest = dest_dir / filename
    result = {
        "filename": filename,
        "url": url,
        "status": "pending",
        "size": 0,
        "error": "",
    }

    if dry_run:
        result["status"] = "dry_run"
        return result

    if skip_existing and dest.exists() and dest.stat().st_size > 1000:
        result["status"] = "skipped"
        result["size"] = dest.stat().st_size
        return result

    try:
        await asyncio.sleep(0.5)
        resp = await client.get(url, headers=HEADERS, follow_redirects=True, timeout=30.0)

        if resp.status_code == 404:
            result["status"] = "not_found"
            result["error"] = "404 — URL may have changed on BIS website"
            return result

        if resp.status_code != 200:
            result["status"] = "error"
            result["error"] = f"HTTP {resp.status_code}"
            return result

        content = resp.content

        if doc_type == "pdf":
            if not content.startswith(b"%PDF"):
                # PDF URL returned HTML: try to find a PDF link on the page
                pdf_links = _extract_pdf_links_from_html(content, url)
                for pdf_url in pdf_links:
                    await asyncio.sleep(0.5)
                    r2 = await client.get(pdf_url, headers=HEADERS, follow_redirects=True, timeout=30.0)
                    if r2.status_code == 200 and r2.content.startswith(b"%PDF") and len(r2.content) >= 5000:
                        dest.write_bytes(r2.content)
                        result["status"] = "ok"
                        result["size"] = len(r2.content)
                        return result
                # No usable PDF: save HTML as fallback so we have content for RAG
                fallback_name = Path(filename).stem + "_fallback.html"
                fallback_path = HTML_DIR / fallback_name
                if len(content) >= 500:
                    HTML_DIR.mkdir(parents=True, exist_ok=True)
                    fallback_path.write_bytes(content)
                    result["status"] = "html_fallback"
                    result["size"] = len(content)
                    result["error"] = ""  # not an error for pipeline
                    result["fallback_file"] = fallback_name
                else:
                    result["status"] = "error"
                    result["error"] = "Response is not a PDF (got HTML); no PDF link found; HTML too small to save"
                return result
            if len(content) < 5000:
                result["status"] = "error"
                result["error"] = f"PDF too small ({len(content)} bytes) — probably an error page"
                return result
        else:  # html
            if len(content) < 500:
                result["status"] = "error"
                result["error"] = f"Response too small ({len(content)} bytes)"
                return result

        dest.write_bytes(content)
        result["status"] = "ok"
        result["size"] = len(content)

    except httpx.TimeoutException:
        result["status"] = "error"
        result["error"] = "Timeout (30s) — BIS site may be slow, try again"
    except httpx.RequestError as exc:
        result["status"] = "error"
        result["error"] = str(exc)

    return result


async def main(dry_run: bool, skip_existing: bool) -> None:
    console.rule("[bold]BIS Document Downloader[/bold]")

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    HTML_DIR.mkdir(parents=True, exist_ok=True)

    console.print(f"PDFs  → [cyan]{PDF_DIR}[/cyan]")
    console.print(f"HTML  → [cyan]{HTML_DIR}[/cyan]")
    console.print(f"Files : [bold]{len(DOCUMENTS)}[/bold]\n")

    if dry_run:
        console.print("[yellow]DRY RUN — nothing will be downloaded[/yellow]\n")
        for fname, url, dtype in DOCUMENTS:
            console.print(f"  [{dtype.upper()}] {fname}")
            console.print(f"         {url}")
        return

    results = []
    async with httpx.AsyncClient(verify=False) as client:  # BIS site sometimes has cert issues
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description:<45}"),
            BarColumn(bar_width=20),
            TextColumn("{task.fields[status]}"),
        ) as progress:
            tasks = {}
            for fname, url, dtype in DOCUMENTS:
                tid = progress.add_task(fname[:44], total=1, status="waiting")
                tasks[fname] = (tid, url, dtype)

            for fname, (tid, url, dtype) in tasks.items():
                progress.update(tid, description=fname[:44], status="[cyan]downloading[/cyan]")
                result = await download_one(
                    client, fname, url, dtype, skip_existing, dry_run
                )
                results.append(result)

                status_str = {
                    "ok": f"[green]✓ {result['size'] // 1024}KB[/green]",
                    "html_fallback": f"[yellow]saved as HTML ({result.get('fallback_file', '')})[/yellow]",
                    "skipped": "[yellow]skipped (exists)[/yellow]",
                    "not_found": "[red]404 not found[/red]",
                    "error": f"[red]✗ {result['error'][:30]}[/red]",
                }.get(result["status"], result["status"])

                progress.update(tid, completed=1, status=status_str)

    # Summary (html_fallback = saved HTML when PDF failed, still usable for RAG)
    ok = [r for r in results if r["status"] == "ok"]
    html_fallback = [r for r in results if r["status"] == "html_fallback"]
    skipped = [r for r in results if r["status"] == "skipped"]
    failed = [r for r in results if r["status"] in ("error", "not_found")]

    console.print()
    table = Table(title="Download Summary", border_style="green" if not failed else "yellow")
    table.add_column("Status", style="bold")
    table.add_column("Count")
    table.add_row("Downloaded", f"[green]{len(ok)}[/green]")
    table.add_row("HTML fallback (PDF URL returned HTML)", f"[yellow]{len(html_fallback)}[/yellow]")
    table.add_row("Skipped (existing)", f"[yellow]{len(skipped)}[/yellow]")
    table.add_row("Failed", f"[red]{len(failed)}[/red]")
    console.print(table)

    if failed:
        console.print("\n[red]Failed downloads:[/red]")
        for r in failed:
            console.print(f"  ✗ {r['filename']}")
            console.print(f"    {r['error']}")
        console.print(
            "\n[yellow]Tip: BIS website URLs change frequently. "
            "If a PDF fails, go to bis.gov.in manually and download it, "
            "then place it in data/raw/pdfs/ with the filename shown above.[/yellow]"
        )

    if ok or skipped or html_fallback:
        console.print(f"\n[bold green]✓ Ready for next step:[/bold green]")
        console.print("  [cyan]python scripts/prepare_data.py[/cyan]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-existing", action="store_true", default=True)
    args = parser.parse_args()
    asyncio.run(main(args.dry_run, args.skip_existing))
