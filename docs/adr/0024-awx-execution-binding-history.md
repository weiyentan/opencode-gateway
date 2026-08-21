# ADR 0024: Preserve AWX execution binding history

## Status

Accepted

## Context

The Gateway needs to relate an AWX job and its OpenCode session to a GitHub pull request or GitLab merge request. A failed job may be retried for the same change request, so the change request cannot be the uniqueness boundary. The existing AFK correlation tables also represent inferred relationships and do not provide an explicit AWX execution identity.

## Decision

Add a dedicated execution-binding persistence model and API. Each AWX job is one execution binding with its own OpenCode session, terminal outcome, optional EDA source event ID, and normalized provider resource identity. A resource may have many bindings, including failed and later successful jobs. Idempotency is keyed by AWX job ID: identical repeats are no-ops and conflicting repeats are rejected. GitHub pull requests and GitLab merge requests use the canonical `change_request` identity. The write endpoint uses a dedicated collector credential; raw tokens and arbitrary AWX `extra_vars` are never persisted.

The slice includes Alembic migration(s), persistence/model code, `POST /api/v1/afk/executions`, and read paths by AWX job or provider resource. It does not redesign Ansible, EDA, Kafka, AWX retry behavior, or the wrapper.
