## Summary

This automated develop-loop run implemented the following issue:

| Issue | Title |
|-------|-------|
| #664 | docs: reframe README to current observability direction — deprecation appendix + purge superseded ADRs |

## Changes

- Added "Deprecated / compatibility-only" appendix to README
- Moved `GATEWAY_AFK_OUTCOMES_TOPIC` / `GATEWAY_AFK_OUTCOMES_DLQ_TOPIC` and legacy `external_session_id` normalization to appendix
- Kept `active_tokens` deprecation + sunset in live tables
- Removed superseded ADRs 0002, 0003, 0020 from ADR index
- Deleted ADR files 0002, 0003, 0020
- Created new ADR 0030: superseded ADR retention policy
- Fixed dangling references in CONTEXT.md and docs/

## Review

A consolidated diff review is available.

Closes #664
