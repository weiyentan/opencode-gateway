# Collector auth requires both API key middleware AND collector_credentials lookup

The Gateway protects all non-`/health` routes with `ApiKeyMiddleware` (checks
`GATEWAY_API_KEY`). The `/ingest` endpoint additionally runs
`require_collector_token`, which looks up the token's SHA-256 hash in
`collector_credentials`. A collector's bearer token must pass **both** layers.

**Why not remove the API key middleware from `/ingest`?** The middleware is
applied globally via `app.add_middleware(ApiKeyMiddleware)` — Starlette
middleware runs on every request before routing. Exempting `/ingest` would
require path-aware middleware logic that breaks the clean separation: middleware
handles transport-level auth (`GATEWAY_API_KEY`), while the dependency handles
domain-level auth (which client, which credential, is it revoked).

**Why not use a single layer?** The `collector_credentials` table supports
multiple tokens per client, revocation, and `last_used_at` tracking. The API key
middleware provides a simpler, env-var-based gate that doesn't require a
database round-trip to reject unauthenticated requests at the edge.

**Consequence:** Bootstrapping a collector after a fresh deploy requires either
(a) provisioning a token via `/admin/clients/{id}/tokens` AND setting
`GATEWAY_API_KEY` to that same token, or (b) inserting the `GATEWAY_API_KEY`'s
SHA-256 hash directly into `collector_credentials`.
