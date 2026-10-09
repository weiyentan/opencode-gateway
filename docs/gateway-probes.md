# Gateway Probe Contracts and Operator Behaviour

**Issue #772** — fail safely on PostgreSQL outage: 503 responses and
independent Gateway probes. **Issue #773** — auto-recover PostgreSQL
connectivity after startup and runtime outages. This document defines the
wire contracts of the three health/probe endpoints, the Kubernetes probe
configuration that consumes them, and what operators should expect during
a database outage — including automatic recovery.

## Endpoint contracts

All three endpoints below are **exempt from API-key authentication**
(`ApiKeyMiddleware.EXEMPT_PATHS`); kubelet probes never present
credentials, and the exemptions are deliberately limited to these routes.
Every other route keeps requiring the Admin API Key.

### `GET /live` — liveness (process)

| Condition | Response |
|-----------|----------|
| FastAPI process can serve requests (any DB state) | `200` `{"status": "ok", "data": {"alive": true}}` |

* Deliberately **never touches PostgreSQL** and never runs collector
  queries. The pool is not read, acquired, or probed.
* Kubernetes uses this to decide whether to **restart** the pod — because
  it never depends on the database, a PostgreSQL outage can never restart
  a healthy API process.
* Body is informational; only the status code is contractual.

### `GET /ready` — readiness (database usability)

| Condition | Response |
|-----------|----------|
| Pool initialized **and** bounded acquisition succeeds | `200` `{"status": "ok", "data": {"ready": true}}` |
| Pool missing / absent (`app.state.pool` is `None` or unset) | `503` `{"status": "error", "error": {"code": "SERVICE_UNAVAILABLE", ...}}` |
| Pool registered but uninitialized (`pool.pool is None`) | `503` (same envelope) |
| Bounded acquisition fails (unreachable/broken connection) | `503` (same envelope) |
| Probe exceeds the internal bound | `503` `... "Database readiness probe timed out"` |

* The whole probe runs under `READY_PROBE_TIMEOUT_SECONDS` (2.0s, constant
  in `app/api/health.py`) so a DB outage can never hang kubelet probes or
  request workers.
* A successful probe acquires a connection **and** performs a `SELECT 1`
  round-trip, then releases it — "usable", not merely "handed out".
* Kubernetes uses this to decide whether to route traffic to the pod:
  a DB outage marks the pod **NotReady** without restarting it.

### `GET /health` — structured health (unchanged)

