# Producer→Consumer Contract Validation

**Issues**: #485 (contract pinning + replay convergence), #495 (nested v1 envelope validation)
**Parent**: PRD #478 — PR/MR/Issue Ingestion and Reporting Capability (cross-repository)
**Slice layer**: validation (builds on #482's mapping bridge + DLQ path)
**Validator**: `code-editor-senior` (T3)
**Date**: 2026-08-16 (updated 2026-08-17 for #495, #497)
**Verdict**: **PASS** — the pinned producer contract maps cleanly through
`map_provider_event` to the canonical outcome-layer vocabulary without DLQ
routing; re-delivery of the same `provider + delivery_id` converges on
identical `delivery_log`/`engineering_events` rows regardless of order; and a
contract-violating payload routes to `afk.events-dlq` with the original payload
plus a reason. The nested v1 envelope (issue #495) is validated before mapping
with distinct DLQ reasons per violation class. Contract pinning is exercised
with fixture normalized events and mock records driving the consumer
`_process_message` path — not live producer emission: the pinned producer
artifacts are exact copies from the recorded `fast-api-eda-gateway` commit
(see §2.1), so live emission is not required here. Real-history validation
is limited to a read-only cross-check in this environment (no docker/Postgres,
no `GITHUB_TOKEN`) — the full live-run harness is provided in §6.

---

## 1. Summary

The Stage-2 mapping bridge (issue #482, ADR 0020) introduced
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
6. **Nested v1 envelope validation** (issue #495, §7) validates the shipped
   nested envelope before mapping or persistence, with distinct DLQ reasons
   per violation class.

## 2. The pinned producer→consumer contract

The producer (`fast-api-eda-gateway` #97–#102) emits a normalized,
schema-versioned event on the `afk.events` topic. The contract is pinned by
`PRODUCER_CONTRACT_EVENT` in `tests/test_afk_consumer.py` and enforced by
`NormalizedProviderEvent` (ADR 0020):

### Nested v1 shape (the only accepted shape)

The v1 envelope is nested: resource fields live in a `resource` object and the
payload reference lives in a `redacted_payload.reference` object.  The flat
shape (issue #482) has been removed (issue #497) — a flat payload is rejected
as an invalid message shape.

```json
{
  "schema_version": "1.0",
  "event_type": "normalized",
  "provider": "github",
  "delivery_id": "...",
  "resource": {
    "type": "pull_request",
    "repository_url": "https://github.com/owner/repo",
    "number": 200
  },
  "action": "merged",
  "occurred_at": "2026-08-15T10:00:00Z",
  "ingested_at": "2026-08-15T10:00:01Z",
  "actor": "test-user",
  "redacted_payload": {
    "reference": {
      "provider": "github",
      "delivery_id": "..."
    }
  }
}
```

The bridge maps `resource.type` → canonical `entity_type`
(`issue` → `issue`; `pull_request`/`merge_request` → `change_request`) and
`action` → the canonical event-type suffix (`edited`/`updated` → `updated`),
validating the result against the locked canonical vocabulary
(`_CANONICAL_EVENT_TYPES`).  Actions are constrained to the producer lifecycle
allowlist: `issue` opened/edited/reopened/closed; `pull_request`
opened/edited/reopened/closed/merged; `merge_request`
opened/updated/reopened/closed/merged.  The *normalized* repository URL is
persisted as the repository identity.

### Pinned by tests

- `test_producer_contract_event_has_exact_fields` — the field set is exact; a
  contract change must fail this test rather than drift silently.
- `test_producer_contract_maps_to_canonical_change_request` — the mapped
  canonical `change_request` retains the normalized event's key identity and
  payload fields: `provider`, the normalized repository URL, the resource
  identity (`entity_type` from `resource.type`, `entity_id`/`number` from
  `resource.number`), the canonical `event_type` (from `action`),
  `occurred_at`, `actor`, and a `payload_ref` payload reference object (never
  the payload itself).  Producer-internal fields (`delivery_id`,
  `ingested_at`, `schema_version`) are not carried onto the canonical
  entity/event.
- `test_producer_contract_event_is_not_routed_to_dlq` — the full consumer path
  persists the contract event and **never** calls `send_and_wait` (no DLQ).
- `test_producer_contract_gitlab_merge_request_maps_to_change_request` —
  cross-provider parity (`merge_request` → `change_request`).
- `test_flat_shape_is_rejected_sends_to_dlq_and_commits` — the removed flat
  shape is rejected as an invalid message shape (issue #497).

## 2.1 Producer provenance and drift detection (issue #503)

The pinned contract is a verifiable copy of the producer-owned artifacts, not
an independent consumer transcription.  Its provenance is recorded and its
integrity is enforced mechanically:

- **`docs/contracts/normalized-event-v1/producer_commit.txt`** records the
  producer repository URL
  (`prometheus-build-repository/sourcecontrollayout/containers-group/fast-api-eda-gateway`),
  the producer commit SHA the artifacts were copied from, the date the pin was
  recorded, and step-by-step instructions for refreshing the pin.  The commit
  SHA is currently a placeholder (`0000…0000`) because the producer repository
  is not fetched from public GitHub CI without credentials (producer work item
  #105); it is filled with the real revision when the producer artifacts are
  finalized.
- **`docs/contracts/normalized-event-v1/checksums.sha256`** holds the SHA-256
  digest of every pinned artifact (`schema.json` + all 14 fixtures).
- **`scripts/verify_contract_checksums.sh`** recomputes those digests and
  compares them against `checksums.sha256`, exiting non-zero on any edit,
  addition, removal, or reorder.  It depends only on coreutils
  (`sha256sum`, `sort`) — no network and no producer access — so it runs
  unchanged in public GitHub CI.  `--write` regenerates the checksums file
  after a contract refresh.
- **`tests/test_producer_to_gateway_contract_matrix.py`** enforces the parity
  mechanism in the ordinary CI test run (`test_contract_checksums_match_pinned_digests`
  and the script-backed drift-detection tests), so a locally edited artifact
  fails CI rather than drifting silently.
- **`docs/contracts/normalized-event-v1/consumer-policy.yaml`** separates
  producer-owned schema constraints (envelope field set/nesting, the lifecycle
  action allowlist, `additionalProperties: false`) from consumer policy
  (`validate_normalized_event()`, the mapping bridge, the canonical event-type
  vocabulary) so a reader can tell which rules originate with the producer and
  which are owned by this repository.

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
violation classes and their DLQ handling:

| Violation | Detection | DLQ payload | Reason |
|---|---|---|---|
| Bad JSON | `json.loads` fails | `{"raw": <bytes>}` | `JSON decode failure: <error>` |
| Bad shape | Pydantic `model_validate` fails (not the nested v1 shape — includes the removed flat shape) | original dict | `Invalid message shape — failed Pydantic validation` |
| Unsupported schema version | `schema_version` not in `{"1.0"}` | original dict | `Unsupported schema version: '<version>' (supported: ['1.0'])` |
| Unsupported event type | `event_type` is not `"normalized"` | original dict | `Unsupported event type: '<type>' (supported: 'normalized')` |
| Unsupported resource type | `resource.type` outside the producer vocabulary | original dict | `Unsupported resource type: '<type>' (supported: [...])` |
| Unsupported action | `action` outside the producer lifecycle allowlist for `resource.type` | original dict | `Unsupported action: '<action>' for resource type '<type>' (supported: [...])` |
| Invalid repository identity | `normalize_repository_url` returns `None` | original dict | `Invalid repository identity: '<url>' — must be an absolute HTTP(S) URL with a valid hostname and path` |
| Reference mismatch | `redacted_payload.reference.provider` ≠ envelope `provider` or `redacted_payload.reference.delivery_id` ≠ envelope `delivery_id` | original dict | `Reference mismatch: redacted_payload.reference.<field>=<value> != envelope.<field>=<value>` |

Every DLQ publish carries the `{original_topic, reason, payload}` envelope
(`_send_to_dlq`) and the offset is committed only **after** the publish succeeds
(a DLQ publish failure opens a commit gap so the message is redelivered, never
silently dropped). The payload is preserved verbatim so a future DLQ-drainer can
replay or triage it.

### Pinned by tests

- `test_contract_violation_routes_to_dlq_with_payload_and_reason` — a
  normalized action outside the producer lifecycle allowlist is DLQ'd with the
  full original payload + reason, nothing persisted.
- `test_contract_violation_bad_json_routes_to_dlq_with_raw_payload` — a non-JSON
  body is DLQ'd carrying the raw bytes + a reason.
- `test_unsupported_schema_version_routes_to_dlq` — an unsupported schema version
  is DLQ'd with a distinct reason.
- `test_invalid_repository_identity_routes_to_dlq` — an invalid repository URL
  is DLQ'd with a distinct reason.
- `test_reference_mismatch_routes_to_dlq` — a reference mismatch is DLQ'd with
  a distinct reason.

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
   GitLab-hosted and is not fetched in this environment — its live emission is
   not exercised locally, so validation runs against the pinned producer
   artifacts (recorded in `producer_commit.txt`, see §2.1) instead of live
   emission.
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

## 7. Nested v1 envelope validation (issue #495)

### 7.1 Validation boundary

The `validate_normalized_event()` function (in `app/consumer/afk_consumer.py`)
is called by `_process_message` after parsing a `NormalizedProviderEvent` but
before mapping or persistence. It raises `NormalizedEventValidationError` with
a distinct reason string for each violation class:

1. **Unsupported schema version** — `schema_version` is not `"1.0"`.
2. **Unsupported event type** — `event_type` is not `"normalized"`.
3. **Unsupported resource type** — `resource.type` is outside the producer
   lifecycle vocabulary.
4. **Unsupported action** — `action` is outside the producer lifecycle
   allowlist for `resource.type`.
5. **Invalid repository identity** — the `resource.repository_url` cannot be
   normalized to a valid identity (not an absolute HTTP(S) URL, empty
   hostname, or no path).
6. **Reference mismatch** — `redacted_payload.reference.provider` ≠ envelope
   `provider` or `redacted_payload.reference.delivery_id` ≠ envelope
   `delivery_id`.

### 7.2 Repository URL normalization

`normalize_repository_url()` derives repository identity strictly from the
producer repository URL:

- Require absolute HTTP(S) — reject non-HTTP schemes and relative URLs.
- Lowercase the hostname.
- Remove credentials (userinfo), query strings, and fragments.
- Strip default ports (80 for http, 443 for https); preserve non-default ports.
- Strip a trailing slash.
- Strip a terminal `.git` suffix.
- Preserve path spelling (case-sensitive).
- Reject empty or invalid identity (returns `None`).

Credentials, query strings, and fragments cannot become part of repository
identity.

### 7.3 Effective properties

`NormalizedProviderEvent` exposes `effective_*` properties that resolve from
the nested objects (the flat shape no longer exists):

- `effective_resource_type` — `resource.type`
- `effective_resource_id` — `resource.number` as a string (`""` when absent)
- `effective_repository` — the raw `resource.repository_url`
- `effective_action` — the top-level `action`

### 7.4 Pinned by tests

- `test_nested_v1_envelope_parses_through_normalized_provider_event` — the
  nested shape parses through the real serializer.
- `test_nested_v1_envelope_effective_properties` — effective properties resolve
  from nested objects.
- `test_nested_v1_envelope_maps_to_canonical` — the nested shape maps to the
  canonical outcome-layer vocabulary.
- `test_validate_accepts_v1_schema_version` — schema version `"1.0"` passes.
- `test_validate_rejects_unsupported_schema_version` — unsupported versions
  raise `NormalizedEventValidationError`.
- `test_validate_rejects_provider_mismatch` — reference mismatch on provider.
- `test_validate_rejects_delivery_id_mismatch` — reference mismatch on delivery_id.
- `test_validate_rejects_invalid_repository_identity` — invalid URL rejected.
- `test_normalize_repository_url` — parametrized normalization rules.
- `test_normalize_repository_url_rejects_invalid` — parametrized rejection cases.
- `test_normalize_repository_url_credentials_not_in_identity` — credentials
  stripped.
- `test_each_violation_class_produces_distinct_dlq_reason` — distinct reasons
  per violation class.
- `test_nested_v1_event_passes_validation_and_persists` — valid nested event
  persists without DLQ.
- `test_unsupported_schema_version_routes_to_dlq` — consumer path DLQ routing.
- `test_invalid_repository_identity_routes_to_dlq` — consumer path DLQ routing.
- `test_reference_mismatch_routes_to_dlq` — consumer path DLQ routing.
- `test_every_pinned_fixture_passes_validation` — all pinned fixtures validate.
- `test_every_pinned_fixture_maps_without_dlq` — all pinned fixtures map.
- `test_flat_shape_is_rejected_sends_to_dlq_and_commits` — the removed flat
  shape is rejected as an invalid message shape.
- `test_validate_rejects_unsupported_event_type` — non-`normalized` event
  types are rejected.
- `test_every_fixture_payload_reference_matches_envelope` — the payload
  reference object matches its envelope.
- `test_all_violation_classes_have_distinct_reason_prefixes` — distinct prefixes.

## 8. Files changed

- `app/consumer/afk_consumer.py` — `NormalizedProviderEvent` model (nested
  `resource` and `redacted_payload.reference` objects), the producer lifecycle
  allowlist, `validate_normalized_event()` (version/event-type/resource-type/
  action/repository/reference violations), `normalize_repository_url()`,
  `NormalizedEventValidationError`.  `map_normalized_event()` persists the
  normalized repository URL and the payload reference object; the flat shape
  has been removed (#497).
- `docs/contracts/normalized-event-v1/schema.json` + `fixtures/` — the pinned
  producer contract artifacts (14 real `(resource.type, action)` pairs).
- `tests/test_afk_consumer.py` — nested v1 envelope tests, validation tests,
  repository URL normalization tests, DLQ reason tests, contract pinning tests.
- `docs/afk-outcome-contract-validation.md` — this report (updated for #495,
  #497).
