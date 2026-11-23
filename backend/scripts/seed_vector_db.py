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
import hashlib
import re
from datetime import datetime, timezone
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
from google import genai
from app.services.gemini import embed_content

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

async def _embed_batch_documents(texts: list[str]) -> list[list[float]]:
    """Embed a batch of document chunks concurrently."""
    async with genai.Client(api_key=settings.gemini_api_key).aio as client:
        return list(await asyncio.gather(*(
            embed_content(client, text, "RETRIEVAL_DOCUMENT") for text in texts
        )))


def validate(doc: dict, i: int) -> list[str]:
    errors = []
    if not isinstance(doc.get("title"), str) or not doc["title"].strip():
        errors.append(f"[{i}] Missing 'title'")
    if not isinstance(doc.get("content"), str) or len(doc["content"].strip()) < 30:
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


async def seed_knowledge(
    source: Path, clear: bool, dry_run: bool, release: str | None = None
) -> None:
    if not release and not dry_run and (
        settings.app_env != "development" or settings.col_knowledge.startswith("bis_knowledge_")
    ):
        raise SystemExit("Seed an isolated --release instead of modifying a published corpus")
    target = f"bis_knowledge_{release}" if release else settings.col_knowledge
    console.rule("[bold]BIS Knowledge Base Seeder[/bold]")
    console.print(f"Source     : [cyan]{source}[/cyan]")
    console.print(f"Collection : [cyan]{target}[/cyan]")
    console.print(f"Embedding  : [cyan]{settings.gemini_embedding_model}[/cyan] ({settings.embedding_dimensions}-dim)")
    console.print(f"Dry run    : [cyan]{dry_run}[/cyan]\n")

    if not source.exists():
        console.print(f"[red]File not found: {source}[/red]")
        sys.exit(1)

    snapshot = source.read_bytes()
    docs = json.loads(snapshot)
    if not isinstance(docs, list) or not docs or any(not isinstance(doc, dict) for doc in docs):
        raise SystemExit("Expected a non-empty list of knowledge records")
    manifest = {
        "collection": target,
        "sha256": hashlib.sha256(snapshot).hexdigest(),
        "embedding_model": settings.gemini_embedding_model,
        "dimensions": settings.embedding_dimensions,
        "chunks": len(docs),
        "ingested_at": datetime.now(timezone.utc),
    }

    console.print(f"Loaded [bold]{len(docs)}[/bold] documents")

    # Validate
    errors = []
    for i, doc in enumerate(docs):
        errors.extend(validate(doc, i))
        if release and not settings.is_domain_allowed(doc.get("source") or ""):
            errors.append(f"[{i}] Expected an official HTTPS source")
    if errors:
        for e in errors:
            console.print(f"  [red]✗ {e}[/red]")
        sys.exit(1)

    if dry_run:
        console.print(f"[green]✓ Dry run passed — {len(docs)} documents valid[/green]")
        return

    await MongoDB.connect()
    await MongoDB.ensure_indexes()

    col = MongoDB.col(target)
    if release:
        await MongoDB.col("corpus_releases").insert_one({"_id": release, **manifest, "status": "building"})

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

            for offset, (doc, vec) in enumerate(zip(batch, vectors)):
                try:
                    mongo_doc = {
                        "title": doc["title"],
                        "content": doc["content"][:2000],
                        "source": doc.get("source", ""),
                        "category": doc.get("category", "general"),
                        "is_number": doc.get("is_number"),
                        "tags": doc.get("tags", []),
                        "embedding": vec,
                        "_seed_key": f"{release}/{batch_start + offset}" if release else doc["title"],
                        "content_sha256": hashlib.sha256(doc["content"].encode()).hexdigest(),
                        "section": doc.get("section"),
                        "effective_date": doc.get("effective_date"),
                        "retrieved_at": doc.get("retrieved_at"),
                        "superseded": doc.get("superseded", False),
                    }
                    result = await col.update_one(
                        {"_seed_key": mongo_doc["_seed_key"]},
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

    if release:
        await MongoDB.col("corpus_releases").update_one(
            {"_id": release}, {"$set": {"status": "failed" if errors_count else "seeded"}}
        )
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
        console.print(f"  Collection : [cyan]{target}[/cyan]")
        console.print("  Index name : [cyan]bis_knowledge_vector_index[/cyan]")
        console.print("  Field      : [cyan]embedding[/cyan]")
        console.print(f"  Dimensions : [cyan]{settings.embedding_dimensions}[/cyan]  ← EMBEDDING_DIMENSIONS")
        console.print("  Similarity : [cyan]cosine[/cyan]")
    if errors_count:
        raise SystemExit(1)


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


async def check_release(release: str) -> None:
    await MongoDB.connect()
    try:
        manifest = await MongoDB.col("corpus_releases").find_one({"_id": release, "status": "seeded"})
        if not manifest:
            raise SystemExit("Release did not finish seeding")
        if (manifest["embedding_model"], manifest["dimensions"]) != (
            settings.gemini_embedding_model, settings.embedding_dimensions
        ):
            raise SystemExit("Runtime embedding configuration differs from the candidate")
        collection = MongoDB.col(manifest["collection"])
        if await collection.count_documents({}) != manifest["chunks"]:
            raise SystemExit("Candidate document count differs from the manifest")
        name = "bis_knowledge_vector_index"
        indexes = await (await collection.list_search_indexes(name)).to_list()
        if not any(index.get("queryable") and index.get("status") == "READY" for index in indexes):
            raise SystemExit("The candidate vector index is not ready")
        document = await collection.find_one({}, {"embedding": 1})
        cursor = await collection.aggregate([{"$vectorSearch": {
            "index": name, "path": "embedding", "queryVector": document["embedding"],
            "numCandidates": 20, "limit": 1,
        }}, {"$project": {"_id": 1}}])
        if not await cursor.to_list(1):
            raise SystemExit("Candidate vector smoke query returned no result")
        await MongoDB.col("corpus_releases").update_one(
            {"_id": release}, {"$set": {"checked_at": datetime.now(timezone.utc)}}
        )
        console.print(f"Candidate passed. Promote via COL_KNOWLEDGE={manifest['collection']}")
    finally:
        await MongoDB.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="BIS Assistant Vector DB Seeder")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "--collection",
        default=settings.col_knowledge,
        choices=[settings.col_knowledge, settings.col_standards],
    )
    parser.add_argument("--release", help="Seed an isolated, immutable knowledge release")
    parser.add_argument("--check-release", help="Verify an existing release and its Atlas index")
    parser.add_argument("--clear", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    release = args.release or args.check_release
    if release and not re.fullmatch(r"[a-z0-9_]{1,32}", release):
        parser.error("Release IDs use lowercase letters, digits, and underscores")
    if release and (args.clear or args.collection == settings.col_standards):
        parser.error("Knowledge releases require a fresh collection")
    if args.release and args.check_release:
        parser.error("Seed and check must run separately")
    if args.check_release:
        asyncio.run(check_release(args.check_release))
    elif args.collection == settings.col_standards:
        asyncio.run(seed_standards(args.source, args.clear))
    else:
        asyncio.run(seed_knowledge(args.source, args.clear, args.dry_run, args.release))


if __name__ == "__main__":
    main()