# ADR 0024: Preserve AWX execution binding history

## Status

Accepted

## Context

The Gateway needs to relate an AWX job and its OpenCode session to a GitHub pull request or GitLab merge request. A failed job may be retried for the same change request, so the change request cannot be the uniqueness boundary. The existing AFK correlation tables also represent inferred relationships and do not provide an explicit AWX execution identity.

## Decision

Add a dedicated execution-binding persistence model and API. Each AWX job is one execution binding with its own OpenCode session, terminal outcome, optional EDA source event ID, and normalized provider resource identity. A resource may have many bindings, including failed and later successful jobs. Idempotency is keyed by AWX job ID: identical repeats are no-ops and conflicting repeats are rejected. GitHub pull requests and GitLab merge requests use the canonical `change_request` identity. The write endpoint uses a dedicated collector credential; raw tokens and arbitrary AWX `extra_vars` are never persisted.

The implementation comprises Alembic migration 0037, the `ExecutionBinding` ORM model (`app/db/models/afk.py`), `POST /api/v1/afk/executions` (write path), `GET /api/v1/afk/executions` (list filtered by provider resource), and `GET /api/v1/afk/executions/{awx_job_id}` (single binding by AWX job ID). The write path uses a dedicated `awx-execution-bindings` integration client with two-layer authentication (see AWX request credential contract below).

## AWX request credential contract (issue #550)

The execution-binding write path reuses the existing two-layer
authentication mechanism — no new authentication system is introduced.

**Dedicated integration client.** `POST /api/v1/afk/executions`
accepts only collector credentials attributable to the dedicated AWX
execution-binding integration client, identified by the client name
`awx-execution-bindings` (the module constant
`AWX_EXECUTION_BINDING_CLIENT_NAME` in `app/api/afk_executions.py`).
A valid credential owned by any other client — including the usage
collector's `opencode-collector` client — is rejected with `403
FORBIDDEN`. This client is provisioned through the existing admin
clients API (`POST /admin/clients`) and is never shared with other
pipelines.

**Request contract.** AWX sends its execution-binding callback as
`POST /api/v1/afk/executions` with a single `Authorization: Bearer
<token>` header. The request must pass both existing layers:

1. `ApiKeyMiddleware` — the bearer token must match `GATEWAY_API_KEY`
   (layer 1; unchanged global boundary).
2. `require_collector_token` — the SHA-256 hash of the same bearer
   token must be a non-revoked `collector_credentials` row owned by
   the active `awx-execution-bindings` client (layer 2).

Operationally this means registering the SHA-256 hash of
`GATEWAY_API_KEY` as a collector credential of the dedicated client
(the existing Admin-API-Key bootstrap pattern) and having AWX present
`GATEWAY_API_KEY` as its bearer token. The dedicated credential row —
not a distinct header scheme — is what makes the write path
attributable to the AWX integration and keeps it separate from
`opencode-collector`. Provision the credential with a placeholder-free
value from the operator's secret store; never commit a real token,
key, or hash to source control or documentation.

**Failure behavior.** Missing, malformed, empty, invalid, revoked, and
inactive credentials are rejected with `401 UNAUTHORIZED`, using the
same error codes and messages as the existing `/ingest`
collector-token path. Read endpoints (`GET /api/v1/afk/executions`
and `GET /api/v1/afk/executions/{awx_job_id}`) remain protected by the
global `ApiKeyMiddleware` boundary alone and accept the Admin API Key.

**Secrets handling.** Only the SHA-256 `token_hash` is ever persisted
in `collector_credentials`. Raw bearer tokens are never persisted,
returned by any endpoint, or written to logs.
