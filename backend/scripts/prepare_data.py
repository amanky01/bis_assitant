#!/usr/bin/env python3
"""
scripts/prepare_data.py
────────────────────────
Reads raw PDFs and HTML files from data/raw/,
extracts + cleans text, chunks it, and writes data/bis_knowledge.json
ready for seed_vector_db.py.

Usage:
  python scripts/prepare_data.py                  # process all files in data/raw/
  python scripts/prepare_data.py --preview        # show first chunk of each doc
  python scripts/prepare_data.py --chunk-size 600 # override chunk size

Requirements:
  pip install pymupdf beautifulsoup4 lxml rich

Output format (data/bis_knowledge.json):
[
  {
    "title": "BIS ISI Mark Scheme — Overview",
    "content": "The ISI mark is India's most prominent...",
    "source": "data/raw/pdfs/bis_isi_scheme.pdf",
    "category": "marks",
    "is_number": null,
    "tags": ["isi", "mark", "certification"]
  },
  ...
]
"""
import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import fitz  # PyMuPDF
except ImportError:
    print("ERROR: Install PyMuPDF → pip install pymupdf")
    sys.exit(1)

from bs4 import BeautifulSoup
from rich.console import Console
from rich.table import Table

from app.data.chunking import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    clean_text,
    chunk_text,
    detect_category,
    detect_tags,
    extract_is_number,
)

console = Console()

# ── Directories ───────────────────────────────────────────────────────────────

RAW_DIR = PROJECT_ROOT / "data" / "raw"
PDF_DIR = RAW_DIR / "pdfs"
HTML_DIR = RAW_DIR / "html"
OUTPUT_FILE = PROJECT_ROOT / "data" / "bis_knowledge.json"
IS_STANDARDS_OUTPUT = PROJECT_ROOT / "data" / "is_standards_map.json"
CURATED_KNOWLEDGE_FILE = PROJECT_ROOT / "data" / "curated_bis_knowledge.json"
CRAWLED_KNOWLEDGE_FILE = PROJECT_ROOT / "data" / "crawled_knowledge.json"

# ── PDF extraction ────────────────────────────────────────────────────────────

def extract_pdf(path: Path) -> list[dict]:
    """Extract text from a PDF and return structured chunks."""
    console.print(f"  [cyan]PDF[/cyan] {path.name}")

    try:
        doc = fitz.open(str(path))
    except Exception as exc:
        console.print(f"    [red]Failed to open: {exc}[/red]")
        return []

    # Extract full text
    full_text = ""
    for page_num in range(len(doc)):
        page = doc[page_num]
        full_text += page.get_text() + "\n\n"

    doc.close()

    if not full_text.strip():
        console.print(f"    [yellow]No text extracted (may be scanned PDF)[/yellow]")
        return []

    full_text = clean_text(full_text)
    chunks = chunk_text(full_text)

    if not chunks:
        return []

    # Detect category from the whole document first
    doc_category = detect_category(full_text[:3000])

    # Build document name from filename
    doc_name = path.stem.replace("_", " ").replace("-", " ").title()

    records = []
    for i, chunk in enumerate(chunks):
        category = detect_category(chunk) if i > 0 else doc_category
        records.append({
            "title": f"{doc_name} — Part {i + 1}" if len(chunks) > 1 else doc_name,
            "content": chunk,
            "source": f"data/raw/pdfs/{path.name}",
            "category": category,
            "is_number": extract_is_number(chunk),
            "tags": detect_tags(chunk, category),
        })

    console.print(f"    → [green]{len(records)} chunks[/green]")
    return records


# ── HTML extraction ───────────────────────────────────────────────────────────

