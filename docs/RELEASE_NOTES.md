# BIS Assistant release notes

## P0 · Security and verification

- Every session operation checks account ownership or a per-chat guest credential. Guest messages share the guest session expiry. Account chats persist until deleted.
- Browser login uses an HttpOnly cookie and a required header for browser mutations. Production requires a generated JWT signing secret. Configure proxy trust before relying on per-client rate limits.
- Registry adapters require a matching identifier and recognizable fields. Invalid, unavailable, and unrecognized responses have distinct outcomes. A registry result cannot authenticate a physical item.
- The official fetcher accepts bounded HTTPS requests, checks redirects, pins public destinations, and extracts documents without starting a browser. Application logs omit prompts and registry identifiers.
- The backend uses a Python 3.12 uv lock and patched dependencies. The frontend uses Next.js 16, React 19, and Webpack for production builds.

Existing browser sessions need a fresh sign-in after the auth migration. Review the dry-run retention migration before applying it to existing data. The initial P0 validation used disposable MongoDB and fixture portal responses.

## P1 · Release and operations foundation

- The guidance route builds a server-side evidence map from official documents. The API renders inline citation links and abstains when the draft lacks known source references. A source reference is a format check. Reviewers still need to verify that each source supports its claim.
- Session streams send keep-alives, stop the producer on disconnect, and finish after the answer is saved. Clients can supply an idempotency key to replay a completed turn or detect a duplicate submission.
- Separate liveness and readiness probes support ingress and deployment checks. Request, stream, guidance, and registry events provide structured logs for latency, outcomes, citation coverage, and provider-reported token usage.
- CI runs offline regressions, a frontend build, dependency checks, and secret scanning. Both services have container build definitions.
- Corpus builds can target isolated versioned collections. A manifest records the snapshot hash, embedding configuration, and document count. A candidate must pass an Atlas search check before a staging or production app can load it.

See the [operations runbook](OPERATIONS.md) and [BIS evaluation cases](EVALUATION.md) before promoting a release.

### Corpus review finding

The checked-in baseline contains 158 chunks, including 149 that point to raw local files. All eight PDFs have identical SHA-256 hashes despite their different scheme filenames. The release preparation command omits raw files and uses curated and crawled entries with official HTTPS URLs. Reacquire and verify the original scheme documents before including them in a release.
