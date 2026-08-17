# Separate Aurora Glass from Gateway Service

Aurora Glass is delivered as a separate frontend container, while the Gateway remains an API service. Both artifacts are built from this repository, published from one CI workflow with shared version tags, and intended to share one public origin so Aurora Glass can keep using relative API paths without introducing CORS as part of this split.

## Normalized-event contract ownership and versioning

The FastAPI EDA Gateway (the producer, `fast-api-eda-gateway` #97–#102) owns the
executable normalized-event contract published in `docs/contracts/normalized-event-v1/`.
The contract is the producer's public API surface for the `afk.events` topic:
every consumer (including this Gateway's AFK Outcome Consumer) validates
incoming messages against this contract.

### Ownership

- **Owner**: FastAPI EDA Gateway (producer).
- **Published artifacts**: JSON Schema (`schema.json`) plus serializer-generated
  fixture examples (`fixtures/`) for every allowed `(resource_type, action)` pair.
- **Consumer validation**: The AFK Outcome Consumer's `NormalizedProviderEvent`
  Pydantic model is the consumer-side projection of this contract.  The consumer
  does not own the contract — it pins the producer's published schema and
  validates that real serializer output conforms to it.

### Closed-version policy

The normalized-event vocabulary is **closed by version**.  A semantic vocabulary
change — adding, removing, or renaming a `resource_type`, `action`, or field —
requires a **new explicit version** (e.g. `v2`).  The version is carried in the
`schema_version` field of every message and in the contract artifact directory
name.

Non-semantic changes that do **not** require a new version:

- Adding an optional field with a safe default (backward-compatible).
- Relaxing a validation constraint (e.g. making a required field optional).
- Documentation or fixture-only changes.

Semantic changes that **do** require a new version:

- Adding a new `resource_type` value (e.g. `commit`).
- Adding a new `action` value that changes the canonical vocabulary.
- Removing or renaming any field.
- Changing the type or semantics of an existing field.

A new version is published as a new directory (`docs/contracts/normalized-event-v2/`)
with its own schema and fixtures.  The previous version's artifacts remain
immutable — consumers pinning `v1` are unaffected.