`/health` keeps its existing structured JSON contract for dashboards and
MCP clients: `status`, `version`, `database` (`"connected"` /
`"disconnected"`), `last_ingest_timestamp`, `collectors[]`, and
`source_databases[]` (issues #749/#750). It is **not** used as a
Kubernetes probe and is not repurposed by this change. Docker-compose
`HEALTHCHECK` usage (if any) is unaffected.

## DB-backed API requests during an outage

`get_session` (`app/db/session.py`) now fails safely:

* `app.state.pool` missing or `None` → `503 SERVICE_UNAVAILABLE`
  (`"Database connection pool is not initialized"`) through the standard
  error envelope.
* Pool registered but `pool.pool is None` (startup pool connect failed) →
  same `503`.
* `acquire()` fails (Postgres went down after startup) → `503
  SERVICE_UNAVAILABLE` (`"Database is unavailable"`).

These replace the former `'NoneType' object has no attribute 'acquire'`
`AttributeError` / `RuntimeError` that surfaced as an unhandled HTTP 500.
The healthy path (acquire → yield → release) is unchanged.

## Kubernetes deployment probes (source of truth)

`k8s/gateway-deployment.yaml` is the in-repo source of truth for the
Gateway API Deployment:

```yaml
livenessProbe:
  httpGet: { path: /live, port: http }
  initialDelaySeconds: 10
  periodSeconds: 10
  timeoutSeconds: 3
  failureThreshold: 3
readinessProbe:
  httpGet: { path: /ready, port: http }
  initialDelaySeconds: 5
  periodSeconds: 5
  timeoutSeconds: 3
  failureThreshold: 3
```

If the live cluster applies the Gateway Deployment from an authoritative
deployment repository distinct from this repo, that repository's manifest
must mirror these paths and timeout/threshold values — they are the
contract. `timeoutSeconds` (3s) is kept above the application's internal
2s `/ready` bound so kubelet never cancels a probe that is still
legitimately evaluating. Do not modify CNPG / PostgreSQL operator
resources as part of this contract.

## Operator behaviour during a PostgreSQL outage

* **Traffic stops, process survives.** The pod flips to `NotReady` within
  roughly `periodSeconds × failureThreshold` (≈15s) while `Ready`/`Live`
  probes stay green — no restart loop caused by the database.
* **Log signals.**
  * `Database unavailable — connect attempt N failed; Gateway not ready;
    next attempt in X.XXs` (first failure of an outage, `app.db.session`).
  * `Database connection lost — Gateway marked not ready; supervised
    reconnect started (capped backoff 60.00s)` — the moment a healthy pool
    is detected as failed (single-flight wake from a request/probe
    acquisition failure).
  * `Ready probe: database acquisition failed` / `Ready probe: timed out
    after 2.0s` warnings on `/ready`; `Database connection acquisition
    failed` warnings on API requests (`app.api.health` /
    `app.db.session`).
  * `Database connectivity recovered ... — Gateway ready` when the
    automatic reconnection publishes a verified replacement pool.
  * All reconnect logs are bounded by the capped backoff and pass through
    the standard redacting formatter — no credentials are ever logged.
* **Automatic recovery (issue #773).** The Gateway supervises its pool;
  it does **not** need a restart:

  ```
  Postgres unreachable at startup          Postgres lost after startup (failover)
  ───────────────────────────────          ────────────────────────────────────────
  /live 200                                /live 200
  /ready 503   ─┐                          /ready 200 (healthy)
  /health.disconnected                    /ready 503   (first acquisition failure
  DB API 503    │ supervised reconnect     ─┐            wakes the single-flight loop)
                 │ with capped backoff      │ supervised reconnect
  Postgres back ─┘                          └─ Postgres back → /ready 200,
  /ready 200, /health connected,            /health connected, DB API normal
  DB API normal                             (pool replaced only after a verified
   (no restart)                              connection test + schema init)
  ```

  One background task (**never one task per request**) performs each
  attempt with a bounded per-attempt timeout (`reconnect_timeout_seconds`,
  default 10s) and capped exponential backoff with jitter:
  `reconnect_initial_backoff_seconds` (1s) doubles up to
  `reconnect_max_backoff_seconds` (60s), scaled by ±
  `reconnect_jitter_ratio` (0.2) — a sustained outage can never busy-loop
  or log without bound.  A candidate pool is published as the active
  `app.state.pool` only after a `SELECT 1` connection test AND the
  required schema initialization (`ensure_schema` — Alembic) complete;
  migrations never run concurrently (the loop serialises attempts) and an
  incompletely initialized pool is never exposed.  Environment knobs:
  `GATEWAY_RECONNECT_TIMEOUT_SECONDS`,
  `GATEWAY_RECONNECT_INITIAL_BACKOFF_SECONDS`,
  `GATEWAY_RECONNECT_MAX_BACKOFF_SECONDS`,
  `GATEWAY_RECONNECT_JITTER_RATIO`.
* **Wire-level smoke test.** Stop the database (or partition it) and
  observe: `GET /ready` → `503`, `GET /live` → `200`, authenticated
  `GET /api/v1/afk-outcomes/change-requests` → structured `503` envelope;
  restore the database and observe readiness return to `200` and the DB
  API return to normal — without restarting the pod.  The in-repo
  integration test
  (`tests/integration/test_db_reconnect_smoke.py`, against
  `docker compose -f docker-compose.test.yml up -d`) automates exactly
  this scenario for both a startup outage and a runtime outage (via a TCP
  partition proxy that kills established connections and refuses new
  ones), asserting unready → reconnecting → ready → normal API.