# ADR 0001: Separate Observation Tables Per Domain Entity

## Status

Implemented (Accepted)

## Context

The Gateway needs to store time-series observations about runner health, workspace disk usage, and OpenCode serve instance status. These observations come from a scheduled collector that runs against the Runner VM.

Three options were considered:
1. **Separate tables** — `runner_observations`, `workspace_observations`, `opencode_instance_observations`
2. **Single generic table** — one `observations` table with a `category` column and JSONB `payload`
3. **Hybrid** — separate tables but with shared base columns

## Decision

Use separate observation tables per domain entity (option 1).

## Rationale

- Each observation type has different query patterns and different columns (disk metrics vs. service health vs. workspace size)
- Separate tables keep referential integrity clear (each table references its parent entity by FK)
- Queries are simpler and more self-documenting — no type-discrimination in WHERE clauses
- The schema explicitly models the domain, making it easier for new developers to understand
- PostgreSQL handles many tables efficiently; there is no performance concern at MVP scale

## Consequences

Positive:
- Clear, self-documenting schema
- Type-safe columns per observation type
- Simple queries without JSONB extraction or type filtering
- Easy to add observation-type-specific indexes

Negative:
- More tables to maintain
- Adding a new observation type requires a new table + migration
- Slightly more boilerplate in the data access layer

## Alternatives Considered

**Single generic table**: One `observations` table with `category TEXT` and `payload JSONB`. Flexible but shifts type safety to application code, makes queries harder to optimize, and loses the benefit of native column types.

**Hybrid**: Shared base columns with type-specific extension tables. Adds complexity without clear benefit at MVP scale.
