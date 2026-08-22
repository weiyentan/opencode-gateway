# PRD: AWX Execution Bindings

## Problem Statement

The Gateway currently correlates OpenCode sessions with GitHub and GitLab engineering activity primarily through inferred relationships. It has no durable, explicit relationship between an AWX job, the OpenCode session started by that job, and the GitHub pull request or GitLab merge request being worked on.

This creates ambiguity when a job fails and is retried. A failed execution and its later successful retry can target the same change request, but the Gateway cannot currently preserve both execution histories or distinguish explicit execution evidence from temporal correlation. The existing Ansible integration is expected to expose the OpenCode session ID separately; this PRD covers only the Gateway-side receiving, persisting, and querying of that binding.

## Solution

Add an explicit execution-binding capability to the Gateway. An authenticated AWX callback records one AWX job execution, its OpenCode external session ID, its terminal outcome, and the normalized GitHub/GitLab change-request identity it served.

The Gateway will persist one binding per AWX job. A GitHub pull request or GitLab merge request may therefore have many bindings, including failed executions followed by successful retries. Repeated callbacks for the same AWX job are idempotent; conflicting data for an existing AWX job is rejected.

## User Stories

1. As an AWX integration, I want to submit completed, failed, or cancelled execution bindings, so that every terminal attempt remains observable.
2. As an AWX integration, I want a dedicated collector credential, so that this integration can be independently revoked and audited.
3. As an AWX integration, I want to identify a target using provider, normalized repository identity, canonical entity type, and entity number, so that I do not need Gateway database IDs.
4. As an AWX integration, I want to include the OpenCode external session ID, so that the Gateway can connect the execution to the exact OpenCode session rather than an inferred temporal match.
5. As an AWX integration, I want repeated callbacks for one AWX job to be safe, so that network retries do not create duplicates or rewrite history.
6. As an AWX integration, I want a new AWX job targeting the same change request to be accepted, so that restarted jobs preserve prior failed attempts.
7. As an operator, I want to retrieve one binding by AWX job ID and all bindings by provider resource identity, so that I can inspect execution history.
8. As an operator, I want GitHub pull requests and GitLab merge requests represented as `change_request`, so that consumers use one provider-neutral vocabulary.
9. As an operator, I want branch, title, timestamps, source event ID, and bounded redacted failure summaries retained when supplied, so that records remain useful for audit and troubleshooting.
10. As a security operator, I want raw tokens, stdout, prompts, extra variables, and arbitrary payloads excluded from persistence and responses.
11. As a maintainer, I want the schema change reversible and the binding storage isolated from inferred AFK correlation.

## Implementation Decisions

- Use a dedicated execution-binding persistence model with one row per AWX job execution.
- Enforce unique AWX job identity at the database level; do not make provider resource identity unique.
- Make creation atomic under concurrent callbacks: one request creates the row, identical replays return the stored row, and conflicting replays return `409` without mutation.
- Require a non-empty OpenCode external session ID on new callback writes. Database/read compatibility may remain nullable for legacy or reconciliation rows.
- Normalize repository identity at the API boundary using the Gateway's existing repository URL normalizer for writes and reads; reject invalid values.
- Accept GitHub `pull_request` or `change_request`, and GitLab `merge_request` or `change_request`; reject provider/type mismatches and persist only canonical `change_request`.
- Accept terminal outcomes `completed`, `failed`, and `cancelled`.
- Canonicalize failure metadata as `failure_summary`; redact recognizable bearer tokens and common `token`, `key`, `password`, and `secret` assignments before truncating to 1000 characters.
- Reject failure metadata on completed executions; failed and cancelled executions may omit it.
- Persist optional source event ID, branch, title, and start/finish timestamps when supplied.
- Never persist raw AWX extra variables, stdout, prompts, model configuration, tokens, or arbitrary payloads.
- Expose `POST /api/v1/execution-bindings`, `GET /api/v1/execution-bindings/{awx_job_id}`, and a provider-resource query at `GET /api/v1/execution-bindings`.
- The provider-resource query at `GET /api/v1/execution-bindings` accepts query parameters `?provider=github&repository=org/repo&entity_type=change_request&entity_number=42` to filter by provider resource identity.
- Preserve the two-layer authentication boundary: global Admin API Key middleware plus a collector credential owned by `awx-execution-bindings`.
- Order resource history by `created_at ASC, id ASC`.
- Modify the existing migration `0037` if needed; do not add a duplicate migration or backfill historical executions. Keep the migration additive and reversible.
- Preserve separation between pure AFK domain models and application/database infrastructure.
- Do not change Ansible wrappers, EDA rules, Kafka behavior, provider API lookup behavior, inferred correlation, frontend behavior, or deployment manifests.

## Testing Decisions

- Assert externally observable API, persistence, migration, authentication, idempotency, normalization, and query behavior rather than private helper details.
- Test GitHub PR and GitLab MR normalization, provider/type mismatch rejection, repository URL normalization, and invalid identity rejection.
- Test completed, failed, and cancelled outcomes, including failure-summary redaction, bounding, and completed-outcome rejection.
- Test identical replay, conflicting replay, multiple jobs for one resource, and deterministic history ordering.
- Test missing, malformed, revoked, invalid, and wrong-client collector credentials through existing authentication behavior.
- Test that sensitive fields are structurally absent from persistence and responses.
- Test domain models without FastAPI, asyncpg, or application imports.
- Test the existing migration's table, constraints, indexes, and rollback behavior.
- Add a real PostgreSQL integration test for concurrent identical and conflicting callbacks; mocks alone cannot prove the atomic idempotency guarantee.

## Out of Scope

- Exposing the OpenCode session ID from Ansible wrappers or changing wrapper/session lifecycle behavior.
- AWX relaunch or retry orchestration.
- EDA webhook schema, Kafka delivery, consumer retry, or DLQ changes.
- Automatic provider API lookups during ingestion.
- Temporal or heuristic correlation changes.
- An attempt table, retry parent links, or attempt state machine.
- Persisting complete AWX payloads, stdout, prompts, model policy, tokens, or extra variables.
- Frontend, Aurora Glass, deployment manifests, AWX credential provisioning automation, or Ansible playbook changes.
- Historical backfill or issue-tracker writes.

## Further Notes

- The Gateway repository contains `0037_execution_binding.py`; this PRD governs the contract and corrections to that migration rather than creating a duplicate.
- `CONTEXT.md`, ADR 0024, and ADR 0007 define the execution-binding vocabulary, history semantics, and two-layer collector authentication.
- The dedicated collector credential must be provisioned through the existing Gateway client/credential mechanism before AWX can call the write endpoint.
- Explicit execution evidence must remain separate from existing inferred AFK correlation and available for future projections and operational tooling.
