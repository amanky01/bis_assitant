# BIS Assistant

BIS Assistant answers questions about Indian Standards, certification, and hallmarking. It uses a Next.js chat UI, FastAPI, LangGraph, Gemini or Groq for chat, and Gemini embeddings with MongoDB Atlas Vector Search. Recognized CM/L, CRS R-number, and HUID lookups go directly to the official registries and stream a deterministic result.

## Local development

Install Python 3.12, uv, and Node.js 20.9 or newer.

```sh
cd backend
cp .env.example .env
uv sync --locked
uv run --locked uvicorn app.main:app --reload --port 8000
```

Set `MONGO_URI` and `GEMINI_API_KEY` in `backend/.env`. Set `LLM_PROVIDER=groq` and `GROQ_API_KEY` to use Groq for chat. MongoDB still uses the Gemini embedding model and its configured dimensions. Set `TAVILY_API_KEY` to enable Tavily search.

```sh
cd frontend
cp .env.local.example .env.local
npm ci
npm run dev
```

The frontend forwards `/api/v1/*` to `BIS_BACKEND_URL` so the browser uses same-origin session cookies. Open `http://localhost:3000`. Backend API docs are available at `http://localhost:8000/docs` in development.

## Knowledge base

Create the Atlas vector search index on the `bis_knowledge` collection. Use the configured index name `bis_knowledge_vector_index`, cosine similarity, and the dimensions in `EMBEDDING_DIMENSIONS` (default 3072).

```sh
cd backend
uv run --locked python scripts/seed_vector_db.py --source data/bis_knowledge.json
uv run --locked python scripts/seed_vector_db.py --source data/is_standards_map.json --collection is_standards
```

Official web reading fetches bounded HTTPS documents. Pages that require a browser or a portal challenge can be unavailable. Compare registry status, manufacturer, and product scope with the item. The registry lookup cannot establish physical authenticity.

## Tests

```sh
cd backend
uv run --locked pytest -q
```

The regression suite uses fixtures for MongoDB, official portal responses, and network behavior.

```sh
cd frontend
npm run typecheck
npm run build
```

## Deployment and existing data

- Run the backend with `uv sync --locked --no-dev` and `uv run --locked --no-dev uvicorn app.main:app --host 0.0.0.0`. Regenerate the compatibility requirements export with `uv export --locked --no-dev --no-emit-project --output-file requirements.txt` when dependencies change.
- Set `APP_ENV=production` and inject a unique `JWT_SECRET_KEY` from the deployment secret store. For example, generate it with `openssl rand -hex 32`. Use HTTPS on the public frontend. Set `BIS_BACKEND_URL` at frontend build time. The browser sends `X-BIS-Client: web` on mutations, and the API uses an HttpOnly, SameSite=Strict, Secure cookie in staging and production.
- Keep the backend behind the trusted frontend/ingress where possible. Configure Uvicorn's trusted forwarded IPs for that ingress if using per-client guest and login rate limits. Without trusted forwarding, requests from a shared proxy share its quota. Set `CORS_ORIGINS` to a JSON array only for intentionally supported cross-origin clients. Tune `AUTH_REQUESTS_PER_MINUTE`, `CHAT_REQUESTS_PER_MINUTE`, and `AGENT_TIMEOUT_SECONDS` for your deployment.
- Account chats persist until explicitly deleted. New guest chats and their messages share an expiry date, and guest credentials live only in the current browser tab. Guest chats created before this change lack the new credential. Users with legacy browser tokens need to sign in again.
- Review legacy anonymous message counts with `uv run --locked python scripts/migrate_message_retention.py`. After reviewing the dry run and taking the old backend out of service, rerun with `--apply` to backfill guest message expiry and mark orphaned guest messages for MongoDB TTL deletion. Account sessions are excluded. This migration has not been run against a live database.

## Release documentation

- [P0 and P1 release notes](docs/RELEASE_NOTES.md)
- [Deployment, telemetry, corpus promotion, and incident runbook](docs/OPERATIONS.md)
- [BIS evaluation cases and rollout criteria](docs/EVALUATION.md)
