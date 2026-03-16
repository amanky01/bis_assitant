#!/usr/bin/env python3
"""
scripts/seed_vector_db.py
──────────────────────────
Seeds MongoDB Atlas with BIS knowledge documents using Gemini embeddings.

Search (search_bis_knowledge) reads from the MongoDB bis_knowledge collection.
If you add or edit data/curated_bis_knowledge.json, run:
  1. python scripts/prepare_data.py     # merges curated → data/bis_knowledge.json
  2. python scripts/seed_vector_db.py --clear   # re-seed MongoDB from bis_knowledge.json

Usage:
  python scripts/seed_vector_db.py                     # seed bis_knowledge
  python scripts/seed_vector_db.py --clear             # wipe + re-seed (use after updating JSON)
  python scripts/seed_vector_db.py --dry-run           # validate only
  python scripts/seed_vector_db.py --source data/custom.json
  python scripts/seed_vector_db.py --collection is_standards --source data/is_standards_map.json

Input JSON format (bis_knowledge):
[
  {
    "title": "ISI Mark Overview",
    "content": "The ISI mark is...",
    "source": "https://www.bis.gov.in/...",
    "category": "marks",
    "is_number": "IS 694",    <- optional
    "tags": ["isi", "mark"]   <- optional
  }
]

Atlas Vector Index (create in UI BEFORE seeding):
  Collection : bis_knowledge
  Index name : bis_knowledge_vector_index
  Field      : embedding
  Dimensions : 3072          <- GEMINI_EMBEDDING_MODEL + EMBEDDING_DIMENSIONS=3072
  Similarity : cosine
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

import asyncio
import google.generativeai as genai

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.mongo import MongoDB

console = Console()
settings = get_settings()
setup_logging("INFO")

DEFAULT_SOURCE = PROJECT_ROOT / "data" / "bis_knowledge.json"
BATCH_SIZE = 5    # conservative — Gemini embedding API has rate limits


# ── Seed-specific embed (retrieval_document task type) ────────────────────────
# IMPORTANT: seed uses task_type="retrieval_document"
#            search uses task_type="retrieval_query"  (in app/services/gemini.py)
# These two task types are a matched pair in Gemini's embedding API.
# Using the wrong type at seed time degrades retrieval quality.

genai.configure(api_key=settings.gemini_api_key if hasattr(settings, "gemini_api_key") else "")

async def _embed_document(text: str) -> list[float]:
    """Embed a document chunk for storage. Uses retrieval_document + same dimensions as app."""
    genai.configure(api_key=settings.gemini_api_key)
    result = await asyncio.to_thread(
        genai.embed_content,
        model=settings.gemini_embedding_model,
        content=text,
        task_type="retrieval_document",
        output_dimensionality=settings.embedding_dimensions,
    )
    return result["embedding"]


async def _embed_batch_documents(texts: list[str]) -> list[list[float]]:
    """Embed a batch of document chunks concurrently."""
    return list(await asyncio.gather(*[_embed_document(t) for t in texts]))


def validate(doc: dict, i: int) -> list[str]:
    errors = []
    if not doc.get("title"):
        errors.append(f"[{i}] Missing 'title'")
    if not doc.get("content") or len(doc.get("content", "")) < 30:
        errors.append(f"[{i}] Missing or too-short 'content'")
    return errors


def embed_text(doc: dict) -> str:
    """Build the text that gets embedded — richer = better retrieval."""
    parts = [doc["title"]]
    if doc.get("is_number"):
        parts.append(f"IS Standard: {doc['is_number']}")
    if doc.get("category"):
        parts.append(f"Category: {doc['category']}")
    if doc.get("tags"):
        parts.append(f"Tags: {', '.join(doc['tags'])}")
    parts.append(doc["content"][:2000])
    return "\n\n".join(parts)


async def seed_knowledge(source: Path, clear: bool, dry_run: bool) -> None:
    console.rule("[bold]BIS Knowledge Base Seeder[/bold]")
    console.print(f"Source     : [cyan]{source}[/cyan]")
    console.print(f"Collection : [cyan]{settings.col_knowledge}[/cyan]")
    console.print(f"Embedding  : [cyan]{settings.gemini_embedding_model}[/cyan] ({settings.embedding_dimensions}-dim)")
    console.print(f"Dry run    : [cyan]{dry_run}[/cyan]\n")

    if not source.exists():
        console.print(f"[red]File not found: {source}[/red]")
        sys.exit(1)

    with open(source) as f:
        docs = json.load(f)

    console.print(f"Loaded [bold]{len(docs)}[/bold] documents")

    # Validate
    errors = []
    for i, doc in enumerate(docs):
        errors.extend(validate(doc, i))
    if errors:
        for e in errors:
            console.print(f"  [red]✗ {e}[/red]")
        if not dry_run:
            sys.exit(1)

    if dry_run:
        console.print(f"[green]✓ Dry run passed — {len(docs)} documents valid[/green]")
        return

    await MongoDB.connect()
    await MongoDB.ensure_indexes()

    col = MongoDB.col(settings.col_knowledge)

    if clear:
        result = await col.delete_many({})
        console.print(f"[yellow]Cleared {result.deleted_count} existing documents[/yellow]")

    inserted = updated = errors_count = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
    ) as progress:
        task = progress.add_task("Embedding + upserting...", total=len(docs))

        for batch_start in range(0, len(docs), BATCH_SIZE):
            batch = docs[batch_start : batch_start + BATCH_SIZE]
            texts = [embed_text(d) for d in batch]

            try:
                vectors = await _embed_batch_documents(texts)
            except Exception as exc:
                console.print(f"\n[red]Embedding batch failed: {exc}[/red]")
                errors_count += len(batch)
                progress.advance(task, len(batch))
                continue

            for doc, vec in zip(batch, vectors):
                try:
                    mongo_doc = {
                        "title": doc["title"],
                        "content": doc["content"][:2000],
                        "source": doc.get("source", ""),
                        "category": doc.get("category", "general"),
                        "is_number": doc.get("is_number"),
                        "tags": doc.get("tags", []),
                        "embedding": vec,
                        "_seed_key": doc["title"],  # dedup key
                    }
                    result = await col.update_one(
                        {"_seed_key": doc["title"]},
                        {"$set": mongo_doc},
                        upsert=True,
                    )
                    if result.upserted_id:
                        inserted += 1
                    else:
                        updated += 1
                except Exception as exc:
                    console.print(f"\n[red]Upsert failed for '{doc['title']}': {exc}[/red]")
                    errors_count += 1

            progress.advance(task, len(batch))

    await MongoDB.disconnect()

    table = Table(title="Seeding Complete", border_style="green")
    table.add_column("Metric", style="bold")
    table.add_column("Count", style="green")
    table.add_row("Inserted (new)", str(inserted))
    table.add_row("Updated (existing)", str(updated))
    table.add_row("Errors", str(errors_count), style="red" if errors_count else "green")
    table.add_row("Total", str(inserted + updated + errors_count))
    console.print(table)

    if errors_count == 0:
        console.print("\n[bold green]✓ Seeding complete![/bold green]")
        console.print("\n[yellow]⚠ Reminder: Create the Atlas Vector Search Index in UI:[/yellow]")
        console.print("  Collection : [cyan]bis_knowledge[/cyan]")
        console.print("  Index name : [cyan]bis_knowledge_vector_index[/cyan]")
        console.print("  Field      : [cyan]embedding[/cyan]")
        console.print(f"  Dimensions : [cyan]{settings.embedding_dimensions}[/cyan]  ← EMBEDDING_DIMENSIONS")
        console.print("  Similarity : [cyan]cosine[/cyan]")


async def seed_standards(source: Path, clear: bool) -> None:
    console.rule("[bold]IS Standards Map Seeder[/bold]")
    with open(source) as f:
        standards = json.load(f)

    await MongoDB.connect()
    col = MongoDB.col(settings.col_standards)

    if clear:
        result = await col.delete_many({})
        console.print(f"[yellow]Cleared {result.deleted_count} existing standards[/yellow]")

    inserted = updated = 0
    for std in standards:
        result = await col.update_one(
            {"is_number": std["is_number"]},
            {"$set": std},
            upsert=True,
        )
        if result.upserted_id:
            inserted += 1
        else:
            updated += 1

    await MongoDB.disconnect()
    console.print(f"[green]Standards seeded: {inserted} new, {updated} updated[/green]")


def main() -> None:
    parser = argparse.ArgumentParser(description="BIS Assistant Vector DB Seeder")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "--collection",
        default=settings.col_knowledge,
        choices=[settings.col_knowledge, settings.col_standards],
    )
    parser.add_argument("--clear", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.collection == settings.col_standards:
        asyncio.run(seed_standards(args.source, args.clear))
    else:
        asyncio.run(seed_knowledge(args.source, args.clear, args.dry_run))


if __name__ == "__main__":
    main()