def extract_html(path: Path) -> list[dict]:
    """Extract text from a saved HTML file."""
    console.print(f"  [magenta]HTML[/magenta] {path.name}")

    try:
        html = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        console.print(f"    [red]Failed to read: {exc}[/red]")
        return []

    soup = BeautifulSoup(html, "lxml")

    # Remove navigation, scripts, styles
    for tag in soup(["nav", "script", "style", "footer", "header", "aside"]):
        tag.decompose()

    # Try to get main content
    main = (
        soup.find("main")
        or soup.find("article")
        or soup.find("div", class_=re.compile(r"content|main|body", re.I))
        or soup.find("body")
    )
    text = main.get_text(separator="\n") if main else soup.get_text(separator="\n")
    text = clean_text(text)

    if len(text) < 100:
        console.print(f"    [yellow]Too little text extracted[/yellow]")
        return []

    chunks = chunk_text(text)
    doc_name = path.stem.replace("_", " ").replace("-", " ").title()
    doc_category = detect_category(text[:3000])

    records = []
    for i, chunk in enumerate(chunks):
        category = detect_category(chunk) if i > 0 else doc_category
        records.append({
            "title": f"{doc_name} — Part {i + 1}" if len(chunks) > 1 else doc_name,
            "content": chunk,
            "source": f"data/raw/html/{path.name}",
            "category": category,
            "is_number": extract_is_number(chunk),
            "tags": detect_tags(chunk, category),
        })

    console.print(f"    → [green]{len(records)} chunks[/green]")
    return records


# ── IS Standards map extraction ───────────────────────────────────────────────

