## Summary

This automated develop-loop run implemented the following issues:

| Issue | Title |
|-------|-------|
| #749 | Filter collector health to remote-collector clients |
| #750 | Aggregate remote collector health at client level |
| #751 | Make Aurora Glass collector views liveness-first |

## Implemented Issues

- **#749** — Filter collector health to remote-collector clients
  - Filter `/health` `collectors[]` to only remote-collector clients (`remote-collector*` prefix) instead of reporting all clients.
- **#750** — Aggregate remote collector health at client level
  - Aggregate remote collector health at the client level rather than per individual collector instance.
- **#751** — Make Aurora Glass collector views liveness-first
  - Reorder Aurora Glass collector views so liveness/health status is shown first, with identity details secondary.

## Changes

- Filter `/health` to remote-collector* clients: the health endpoint's collector list now only includes clients whose registered name starts with `remote-collector`, so operational health signals aren't diluted by unrelated ingestion clients.
- Aggregate remote collector health at client level: collector health rows are rolled up to the owning client, giving one health signal per client rather than one per collector credential/instance.
- Make Aurora Glass collector views liveness-first: dashboard and status views lead with liveness (up/stale/down) so operators see service health before collector metadata.

## Review

A consolidated diff review is available at `.status/handoff/diff-review.md` (includes per-issue diff reviews for #749, #750, and #751).

Closes #749
Closes #750
Closes #751

---

*Note: Changes were pushed directly to `ai/feat/issues-749-750-751` by the autonomous develop-loop.*
