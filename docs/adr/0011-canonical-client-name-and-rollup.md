# ADR 0011: Canonical client name and client-project rollup

## Status

Accepted (2026-08-09)

## Context

The Gateway's Client / Project usage breakdown surfaces one row per client
registration. AWX automation provisions a distinct client per workspace,
producing names like `open-gateway-collectors-6457` where the numeric
suffix is a workspace separator. That suffix is meaningful to AWX
automation but noise to the Gateway: every workspace appears as its own
row, fragmenting what operators experience as a single deployment identity
across the Client / Project view.

## Decision

Adopt a canonical client name plus an additive client-project rollup table,
with grouping applied at read time. Clients are not merged.

1. **Canonical client name.** `opencode_clients` gains a nullable
   `canonical_name` field. When set, all aggregate views group by it; when
   null, the client's own name is used. The value is set manually per
   client via the admin API (`PATCH /admin/clients/{id}`). The per-workspace
   clients share the canonical name `open-gateway-collector`. The
   plural-to-singular relationship between the per-workspace names and the
   canonical name is a manual mapping, not a regex strip.
2. **Additive rollup table.** A new `client_project_rollup` aggregate table,
   keyed by `(client_id, project_label, day)`, stores only additive columns:
   input/output/cache/reasoning tokens and estimated cost. Session counts
   and model counts are NOT stored in the rollup — they remain
   distinct-count queries over raw usage records.
3. **Rollup maintenance.** The rollup is maintained inline at ingest, in
   the same transaction as the usage-record insert, as additive UPSERT
   increments. Raw usage records remain authoritative; if the rollup
   disagrees with raw records, the rollup is the value to correct — the
   same correction discipline used for the session-level
   `total_cache_write_tokens` aggregate (see the Session Cache-Write Total
   glossary entry and `scripts/backfill_cache_write_tokens.py`).
4. **Hybrid read path.** The client-project aggregation reads the rollup
   table; all other aggregate dimensions (model, session, day/week/month)
   keep scanning raw records. Canonical grouping
   (`COALESCE(canonical_name, name)`) is applied at read time on both
   paths, so a canonical-name change never requires table recompute. This
   includes the reasoning-token total (`total_reasoning_tokens`) that the
   client-project aggregation currently surfaces, so the rollup-backed
   read path exposes the same metric set as the raw-record path.
5. **Backfill out of scope.** The rollup starts empty and is filled by new
   ingests only; a separate decision will cover backfill.

## Alternatives Considered

1. **Data merge / rename** — rewrite raw usage records to point at a
   single consolidated client. Rejected: destructive and loses the
   per-workspace identity AWX automation relies on; requires rewriting
   history.
2. **Query-time grouping without a table** — compute client-project-day
   aggregates by scanning raw records on every read. Rejected: the
   fragmented per-workspace dimension is exactly the case that would scan
   the most records on the hottest view; the additive rollup exists to
   avoid that.
3. **Frontend-only grouping** — collapse rows in Aurora Glass by
   suffix-stripping client names. Rejected: the fragmentation is a data
   shape problem, not a display problem; every API consumer would still
   see fragmented rows, and suffix-stripping at the frontend re-introduces
   the fragile regex the manual canonical mapping avoids.
4. **Full rollup with distinct counts** — also store session and model
   counts in the rollup. Rejected: distinct counts do not aggregate
   additively across days; storing them would require recompute or
   read-time distinct queries against the rollup, defeating its purpose.

## Consequences

- **Empty history until backfill:** the rollup carries no pre-existing
  client-project-day data; those aggregates are unavailable retroactively
  until a future backfill decision.
- **Raw records remain authoritative:** the rollup is a derived
  convenience; disagreements are resolved by correcting the rollup, not
  the raw records.
- **Canonical renames are cheap:** because grouping is applied at read
  time, changing a client's canonical name takes effect immediately with
  no table recompute.