def build_is_standards_map(all_records: list[dict]) -> list[dict]:
    """
    Extract IS numbers from all records and build a standards map.
    This populates the is_standards MongoDB collection.
    """
    standards: dict[str, dict] = {}

    for record in all_records:
        is_num = record.get("is_number")
        if not is_num:
            continue

        content_lower = record["content"].lower()
        category = record["category"]

        if is_num not in standards:
            standards[is_num] = {
                "is_number": is_num,
                "title": record["title"],
                "description": record["content"][:300],
                "categories": [],
                "safety_critical": any(
                    kw in content_lower
                    for kw in ["mandatory", "compulsory", "safety", "critical", "lpg", "gas", "electrical", "cable"]
                ),
                "mandatory": "mandatory" in content_lower or "compulsory" in content_lower,
            }

        # Add category if not already there
        std = standards[is_num]
        if category not in std["categories"]:
            std["categories"].append(category)

        # Add any product-type keywords from tags
        for tag in record.get("tags", []):
            if tag not in std["categories"] and not tag.startswith("IS "):
                std["categories"].append(tag)

    return list(standards.values())


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare BIS knowledge base for seeding")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--overlap", type=int, default=DEFAULT_CHUNK_OVERLAP)
    parser.add_argument("--preview", action="store_true", help="Print first chunk of each doc")
    parser.add_argument("--include-crawled", action="store_true", help="Merge data/crawled_knowledge.json from crawl_bis_sites.py")
    args = parser.parse_args()

    console.rule("[bold]BIS Data Preparation[/bold]")

    # Create directories if they don't exist
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    HTML_DIR.mkdir(parents=True, exist_ok=True)

    # Check if any files exist (curated is always merged if present)
    pdfs = list(PDF_DIR.glob("*.pdf"))
    htmls = list(HTML_DIR.glob("*.html"))
    curated_records: list[dict] = []
    if CURATED_KNOWLEDGE_FILE.exists():
        try:
            with open(CURATED_KNOWLEDGE_FILE, encoding="utf-8") as f:
                curated_records = json.load(f)
            console.print(f"Loaded [bold]{len(curated_records)}[/bold] curated BIS knowledge chunks")
        except Exception as e:
            console.print(f"[yellow]Could not load curated knowledge: {e}[/yellow]")

    crawled_records: list[dict] = []
    if getattr(args, "include_crawled", False) and CRAWLED_KNOWLEDGE_FILE.exists():
        try:
            with open(CRAWLED_KNOWLEDGE_FILE, encoding="utf-8") as f:
                crawled_records = json.load(f)
            console.print(f"Loaded [bold]{len(crawled_records)}[/bold] crawled BIS knowledge chunks")
        except Exception as e:
            console.print(f"[yellow]Could not load crawled knowledge: {e}[/yellow]")

    if not pdfs and not htmls and not curated_records and not crawled_records:
        console.print(f"\n[yellow]No files found in data/raw/, no curated data, and no crawled data.[/yellow]")
        console.print(f"\nOptions:")
        console.print(f"  1. Run [cyan]python scripts/download_bis_docs.py[/cyan] then run this script again")
        console.print(f"  2. Put PDFs in [cyan]{PDF_DIR}[/cyan] and HTML in [cyan]{HTML_DIR}[/cyan]")
        console.print(f"  3. Ensure [cyan]{CURATED_KNOWLEDGE_FILE}[/cyan] exists for baseline RAG content")
        console.print(f"  4. Run [cyan]python scripts/crawl_bis_sites.py[/cyan] then [cyan]prepare_data.py --include-crawled[/cyan]")
        sys.exit(0)

    console.print(f"Found [bold]{len(pdfs)}[/bold] PDFs, [bold]{len(htmls)}[/bold] HTML files\n")

    all_records: list[dict] = list(curated_records)
    all_records.extend(crawled_records)

    # Process PDFs
    if pdfs:
        console.print("[bold]Processing PDFs:[/bold]")
        for pdf in sorted(pdfs):
            records = extract_pdf(pdf)
            all_records.extend(records)

    # Process HTML
    if htmls:
        console.print("\n[bold]Processing HTML files:[/bold]")
        for html in sorted(htmls):
            records = extract_html(html)
            all_records.extend(records)

    if not all_records:
        console.print("\n[red]No records extracted. Check your files or curated_bis_knowledge.json.[/red]")
        sys.exit(1)

    # Preview mode
    if args.preview:
        console.print("\n[bold]First chunk preview:[/bold]")
        seen_sources = set()
        for r in all_records:
            src = r["source"]
            if src not in seen_sources:
                seen_sources.add(src)
                console.print(f"\n[cyan]{r['title']}[/cyan]")
                console.print(f"Category: {r['category']} | Tags: {r['tags']}")
                console.print(r["content"][:300] + "...")

    # Write bis_knowledge.json
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)

    # Build and write is_standards_map.json
    standards_map = build_is_standards_map(all_records)
    with open(IS_STANDARDS_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(standards_map, f, ensure_ascii=False, indent=2)

    # Summary table
    console.print()
    table = Table(title="Preparation Complete", border_style="green")
    table.add_column("Metric", style="bold")
    table.add_column("Value", style="green")
    table.add_row("Total chunks", str(len(all_records)))
    table.add_row("Curated chunks", str(len(curated_records)))
    table.add_row("Crawled chunks", str(len(crawled_records)))
    table.add_row("PDF files processed", str(len(pdfs)))
    table.add_row("HTML files processed", str(len(htmls)))
    table.add_row("IS standards found", str(len(standards_map)))
    table.add_row("Output", str(OUTPUT_FILE))
    table.add_row("Standards map", str(IS_STANDARDS_OUTPUT))
    console.print(table)

    # Category breakdown
    categories: dict[str, int] = {}
    for r in all_records:
        categories[r["category"]] = categories.get(r["category"], 0) + 1
    console.print("\n[bold]Chunks by category:[/bold]")
    for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
        console.print(f"  {cat:<20} {count}")

    console.print(f"\n[bold green]✓ Ready to seed![/bold green]")
    console.print("Next step:")
    console.print("  [cyan]python scripts/seed_vector_db.py --clear[/cyan]")
    console.print("  (Use [cyan]--clear[/cyan] to replace existing vectors when you have new/crawled content)")
    console.print("  [cyan]python scripts/seed_vector_db.py --collection is_standards --source data/is_standards_map.json[/cyan]")


if __name__ == "__main__":
    main()
