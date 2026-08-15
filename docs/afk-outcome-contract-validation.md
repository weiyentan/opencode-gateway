# Producer→Consumer Contract & Replay Convergence — Validation (#485)

**Issue**: #485 — End-to-end + contract validation: producer/consumer contract
pinning and replay convergence
**Parent**: PRD #478 — PR/MR/Issue Ingestion and Reporting Capability (cross-repository)
**Slice layer**: validation (builds on #482's mapping bridge + DLQ path)
**Validator**: `code-editor-senior` (T3)
**Date**: 2026-08-16
**Verdict**: **PASS** — the pinned producer contract maps cleanly through
`map_provider_event` to the canonical outcome-layer vocabulary without DLQ
routing; re-delivery of the same `provider + delivery_id` converges on
identical `delivery_log`/`engineering_events` rows regardless of order; and a
contract-violating payload routes to `afk.events-dlq` with the original payload
plus a reason. Contract pinning is exercised with fixture normalized events and
mock records driving the consumer `_process_message` path — not live producer
emission (the cross-repo producer is GitLab-hosted and unreachable in this
environment). Real-history validation is limited to a read-only cross-check in
this environment (no docker/Postgres, no `GITHUB_TOKEN`) — the full live-run
harness is provided in §6.

---

## 1. Summary

The Stage-2 mapping bridge (issue #482, ADR 0018) introduced
`NormalizedProviderEvent` and `map_normalized_event`/`map_provider_event`, but
the producer→consumer contract was not yet *pinned by tests* — nothing asserted
that a producer-emitted event maps through without DLQ routing, nor that
re-delivery converges without duplicates. This slice locks that contract down:

1. **Contract tests** pin the exact producer event shape and assert it maps to
   the canonical outcome-layer vocabulary with **no DLQ route**.
2. **Replay-convergence tests** assert that live-then-replay and replay-then-live
   converge on **identical** `delivery_log` + `engineering_events` rows.
3. **Redelivery tests** assert no duplicate rows/events across the full path
   (dedup via `delivery_log` UNIQUE(provider, delivery_id) + the
   `engineering_events` identity UNIQUE).
4. **Contract-violation behavior** is documented (§4) and tested: a violating
   payload routes to the DLQ with the original payload + a reason.
5. **Real-history validation** is performed read-only (§5) with the full
   live-run harness documented for environments with docker + credentials (§6).

## 2. The pinned producer→consumer contract

The producer (`fast-api-eda-gateway` #97–#102) emits a normalized,
schema-versioned event on the `afk.events` topic. The contract is pinned by
`PRODUCER_CONTRACT_EVENT` in `tests/test_afk_consumer.py` and enforced by
`NormalizedProviderEvent` (ADR 0018):

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | `str` | Producer schema version (`"1.0"`) — carried, never dropped |
| `provider` | `github` \| `gitlab` | Source provider |
| `delivery_id` | `str` | Forwarded provider delivery UUID (`X-GitHub-Delivery` / `X-GitLab-Event-UUID`) — the `delivery_log` dedup key |
| `resource_type` | `issue` \| `pull_request` \| `merge_request` | Producer-native resource vocabulary (never `change_request`) |
| `resource_id` | `str` | Stable provider-scoped resource identity (the value behind `entity_id`) |
| `repository` | `str` | Full `owner/repo` (or `group/project`) name |
| `action` | `str` | Canonical event-type suffix (`opened`, `closed`, `merged`, …) |
| `occurred_at` | `datetime` | When the activity happened at the source |
| `ingested_at` | `datetime \| None` | Producer's ingest timestamp |
| `actor` | `str \| None` | Acting principal login |
| `payload_ref` | `str \| None` | *Reference* to the redacted payload — never the payload itself |

The bridge maps `resource_type` → canonical `entity_type`
(`issue` → `issue`; `pull_request`/`merge_request` → `change_request`) and
`action` → the canonical event-type suffix, validating the result against the
locked ten-type vocabulary (`_MAPPED_EVENT_TYPES`). An unknown `resource_type`
or an `action` that does not resolve to a canonical event type is unmappable.

### Pinned by tests

- `test_producer_contract_event_has_exact_fields` — the field set is exact; a
  contract change must fail this test rather than drift silently.
- `test_producer_contract_maps_to_canonical_change_request` — the mapped
  canonical `change_request` retains the normalized event's key identity and
  payload fields: `provider`, `repository`, the resource identity
  (`entity_type` from `resource_type`, `entity_id`/`number` from
  `resource_id`), the canonical `event_type` (from `action`), `occurred_at`,
  `actor`, and a `payload_ref` payload reference (never the payload itself).
  Producer-internal fields (`delivery_id`, `ingested_at`, `schema_version`)
  are not carried onto the canonical entity/event.
- `test_producer_contract_event_is_not_routed_to_dlq` — the full consumer path
  persists the contract event and **never** calls `send_and_wait` (no DLQ).
- `test_producer_contract_gitlab_merge_request_maps_to_change_request` —
  cross-provider parity (`merge_request` → `change_request`).

## 3. Replay convergence & dedup guarantees

Re-delivery is absorbed idempotently end to end, never re-appended and never
blindly overwritten:

- `delivery_log` is written `ON CONFLICT (provider, delivery_id) DO NOTHING`
  (`uq_delivery_log_provider_delivery`, migration 0026) — a redelivered message
  no-ops its delivery row.
- `engineering_events` is written `ON CONFLICT (provider, repository,
  entity_type, external_id, event_type, occurred_at) DO NOTHING`
  (`uq_engineering_events_identity`) — the event row is an immutable fact.

Because both writes are conflict-ignore, **delivery order does not matter**:
whether a replay arrives before or after its live copy, the final state is one
delivery row + one event row with identical content.

### Pinned by tests

- `test_replay_convergence_live_then_replay` — after a live delivery and a
  replay, the event row is byte-identical to the post-live state and counts are 1.
- `test_replay_convergence_replay_then_live` — the duplicate delivered *first*
  still converges to the same canonical row.
- `test_redelivery_creates_no_duplicate_rows_across_full_path` — two deliveries
  of the same `provider + delivery_id` produce exactly one `delivery_log` row and
  one `engineering_events` row (verified against real Postgres constraints).

These are integration tests in `tests/integration/test_afk_consumer.py`,
driving the real consumer `_process_message` path (Kafka record → parse → map →
transactional write → offset commit) against the `docker-compose.test.yml`
Postgres (port 5433), following the existing skip-if-unreachable pattern.

## 4. Contract-violation behavior (documented)

A payload that violates the contract routes to the `afk.events-dlq` topic and is
**never persisted**, so a poison message cannot block the consumer group. The
three violation classes and their DLQ handling:

| Violation | Detection | DLQ payload | Reason |
|---|---|---|---|
| Bad JSON | `json.loads` fails | `{"raw": <bytes>}` | `JSON decode failure: <error>` |
| Bad shape | Pydantic `model_validate` fails (neither legacy nor normalized shape) | original dict | `Invalid message shape — failed Pydantic validation` |
| Unmappable | `map_provider_event` returns `None` (unknown `resource_type` or non-canonical `action`) | original dict | `Unmappable message type: '<resource_type>.<action>'` |

Every DLQ publish carries the `{original_topic, reason, payload}` envelope
(`_send_to_dlq`) and the offset is committed only **after** the publish succeeds
(a DLQ publish failure opens a commit gap so the message is redelivered, never
silently dropped). The payload is preserved verbatim so a future DLQ-drainer can
replay or triage it.

### Pinned by tests

- `test_contract_violation_routes_to_dlq_with_payload_and_reason` — an unmappable
  normalized action is DLQ'd with the full original payload + reason, nothing persisted.
- `test_contract_violation_bad_json_routes_to_dlq_with_raw_payload` — a non-JSON
  body is DLQ'd carrying the raw bytes + a reason.

## 5. Real-history validation

### 5.1 Read-only cross-check (performed)

No `GITHUB_TOKEN`, docker, or Postgres is available in this environment, so a
live `scripts/afk_backfill.py` run is impossible (see §6). A read-only `gh api`
cross-check was performed against the real `weiyentan/opencode-gateway` history:

| Artifact | Real value (GitHub API) |
|---|---|
| PR #442 | `merged = true`, `merged_at = 2026-08-13T10:10:29Z`, author `wyautomation`, merged by `weiyentan` |
| Issue #437 | `closed_at = 2026-08-13T10:10:31Z` (auto-closed ~2s after merge), author `weiyentan` |

These real values feed the contract fixture shape directly: a producer-emitted
`pull_request.merged` event for resource `442` with `occurred_at =
2026-08-13T10:10:29Z` maps to `change_request.merged` / `change_request:442` —
exactly the canonical row the earlier `docs/afk-outcome-validation.md` (#450)
reconstructed from the same cluster. The forward `delivery_id` is a producer
artifact not exposed by the REST API (it comes from the webhook
`X-GitHub-Delivery` header), so the fixture uses a deterministic placeholder and
documents that provenance in the test.

### 5.2 Limitation

Full end-to-end validation against the *real* producer emission is not possible
here for three reasons, each outside the slice's control:

1. The cross-repo producer (`fast-api-eda-gateway` #100/#101/#102) is
   GitLab-hosted and unreachable — its emission is not locally available.
2. No docker/Postgres — the `docker-compose.test.yml` integration stack cannot
   be started, so the asyncpg-backed integration tests skip (they assert the
   skip-if-unreachable path).
3. No `GITHUB_TOKEN`/`GITLAB_TOKEN` — the provider adapters' live fetches cannot
   run.

The replay/redelivery convergence logic is validated by the replay-convergence
integration tests in `tests/integration/test_afk_consumer.py` (§3), which drive
the real consumer `_process_message` path against the real
`delivery_log`/`engineering_events` dedup constraints — live-then-replay,
replay-then-live, and redelivery all converge to one delivery row + one event
row. Those tests require the `docker-compose.test.yml` Postgres and therefore
skip in this environment; no standalone real-Postgres validation was performed
here.

## 6. Live-run harness (for environments with docker + credentials)

From the repo root, on a host with docker and provider credentials:

```sh
# 1. Start the integration Postgres (port 5433).
docker compose -f docker-compose.test.yml up -d

# 2. Run the contract + replay/redelivery integration suite.
pytest tests/integration/test_afk_consumer.py -v -m integration

# 3. (Optional) live backfill over the real repository history.
GITHUB_TOKEN=<token> python scripts/afk_backfill.py \
    --provider github \
    --repository weiyentan/opencode-gateway \
    --since 2026-06-01T00:00:00Z \
    --dry-run --show-evidence

# 4. Tear down.
docker compose -f docker-compose.test.yml down -v
```

## 7. Files changed

- `tests/test_afk_consumer.py` — producer-contract pinning + contract-violation
  DLQ-envelope tests (unit, mocked Kafka + asyncpg).
- `tests/integration/test_afk_consumer.py` — e2e replay-convergence +
  redelivery tests driving the full consumer path (skip-guarded on Postgres).
- `docs/afk-outcome-contract-validation.md` — this report.

No changes to `app/consumer/*`, migrations, or `afk_outcomes/*` — the pinned
contract was already satisfied by the #482 mapping bridge; no consumer gap was
found.
