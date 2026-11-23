# BIS Assistant operations

## Scope and release criteria

The assistant covers informational questions about ISI, CRS, hallmarking, and certification. It answers from retrieved official references and displays the scope of registry evidence. Success means a user can identify the relevant official material or understand why the system cannot verify an answer. Current requirements need a dated official notice or order. Ambiguous categories need the product model, intended use, and relevant standard. Physical authentication requires separate evidence.

Release to staging after CI passes. Exercise browser login, guest and account history isolation, streamed cancellation, registry failure, and citation rendering. Run the [evaluation cases](EVALUATION.md) against the selected corpus and model. Test load through the staging proxy and rehearse corpus rollback and a database restore before public rollout.

## Deployment

Build and run the services with deployment-managed secrets. The backend image installs the locked Python dependencies and starts as an unprivileged user. The frontend build needs a backend URL reachable by the Next.js server.

```sh
docker build -t bis-backend:release backend
docker build --build-arg BIS_BACKEND_URL=http://bis-backend:8000 -t bis-frontend:release frontend
```

Use separate Atlas databases and keys for staging and production. Configure HTTPS ingress, a trusted proxy list, and an idle timeout above the SSE heartbeat interval. Keep response buffering off for chat streams. Store JWT, Atlas, model, and optional Tavily secrets in the deployment secret manager. Limit the web process to account/session/message operations, corpus reads, and required startup indexes. Use an operator identity for corpus and search-index writes.

`GET /api/v1/health/live` checks that the process responds. `GET /api/v1/health/ready` and the legacy `GET /api/v1/health` check MongoDB connectivity and return 503 on failure. Model and official-site checks belong in staging and synthetic monitoring so an upstream outage does not take all instances out of service.

## Chat contract

`POST /api/v1/chat/sessions/{session_id}/message` accepts `session_id` and `message`. Account requests use the session cookie. Guest requests send the credential as `X-Guest-Token`. Browser mutations include `X-BIS-Client: web`.

An optional UUID `Idempotency-Key` is scoped to a chat. Reusing it with the same payload replays a saved answer or returns 409 while the original is pending or incomplete. Reusing it with different text returns 409. Send a new key to retry a failed turn. During a stream, ignore SSE comments and handle `tool_status`, `token`, `metadata`, `error`, and `done`. A lost connection before `done` should surface as an interruption. `done` means the response was persisted. Guidance can pause before its first token while the server checks citations.

## Telemetry and incident response

Collect JSON event bodies from application logs. Join events on the server-generated `X-Request-ID`. Use route templates and outcome labels for aggregation. Keep prompts, portal identifiers, cookies, and raw source text out of telemetry. Model and search providers may receive questions or retrieval queries. Configure and review their retention settings separately.

| Signal | Response |
| --- | --- |
| Readiness failures or elevated HTTP 5xx | Check Atlas connectivity and network rules. Roll back the app image if the spike follows a deployment. |
| Chat errors, disconnects, or high p95 latency | Compare first-token and completion latency with ingress timeouts and model behavior. Track the terminal event even when the HTTP status is 200. |
| Registry unavailable or unrecognized outcomes | Check portal availability and parser fixtures. Preserve inconclusive results until the response format can be confirmed. |
| Falling evidence coverage or stale corpus | Compare release manifests and reviewed cases. Revert the corpus selection if the previous version has better coverage. |
| Higher provider-reported token usage | Compare model, tool call counts, and generation time. Use the provider’s billing data for actual cost. |

Set alert thresholds after measuring staging traffic. Request logs are the collection interface. A hosted log sink, dashboards, and alert delivery require environment configuration.

## Knowledge releases and rollback

1. Crawl and write a candidate with `uv run --locked python scripts/prepare_data.py --include-crawled --release-output /path/to/new-snapshot.json`. Review source provenance, effective dates, and superseded documents. Archive the input JSON alongside its SHA-256. A crawl timestamp records when a source was collected, while effective dates come from the source.
2. Run `uv run --locked python scripts/seed_vector_db.py --source /path/to/snapshot.json --release <id> --dry-run`. Resolve validation failures and repeat without `--dry-run` to build a new collection. Use a new ID after a failed build. Keep documents that cannot be matched to official HTTPS sources out of the release.
3. Create the configured vector index on the candidate collection in Atlas, then run `uv run --locked python scripts/seed_vector_db.py --check-release <id>`. The check verifies the count, configuration, Atlas index readiness, and a sample vector query. [MongoDB index documentation](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/indexes/)
4. Set `COL_KNOWLEDGE=bis_knowledge_<id>` in staging. Review the evaluation cases. Promote the same artifact and corpus configuration to production after approval.
5. Roll back by restoring the previous image tag and/or `COL_KNOWLEDGE` value. Retain prior collections and manifests for this purpose. For persistence incidents, restore an Atlas backup to an isolated target, validate counts, session isolation and TTL indexes, then carry out the environment’s approved cutover.
