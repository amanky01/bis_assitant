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

console = Console()

# ── Directories ───────────────────────────────────────────────────────────────

RAW_DIR = PROJECT_ROOT / "data" / "raw"
PDF_DIR = RAW_DIR / "pdfs"
HTML_DIR = RAW_DIR / "html"
OUTPUT_FILE = PROJECT_ROOT / "data" / "bis_knowledge.json"
IS_STANDARDS_OUTPUT = PROJECT_ROOT / "data" / "is_standards_map.json"
CURATED_KNOWLEDGE_FILE = PROJECT_ROOT / "data" / "curated_bis_knowledge.json"

# ── Chunking config ───────────────────────────────────────────────────────────

DEFAULT_CHUNK_SIZE = 500     # characters per chunk
DEFAULT_CHUNK_OVERLAP = 80   # overlap between consecutive chunks

# ── Category detection ────────────────────────────────────────────────────────

CATEGORY_RULES: list[tuple[list[str], str]] = [
    (["hallmark", "huid", "gold", "jewellery", "jewelry", "ahc"], "hallmarking"),
    (["crs", "r-number", "electronics", "it product", "mobile", "laptop", "led"], "crs"),
    (["isi mark", "cm/l", "cml", "compulsory", "mandatory", "product certification"], "marks"),
    (["fmcs", "foreign manufacturer", "import"], "fmcs"),
    (["bee", "star rating", "energy efficiency", "bee label"], "energy"),
    (["lpg", "gas cylinder", "regulator", "petroleum"], "safety"),
    (["cable", "wire", "electrical", "mcb", "socket", "plug"], "electrical"),
    (["cement", "construction", "steel", "building material"], "construction"),
    (["toy", "helmet", "child", "children"], "safety"),
    (["certification process", "how to apply", "license", "application fee", "factory audit"], "compliance"),
    (["is standard", "indian standard", "is number", "bureau of indian standards"], "standards"),
]

IS_NUMBER_RE = re.compile(r"\bIS[:\s]?(\d{3,6})\b", re.IGNORECASE)

TAG_RULES: dict[str, list[str]] = {
    "hallmarking": ["hallmark", "huid", "gold", "jewellery"],
    "crs": ["crs", "r-number", "electronics", "registration"],
    "marks": ["isi", "cml", "certification", "mark"],
    "compliance": ["certification", "process", "apply", "license"],
    "electrical": ["cable", "electrical", "mcb", "switch"],
    "safety": ["safety", "mandatory", "critical"],
    "fmcs": ["fmcs", "foreign", "import"],
    "energy": ["bee", "star rating", "energy"],
    "standards": ["is standard", "indian standard"],
    "construction": ["cement", "steel", "construction"],
}


def detect_category(text: str) -> str:
    text_lower = text.lower()
    for keywords, category in CATEGORY_RULES:
        if any(kw in text_lower for kw in keywords):
            return category
    return "general"


def detect_tags(text: str, category: str) -> list[str]:
    tags = TAG_RULES.get(category, []).copy()
    # Add any IS numbers found
    for m in IS_NUMBER_RE.finditer(text):
        tag = f"IS {m.group(1)}"
        if tag not in tags:
            tags.append(tag)
    return tags[:8]  # cap


def extract_is_number(text: str) -> str | None:
    m = IS_NUMBER_RE.search(text)
    return f"IS {m.group(1)}" if m else None


# ── Text cleaning ─────────────────────────────────────────────────────────────

def clean_text(text: str) -> str:
    """Remove noise, normalize whitespace."""
    # Remove page numbers (e.g. "Page 4 of 22")
    text = re.sub(r"Page\s+\d+\s+of\s+\d+", "", text, flags=re.IGNORECASE)
    # Remove excessive whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    # Remove header/footer noise (lines that are just numbers or short symbols)
    lines = [l for l in text.splitlines() if len(l.strip()) > 3 or l.strip() == ""]
    text = "\n".join(lines)
    return text.strip()


# ── Chunking ──────────────────────────────────────────────────────────────────

def chunk_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    """
    Split text into overlapping chunks.
    Tries to split on paragraph boundaries first, then sentence boundaries.
    """
    # Try paragraph splits first
    paragraphs = [p.strip() for p in re.split(r"\n\n+", text) if p.strip()]

    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        # If adding this paragraph keeps us under chunk_size
        if len(current) + len(para) + 2 <= chunk_size:
            current = (current + "\n\n" + para).strip()
        else:
            # Save current chunk if non-empty
            if current:
                chunks.append(current)
            # If the paragraph itself is too long, split it by sentences
            if len(para) > chunk_size:
                sentences = re.split(r"(?<=[.!?])\s+", para)
                sub = ""
                for sent in sentences:
                    if len(sub) + len(sent) + 1 <= chunk_size:
                        sub = (sub + " " + sent).strip()
                    else:
                        if sub:
                            chunks.append(sub)
                        sub = sent
                if sub:
                    chunks.append(sub)
                # Start new current with overlap from last chunk
                current = chunks[-1][-overlap:] if chunks else ""
            else:
                # Start new chunk with overlap
                overlap_text = current[-overlap:] if current else ""
                current = (overlap_text + "\n\n" + para).strip()

    if current:
        chunks.append(current)

    # Filter out very short chunks (likely noise)
    return [c for c in chunks if len(c) >= 80]


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

    if not pdfs and not htmls and not curated_records:
        console.print(f"\n[yellow]No files found in data/raw/ and no curated data.[/yellow]")
        console.print(f"\nOptions:")
        console.print(f"  1. Run [cyan]python scripts/download_bis_docs.py[/cyan] then run this script again")
        console.print(f"  2. Put PDFs in [cyan]{PDF_DIR}[/cyan] and HTML in [cyan]{HTML_DIR}[/cyan]")
        console.print(f"  3. Ensure [cyan]{CURATED_KNOWLEDGE_FILE}[/cyan] exists for baseline RAG content")
        sys.exit(0)

    console.print(f"Found [bold]{len(pdfs)}[/bold] PDFs, [bold]{len(htmls)}[/bold] HTML files\n")

    all_records: list[dict] = list(curated_records)

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
    console.print("  [cyan]python scripts/seed_vector_db.py[/cyan]")
    console.print("  [cyan]python scripts/seed_vector_db.py --collection is_standards --source data/is_standards_map.json[/cyan]")


if __name__ == "__main__":
    main()
