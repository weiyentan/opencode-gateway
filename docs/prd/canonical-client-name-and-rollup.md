# PRD: Canonical Client Name and Client Project Rollup

## Problem Statement

The Aurora Glass **Client / Project Usage Breakdown** shows one row per client
registration. AWX automation provisions a distinct client per workspace,
producing names like `open-gateway-collectors-6457`, where the numeric suffix
is a workspace separator. That suffix is meaningful to AWX automation but
noise to the Gateway: every workspace appears as its own row, fragmenting what
operators experience as a single logical collector deployment across the
Client / Project view. A platform engineer looking at the breakdown sees a
wall of near-identical per-workspace rows instead of one consolidated row per
deployment, which makes deployment-level token consumption hard to read.

The groundwork is already in place — ADR 0011 (accepted 2026-08-09) decided
the approach, and the **Canonical Client Name** and **Client Project Rollup**
vocabulary is recorded in CONTEXT.md — but the implementation has gaps:

1. The client registry has no way to mark a client as belonging to a logical
   deployment (no canonical name field).
2. No client-project-day rollup table exists, so the client,project aggregate
   dimension still scans raw usage records — the exact dimension that carries
   the most fragmented per-workspace rows on the hottest view.
3. The Client / Project breakdown therefore has no way to group usage under a
   single deployment identity.

## Solution

Client registrations that belong to the same logical deployment share a
canonical client name; every reporting view groups usage under the canonical
name; raw client rows stay intact. Clients are **grouped**, never merged.

Concretely:

- The client registry gains a nullable **canonical name** field. When set,
  aggregate and reporting views group by it; when null, the client's own name
  is used, so every existing unassigned client keeps behaving exactly as
  before.
- A new additive **Client Project Rollup** table, keyed by
  `(client id, project label, day)`, holds only additive token and cost
  totals. It is maintained inline at ingest in the same transaction as the
  usage-record insert. Session counts and model counts are not stored in the
  rollup — they remain distinct-count queries over raw usage records.
