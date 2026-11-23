# Crawling BIS Sites for RAG

The crawler discovers and fetches content from BIS and Indian standards websites, chunks it, and merges it into the RAG knowledge base.


For staging and production, use the [versioned corpus release workflow](../../docs/OPERATIONS.md#knowledge-releases-and-rollback). The commands below are for local development.

## Daily commands (run these daily for incremental crawl)

From the **backend** directory:

```bash
# 1. Crawl (adjust --max-pages / --max-chunks if needed; e.g. 50 pages per day)
uv run --locked python scripts/crawl_bis_sites.py --max-pages 50 --max-chunks 1500

# 2. Merge crawled output with curated + raw into bis_knowledge.json
uv run --locked python scripts/prepare_data.py --include-crawled

# 3. Seed DB without --clear so existing chunks are updated and new ones added (no duplicates)
uv run --locked python scripts/seed_vector_db.py
```

Use **`uv run --locked python scripts/seed_vector_db.py`** (no `--clear`) so the DB accumulates and same-URL chunks are updated instead of duplicated.

## Pipeline

1. **Crawl** — `uv run --locked python scripts/crawl_bis_sites.py`
   Crawls allowed domains (bis.gov.in, manakonline.in, crsbis.in, etc.), extracts HTML and PDFs, chunks and enriches with category/tags. Writes **data/crawled_knowledge.json**.

2. **Merge and prepare** — `uv run --locked python scripts/prepare_data.py --include-crawled`
   Merges curated + raw (PDF/HTML from data/raw/) + crawled data into **data/bis_knowledge.json**.

3. **Seed vector DB** — `uv run --locked python scripts/seed_vector_db.py`
   Embeds and upserts into MongoDB `bis_knowledge` collection.

## Crawler options

- `--max-pages 100` — Stop after 100 pages (default 200).
- `--max-chunks 3000` — Stop after 3000 chunks (default 5000).
- `--delay 2` — Seconds between requests (default 2).
- `--output data/crawled_knowledge.json` — Output path.
- `--seeds data/crawl_seeds.json` — Seed URL list (optional).

## Seed URLs

Edit **data/crawl_seeds.json** to add or change start URLs. If the file is missing, the script uses built-in seeds for BIS, hallmarking, CRS, manakonline, and crsbis.

## Daily crawl without duplication

The architecture **supports** crawling some pages daily and adding them to the DB without creating duplicate documents.

- **Deduplication:** Each chunk has a **unique title** derived from the page title and a **URL slug** (hash of the source URL). The seeder uses `_seed_key: doc["title"]` and **upserts** (update if exists, insert if new). So the same URL always yields the same title → the same Mongo document is **updated**, not duplicated.
- **Daily workflow:** Run the crawler (full or a subset of pages). Run `prepare_data.py --include-crawled`, then run **`seed_vector_db.py` without `--clear`**. That way:
  - Existing chunks (same URL as before) are **updated** in place.
  - New chunks (new URLs or new pages) are **inserted**.
  - Nothing is removed unless you run with `--clear` (which wipes the collection and re-inserts only what is in the current bis_knowledge.json).
- **Accumulating over days:** If you crawl only a **subset** of pages each day (e.g. new or changed), keep a **cumulative** crawled file: either append that day’s crawl output to **data/crawled_knowledge.json** before running prepare_data, or merge that day’s output into a separate cumulative file and point prepare_data at it. Then run prepare_data and seed **without --clear** so previous days’ chunks remain in the DB and today’s chunks are added/updated.

**Summary:** Use **no `--clear`** when seeding after a daily crawl so the DB accumulates and same-URL chunks are updated instead of duplicated.

## Requirements

- Respects robots.txt and rate limits.
- Only crawls domains in `allowed_domains` (see app core config).
- Chunk titles include a URL-derived slug so re-seeding updates the same Mongo docs.
