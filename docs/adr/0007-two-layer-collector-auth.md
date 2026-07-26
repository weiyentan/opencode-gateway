# ADR 0007: Collector auth requires both API key middleware AND collector_credentials lookup

## Status

Accepted

## Context

The Gateway protects all non-`/health` routes with `ApiKeyMiddleware` (checks
`GATEWAY_API_KEY`). The `/ingest` endpoint additionally runs
`require_collector_token`, which looks up the token's SHA-256 hash in
`collector_credentials`. A collector's bearer token must pass **both** layers.

This raises two questions: (1) why not exempt `/ingest` from the middleware,
and (2) why not merge the two layers into one.

## Decision

Keep both layers. A collector's bearer token must pass **both**
`ApiKeyMiddleware` and `require_collector_token`.

## Rationale

- **Middleware is global** — Starlette `add_middleware` runs before routing.
  Exempting `/ingest` would require path-aware middleware logic that breaks the
  clean separation between transport-level auth (`GATEWAY_API_KEY`) and
  domain-level auth (which client, which credential, is it revoked).
- **Separate concerns** — the `collector_credentials` table supports multiple
  tokens per client, revocation, and `last_used_at` tracking. The API key
  middleware provides a simpler, env-var-based gate that doesn't require a
  database round-trip to reject unauthenticated requests at the edge.

## Consequences

- Bootstrapping a collector after a fresh deploy requires either (a) provisioning
  a token via `/admin/clients/{id}/tokens` AND setting `GATEWAY_API_KEY` to that
  same token, or (b) inserting the `GATEWAY_API_KEY`'s SHA-256 hash directly
  into `collector_credentials`.

## Alternatives Considered

**Remove API key middleware from `/ingest`**: Would require path-aware middleware
logic and break the separation between transport-level and domain-level auth.
Rejected.

**Single auth layer via `collector_credentials` only**: Would lose the fast-path
env-var gate that rejects unauthenticated requests without a database round-trip.
Rejected.