- The read path is hybrid: the client,project aggregate dimension reads the
  rollup table; all other aggregate dimensions (model, session, day, week,
  month) keep scanning raw records. Canonical grouping (fall back to the
  client's own name when unset) is applied at read time on both paths, so
  changing a canonical name never requires table recompute.
- Rollout is: deploy (schema migration applies at startup, per the existing
  convention), then set canonical names per client via the admin API. No
  backfill.

## User Stories

1. As a platform engineer viewing the Client / Project breakdown, I want all
   per-workspace client registrations belonging to the same logical collector
   deployment to appear as one consolidated row, so that I can see at a glance
   what each deployment consumes instead of counting near-identical workspace
   rows.
2. As a platform engineer viewing the Client / Project breakdown, I want the
   consolidated row to be labeled with the canonical deployment identity
   (`open-gateway-collector`) rather than any one workspace's suffixed name,
   so that the view reflects the deployment I actually operate.
3. As a platform engineer setting up a new workspace client, I want to assign
   it a canonical name when I register or manage it, so that its usage appears
   under the same consolidated deployment row as its sibling workspace
   clients.
4. As a platform engineer setting up a new workspace client, I want a client
   with no canonical name to continue reporting under its own name, so that
   client setup is never blocked on choosing a canonical name.
5. As a platform engineer reading the client,project aggregate dimension, I
   want grouping to be applied on the canonical client name, so that
   per-project usage rolls up across all workspaces of a deployment.
6. As a platform engineer reading the client,project aggregate dimension, I
   want the per-project totals under a canonical row to be the sum of its
   workspace clients' project usage, so that the consolidated row reconciles
   with the underlying raw records.
7. As a platform engineer reading the model aggregate dimension, I want
   grouping behavior to be unchanged from today, so that canonical grouping
   affects only the client-scoped dimension and no other reporting is
   disturbed.
8. As a platform engineer reading the session aggregate dimension, I want
   grouping behavior to be unchanged from today, so that session-level views
   keep their existing semantics.
9. As a platform engineer reading the day, week, and month aggregate
   dimensions, I want grouping behavior to be unchanged from today, so that
   time-series views are unaffected by canonical names.
10. As a platform engineer reading raw usage records, I want each record's
    client to still identify the specific workspace registration, so that I
    can troubleshoot per-workspace issues without losing fidelity to the raw
    data.
11. As a platform engineer reading records-with-context, I want the client
    field to keep identifying the specific workspace registration, so that
    drilldown to raw detail is not collapsed by canonical grouping.
12. As an admin API consumer, I want to set a canonical name on a client via
    the admin API (PATCH), so that I can assign existing workspace clients to
    a deployment identity without touching raw data.
13. As an admin API consumer, I want to update an existing canonical name, so
    that I can correct a mis-assignment or rename a deployment.
14. As an admin API consumer, I want to clear a canonical name back to null,
    so that a client returns to reporting under its own name when the mapping
    no longer applies.
15. As an admin API consumer, I want clients with no canonical name to behave
    exactly as before, so that the change is backward compatible for existing
    consumers and clients.
16. As an operator renaming a canonical label, I want the change to take
    effect on the next read with no data recompute, so that renaming a
    deployment is a single cheap API call.
17. As an operator understanding the system later, I want the documentation
    (CONTEXT.md glossary and ADR 0011) to explain that clients are grouped,
    not merged, so that I do not mistake canonical grouping for a destructive
    rename of raw records.
18. As a platform engineer auditing usage, I want canonical grouping applied
    consistently on both the rollup-backed client,project path and the
    raw-scanning aggregate paths, so that totals agree no matter which
    endpoint I query.

## Implementation Decisions

1. **Nullable canonical name on the client registry.** The client registry
   gains a nullable canonical name field. When set, all aggregate and
   reporting views group by it; when null, the client's own name is used —
   backward compatible with every existing client. The value is set manually
   per client via the admin API (PATCH on the client resource). There is no
   bulk or sweep mechanism: nothing scans for workspace-shaped names and
   assigns canonical names automatically.

2. **New additive aggregate table.** A new client-project rollup table, keyed
   by (client id, project label, day), holds only additive columns: input,
   output, cache read, and cache write token totals plus estimated cost.
   Session counts and model counts are NOT stored in this table — they remain
   distinct-count queries over the raw usage records, because distinct counts
   do not aggregate additively across rows or days.

3. **Inline rollup maintenance at ingest.** Rollup maintenance is inline at
   ingest: the same transaction that inserts a usage record upserts the
   rollup row with additive increments. Raw usage records remain
   authoritative; if the rollup disagrees with the raw records, the rollup is
   the value to correct — the same discipline as the Session Cache-Write
   Total entry in the glossary.

4. **Hybrid read path.** The read path is hybrid: the client,project
   aggregate dimension reads from the rollup table; all other aggregate
   dimensions (model, session, day, week, month) keep scanning raw records.
   Canonical grouping — falling back to the client's own name when unset — is
   applied at read time on both paths, so changing a canonical name never
   requires table recompute.

5. **Everything else unchanged.** The ingest record-insert path, the
   frontend, and all other endpoints are unchanged by this feature.

6. **Rollout.** Deploy (schema migration applies at startup, per the existing
   convention), then set canonical names via the admin API. No backfill.

## Testing Decisions

- **Decision (user-mandated): no new tests for this feature.** This PRD
  records explicitly that the implementation must not add tests for canonical
  name or rollup behavior. The existing test suite must remain green —
  including the existing SQL-shape aggregate tests and the client CRUD tests.
  Do not read coverage expectations into this PRD that were rejected during
  the grilling session.

- **What would have made a good test (recorded for future reference;
  external behavior only):**
  - Rollup totals equal the sum of the raw usage records they were derived
    from.
  - Two clients sharing a canonical name roll up to one row in the
    client,project view.
  - A client with an unset canonical name falls back to reporting under its
    own name.
  - A PATCH set/clear round-trip on the canonical name field.
  - Prior art: the existing aggregates SQL-shape tests and the client CRUD
    tests in the flat tests/ suite would have been the natural homes for
    these.

## Out of Scope

- Backfill of the rollup table from historical raw records — the rollup
  starts empty and is filled by new ingests only; explicitly deferred to a
  separate decision.
- Any replay CLI or backfill script.
- New tests (per the user decision above).
- Frontend changes.
- Repository hygiene items — ghost app module shells, the ADR 0010 numbering
  collision, and root-level review docs — tracked as a separate effort.

## Further Notes

- Implements ADR 0011 (accepted 2026-08-09) and the **Canonical Client
  Name** and **Client Project Rollup** glossary entries in CONTEXT.md.
  Implementation should not reopen those decisions.
- Known accepted gap: until a future backfill, the Client / Project breakdown
  shows partial history — the rollup carries no pre-existing
  client-project-day data.
- The canonical name is a manual per-client mapping: the plural-to-singular
  relationship (`open-gateway-collectors-*` → `open-gateway-collector`) is
  not derivable by regex, which is why no automatic assignment exists.
- Today's date: 2026-08-09.
