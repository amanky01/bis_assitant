# BIS Assistant — Tools Documentation

This document describes each tool available in the BIS Assistant app and the **steps** taken when each tool is invoked. The LangGraph agent answers questions about BIS certification and standards. The application routes recognized registry identifiers to deterministic lookups before it calls the model.

---

## Overview

| Tool | Module | Purpose |
|------|--------|--------|
| `search_bis_knowledge` | `vector_search` | Static RAG over BIS knowledge base (MongoDB Atlas) |
| `web_search_bis` | `web_rag` | Web search on allowed BIS domains (Tavily or DuckDuckGo) |
| `scrape_page` | `web_rag` | Fetch and extract full text from a BIS-related URL (HTML or PDF) |
| `verify_cml` | `verification` | Verify ISI mark CM/L number at manakonline.in |
| `verify_r_number` | `verification` | Verify CRS R-number at crsbis.in |
| `verify_huid` | `verification` | Verify gold hallmark HUID at huid.manakonline.in |
| `check_category_match` | `verification` | Check if IS standard matches product type |
| `detect_fake_mark` | `verification` | Registry lookup with product-scope guidance |
| `get_compliance_guide` | `compliance` | Return certification process for a product type |

---

## 1. Vector Search — `search_bis_knowledge`

**File:** `backend/app/tools/vector_search.py`

**When to use:** Search the BIS knowledge base for Indian Standards, ISI marks, CRS, hallmarking, certification, BEE ratings, etc. **Call this first** before web search.

### Steps

1. **Embed the query**
   - Get the embedding service (Gemini).
   - Generate an embedding vector for the user query.
   - On failure: return an error string; do not proceed.

2. **Run Atlas `$vectorSearch`**
   - Build an aggregation pipeline:
     - `$vectorSearch` on index `bis_knowledge_vector_index`, path `embedding`, with `queryVector`, `numCandidates = top_k * 20`, `limit = top_k`.
     - `$project` to return `title`, `content`, `source`, `category`, `is_number`, and `score` (from `$meta: "vectorSearchScore"`).
   - Execute the pipeline on the knowledge collection.
   - **Note:** Do not add `$match` on `vectorSearchScore` in the pipeline (Atlas returns 0 results). Filter by score in Python instead.

3. **Filter by score in Python**
   - Keep only documents with `score >= MIN_SCORE` (0.30).
   - If none remain: return a “no results” message and suggest `web_search_bis`.

4. **Format for the LLM**
   - For each result: format as `[Source N]`, URL, relevance score, and content.
   - Join all chunks with `---` and return a single string.

---

## 2. Web RAG — `web_search_bis`

**File:** `backend/app/tools/web_rag.py`

**When to use:** Fallback when `search_bis_knowledge` returns no (or insufficient) results. Searches only allowed BIS domains.

### Steps

1. **Get Tavily client**
   - Lazy-initialize Tavily client using `TAVILY_API_KEY` from settings.
   - If key is missing: raise `WebSearchError`.

2. **Run Tavily search**
   - Call Tavily search in a thread (to avoid blocking):
     - `query`: user query
     - `search_depth`: `"basic"`
     - `max_results`: 5
     - `include_domains`: `settings.allowed_domains` (e.g. bis.gov.in, manakonline.in, crsbis.in, huid.manakonline.in, bis.org.in, beeindia.gov.in).

3. **Handle empty results**
   - If no results: return a fixed “[no_web_results]” message.

4. **Format results**
   - For each result: format as Title, URL, and Snippet.
   - Join with `---` and return.

---

## 3. Web RAG — `scrape_page`

The fetcher accepts allowlisted HTTPS destinations on the standard port. It validates each redirect, resolves public addresses, pins the connection to a validated address, and checks TLS against the official hostname. It caps total time, concurrency, response bytes, PDF pages, and extracted text. HTML extraction reads the response without executing scripts or fetching page resources. The model receives extracted content as untrusted reference text.

## 4. Registry lookups

Recognized CM/L, CRS R-number, and HUID identifiers are looked up directly through the official portals. The application requires an identifier match and recognizable record fields before it reports a registry status. CRS uses exact status matching. HUID records require a matching code, purity, and supporting record details.

The rendered result includes the official registry endpoint, check time, matched record information, and scope of the result. The application distinguishes active records, inactive records, matching hallmark records, explicit no-match responses, portal outages, and unrecognized responses. Browser challenges and unfamiliar portal layouts yield an inconclusive result. The lookup cannot establish physical authenticity.

## 5. Category reference — `check_category_match`

The category tool looks up the standard and its reference categories in MongoDB. An exact description match is a reference signal. For other descriptions, compare the product and model with the official licence scope.

## 6. Compliance — `get_compliance_guide`

**File:** `backend/app/tools/compliance.py`

**When to use:** User asks “how do I get BIS certified” or “what is the certification process” for a product type.

### Steps

1. **Normalize input**
   - `product_lower = product_type.lower()`.

2. **Match category**
   - Iterate over `_KEYWORDS` (electrical, electronics, gold, lpg).
   - If any keyword for a category appears in `product_lower`, select that category.

3. **Return guide**
   - Return the corresponding entry from `_GUIDES` (electrical, electronics, gold, lpg, or default).
   - Guides are static text describing: applicable standards, steps (e.g. register, pay fee, testing, audit, license), timeline, fees, validity.

4. **Default**
   - If no keyword matches: return `_GUIDES["default"]`, which summarizes ISI, CRS, and FMCS and invites the user to specify product type for exact steps.

---

## Tool usage order (recommended)

- For **general BIS/standards questions:** `search_bis_knowledge` → if insufficient, `web_search_bis` → optionally `scrape_page` for full content.
- For **CM/L verification:** the application runs the official registry lookup.
- For **R-number verification:** `verify_r_number`.
- For **gold HUID:** `verify_huid`.
- For **“how to get certified”:** `get_compliance_guide`.

The application renders registry results directly. The model summarizes search and guidance tools.
