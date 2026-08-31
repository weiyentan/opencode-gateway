/* ═══════════════════════════════════════════════════════════════════════════
   Change-request adapters and formatters (issue #612)
   ═══════════════════════════════════════════════════════════════════════════
   Pure, framework-agnostic modules that adapt Gateway-owned change-request
   summary and detail contracts into stable UI view models.

   Guarantees:
   * Pure — no DOM access, no fetch, no side effects.  Data composition only;
     rendering belongs to the render layer (app.js), never here.
   * Identity-preserving — provider / repository / external-number identity is
     carried through untouched and never fabricated.
   * No browser-side joins — each adapter consumes ONE composite contract and
     never reconstructs relationships by joining unrelated partial payloads
     (the Gateway owns the composite read models).

   The module exposes itself on `window.ChangeRequestAdapters` in the browser
   (loaded before app.js in index.html) and via `module.exports` for the Node
   test harness — both paths execute the same production functions.
   ═══════════════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  /** The canonical display label for missing/unavailable cost telemetry.
   *  Missing cost data must never be mistaken for zero cost (PRD story 22). */
  var COST_UNAVAILABLE = 'Cost unavailable';

  /** Identity fallback when a change-request source carries no provider. */
  var UNKNOWN_PROVIDER = 'unknown';

  // ── Identity adapters ──────────────────────────────────────────────────
  // The change-request identity is the stable tuple
  // (provider, repository, external_id).  GitHub pull requests and GitLab
  // merge requests both normalize to the canonical change_request identity
  // on the write path, so the frontend adapters only ever see the canonical
  // provider vocabulary (github | gitlab).  Identity is preserved verbatim —
  // never derived, never merged with other payloads.

  /** Extract the stable change-request identity tuple from a summary or
   *  detail contract.  Accepts the documented field vocabulary defensively
   *  (`external_id` / `resource_number` / `number`) and always returns the
   *  canonical shape.  Pure — no DOM or fetch access.
   *  @param {Object|null} cr - change-request object from the API
   *  @returns {{provider: string, repository: string, external_id: string}} */
  function crIdentity(cr) {
    cr = cr || {};
    var external = cr.external_id != null ? cr.external_id
      : (cr.resource_number != null ? cr.resource_number
      : (cr.number != null ? cr.number : ''));
    return {
      provider: cr.provider || UNKNOWN_PROVIDER,
      repository: cr.repository || cr.repository_url || cr.repo || '',
      external_id: external == null ? '' : String(external)
    };
  }

  /** Compose a compact display identity, e.g. `acme/web-app#142`.
   *  Provider-neutral: the render layer chooses PR/MR terminology via
   *  crProviderTerm.  Pure string builder — no HTML.
   *  @param {Object|null} cr - change-request object from the API
   *  @returns {string} e.g. "acme/web-app#142" */
  function crDisplayId(cr) {
    var id = crIdentity(cr);
    if (id.repository && id.external_id) {
      return id.repository + '#' + id.external_id;
    }
    return id.repository || id.external_id || '--';
  }

  /** Derive the provider-specific term for a change request.
   *  GitHub -> "PR", GitLab -> "MR", anything else -> "CR" (the canonical
   *  fallback used by the existing app.js providerCrTerm helper).
   *  @param {string|null} provider
   *  @returns {string} e.g. "PR", "MR", "CR" */
  function crProviderTerm(provider) {
    var p = (provider || '').toLowerCase();
    if (p === 'github') return 'PR';
    if (p === 'gitlab') return 'MR';
    return 'CR';
  }

  // ── Provider state adapters ────────────────────────────────────────────
  // Provider state is read from stored normalized events (opened / updated /
  // reopened / closed / merged) and is NEVER conflated with AFK automation
  // state.  The two statuses render independently (PRD stories 6–8).

  /** Resolve the stored provider state value from a change-request contract.
   *  Prefers the dedicated `provider_state` field; falls back to `state`
   *  (the provenance-timeline vocabulary).  Normalizes the provider-native
   *  `opened` to the display value `open`.  Missing → ''.
   *  @param {Object|null} cr
   *  @returns {string} e.g. "merged" | "open" | "reopened" | "closed" | "" */
  function providerStateValue(cr) {
    cr = cr || {};
    var v = cr.provider_state != null ? cr.provider_state : cr.state;
    if (v == null || v === '') return '';
    v = String(v);
    return v === 'opened' ? 'open' : v;
  }

  /** Human label for a provider state value.  `open` renders as "open"
   *  (provider-native `opened` normalizes to it); merged/reopened/closed
   *  pass through verbatim; null/absent → '--'.
   *  @param {string|null} state
   *  @returns {string} */
  function providerStateLabel(state) {
    if (state == null || state === '') return '--';
    return String(state);
  }

  /** Map a provider state value to a status-badge CSS class.  Reuses the
   *  existing badge vocabulary (badge-merged / badge-open / badge-closed);
   *  reopened renders as open (it is open again); anything else → unknown.
   *  @param {string|null} state
   *  @returns {string} badge class */
  function providerStateBadgeClass(state) {
    if (state === 'merged') return 'badge-merged';
    if (state === 'open' || state === 'reopened') return 'badge-open';
    if (state === 'closed') return 'badge-closed';
    return 'badge-unknown';
  }

  // ── AFK automation state adapters ──────────────────────────────────────
  // AFK automation state is the observed execution aggregation lifecycle:
  // pending (provisioned, not yet launched), running, completed, failed,
  // cancelled, stale.  `completed` means the observed execution aggregation
  // completed — it does NOT imply the PR/MR merged (PRD story 9).

  /** Resolve the AFK automation state value from a change-request contract.
   *  Accepts the documented vocabulary (`automation_state` / `afk_state` /
   *  `afk_status` / `status`) plus the run-nested `run.status` shape used by
   *  the AFK run detail.  Missing → ''.
   *  @param {Object|null} cr
   *  @returns {string} e.g. "pending" | "running" | "completed" | ... */
  function afkStateValue(cr) {
    cr = cr || {};
    var v = cr.automation_state != null ? cr.automation_state
      : (cr.afk_state != null ? cr.afk_state
      : (cr.afk_status != null ? cr.afk_status
      : (cr.status != null ? cr.status
      : (cr.run && cr.run.status != null ? cr.run.status : ''))));
    return v == null ? '' : String(v);
  }

  /** Human label for an AFK automation state value.  The locked RunStatus /
   *  provisioning vocabulary passes through verbatim; null/absent → '--'.
   *  @param {string|null} state
   *  @returns {string} */
  function afkStateLabel(state) {
    if (state == null || state === '') return '--';
    return String(state);
  }

  /** Map an AFK automation state value to a status-badge CSS class.  Extends
   *  the app.js afkRunStatusBadgeClass mapping with the provisioning state
   *  `pending` (rendered as an intentional wait).  Unknown → badge-unknown.
   *  @param {string|null} state
   *  @returns {string} badge class */
  function afkStateBadgeClass(state) {
    if (state === 'running') return 'badge-running';
    if (state === 'completed') return 'badge-completed';
    if (state === 'failed') return 'badge-failed';
    if (state === 'cancelled') return 'badge-cancelled';
    if (state === 'stale' || state === 'timed_out') return 'badge-stale';
    if (state === 'blocked' || state === 'pending') return 'badge-blocked';
    return 'badge-unknown';
  }

  // ── Cost adapters ──────────────────────────────────────────────────────
  // Cost is the sum of available estimated USD usage associated with linked
  // execution sessions.  Missing cost telemetry is represented as
  // UNAVAILABLE, never as zero — a known $0.00 is a real observed zero.

  /** Resolve the total estimated USD cost from a change-request contract.
   *  Accepts the documented vocabulary (`total_estimated_cost_usd` /
   *  `estimated_cost_usd` / `cost_usd`).  Returns a number, or null when the
   *  cost is missing/unparseable (unavailable — NOT zero).
   *  @param {Object|null} cr
   *  @returns {number|null} */
  function crCostUsd(cr) {
    cr = cr || {};
    var v = cr.total_estimated_cost_usd != null ? cr.total_estimated_cost_usd
      : (cr.estimated_cost_usd != null ? cr.estimated_cost_usd
      : (cr.cost_usd != null ? cr.cost_usd : null));
    if (v == null || v === '' || isNaN(Number(v))) return null;
    return Number(v);
  }

  /** Whether a change-request contract carries a known USD cost.
   *  @param {Object|null} cr
   *  @returns {boolean} */
  function crCostAvailable(cr) {
    return crCostUsd(cr) !== null;
  }

  /** Format a cost value as a USD amount, distinguishing a known amount from
   *  missing telemetry.  A known amount renders as `$1.50` (2 decimals, 4 for
   *  sub-cent values — the app.js fmtCost convention); missing/null/unparseable
   *  renders the canonical 'Cost unavailable' label — never `$0.00`.
   *  @param {*} value - number, numeric string, or null/undefined
   *  @returns {string} e.g. "$1.50" | "Cost unavailable" */
  function fmtCrCost(value) {
    if (value == null || value === '' || isNaN(Number(value))) return COST_UNAVAILABLE;
    var num = Number(value);
    return '$' + num.toFixed(num < 0.01 ? 4 : 2);
  }

  // ── Timestamp adapters ─────────────────────────────────────────────────

  /** Resolve the latest linked activity timestamp from a change-request
   *  contract.  The Gateway owns the freshness ordering; the adapter only
   *  preserves the observed value (never derives a clock-dependent age).
   *  @param {Object|null} cr
   *  @returns {string|null} ISO timestamp or null */
  function crLatestActivityAt(cr) {
    cr = cr || {};
    return cr.latest_activity_at || cr.last_activity_at || cr.last_seen_at || cr.updated_at || null;
  }

  /** Format a timestamp as a compact absolute local datetime ("Aug 1, 9:41
   *  AM" style).  Missing/unparseable → '--' (the fmtDT fallback convention).
   *  Pure — no DOM access.
   *  @param {*} isoStr - ISO 8601 timestamp string
   *  @returns {string} */
  function fmtCrTimestamp(isoStr) {
    if (!isoStr) return '--';
    var d = new Date(isoStr);
    if (isNaN(d.getTime())) return '--';
    return d.toLocaleString('en-US', {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit'
    });
  }

  /** Format a duration between two ISO timestamps.  Missing inputs or a
   *  negative (reversed) duration → '--' (the fmtDuration fallback).
   *  @param {string|null} start
   *  @param {string|null} end
   *  @returns {string} e.g. "1h 30m" | "5m" | "--" */
  function fmtCrDuration(start, end) {
    if (!start || !end) return '--';
    var ms = new Date(end) - new Date(start);
    if (isNaN(ms) || ms < 0) return '--';
    var mins = Math.floor(ms / 60000);
    var hrs = Math.floor(mins / 60);
    var days = Math.floor(hrs / 24);
    if (days > 0) return days + 'd ' + (hrs % 24) + 'h';
    if (hrs > 0) return hrs + 'h ' + (mins % 60) + 'm';
    return mins + 'm';
  }

  // ── Execution purpose + status formatters ──────────────────────────────
  // Execution purpose distinguishes what an AWX execution was FOR
  // (implementation / review / retry) and is provided by the Gateway-owned
  // detail contract "when available" (#611).  The adapter normalizes the
  // vocabulary and NEVER guesses a purpose from timestamps or cross-payload
  // relationships.  As a defensive fallback only, an execution's `phase`
  // (the provenance-timeline vocabulary: develop/review) maps to the same
  // purpose it already describes within the SAME payload — no browser-side
  // join.  Execution status follows the locked binding vocabulary:
  // pending, running, completed, failed, cancelled, stale.

  /** Normalize an execution purpose value to the canonical vocabulary:
   *  implementation | review | retry.  Unknown/missing → 'unknown'.
   *  @param {*} value - purpose (or phase) from the detail contract
   *  @returns {string} */
  function executionPurpose(value) {
    if (value == null || value === '') return 'unknown';
    var v = String(value).toLowerCase();
    if (v === 'implementation' || v === 'impl' || v === 'develop') return 'implementation';
    if (v === 'review') return 'review';
    if (v === 'retry' || v === 'retry_execution' || v === 'reattempt') return 'retry';
    return v;
  }

  /** Human label for a normalized execution purpose.
   *  @param {string|null} purpose
   *  @returns {string} e.g. "Implementation" | "Review" | "Retry" | "Unknown" */
  function executionPurposeLabel(purpose) {
    var p = executionPurpose(purpose);
    if (p === 'implementation') return 'Implementation';
    if (p === 'review') return 'Review';
    if (p === 'retry') return 'Retry';
    return 'Unknown';
  }

  /** Map a normalized execution purpose to a badge CSS class.
   *  @param {string|null} purpose
   *  @returns {string} badge class */
  function executionPurposeBadgeClass(purpose) {
    var p = executionPurpose(purpose);
    if (p === 'implementation') return 'badge-completed';
    if (p === 'review') return 'badge-open';
    if (p === 'retry') return 'badge-cancelled';
    return 'badge-unknown';
  }

  /** Human label for an execution status value.  The locked vocabulary
   *  passes through verbatim; null/absent → '--'.
   *  @param {string|null} status
   *  @returns {string} */
  function executionStatusLabel(status) {
    if (status == null || status === '') return '--';
    return String(status);
  }

  /** Map an execution status value to a badge CSS class.
   *  @param {string|null} status
   *  @returns {string} badge class */
  function executionStatusBadgeClass(status) {
    if (status === 'running') return 'badge-running';
    if (status === 'completed') return 'badge-completed';
    if (status === 'failed') return 'badge-failed';
    if (status === 'cancelled') return 'badge-cancelled';
    if (status === 'stale' || status === 'timed_out') return 'badge-stale';
    if (status === 'blocked' || status === 'pending') return 'badge-blocked';
    return 'badge-unknown';
  }

  // ── Aggregate data helpers ─────────────────────────────────────────────

  /** Coerce a count-like value to a non-negative integer (0 fallback).
   *  @param {*} v
   *  @returns {number} */
  function toCount(v) {
    var n = Number(v);
    return (v != null && !isNaN(n) && n >= 0) ? n : 0;
  }

  /** Normalize an execution-counts aggregate into the stable shape.  The
   *  Gateway summary contract carries the outcome-state vocabulary —
   *  ``{ total, running, completed, failed, cancelled }`` (issue #610, the
   *  canonical ``executions`` property) — while the detail adapter derives
   *  the purpose vocabulary — ``{ implementation, review, retry, total }``
   *  — from per-execution purposes WITHIN the same payload.  The normalizer
   *  accepts either vocabulary and preserves it verbatim; a plain number is
   *  treated as ``total`` only.  A missing explicit total is derived as the
   *  sum of the present bucket values.
   *  @param {Object|number|null} counts
   *  @returns {{total: number, running: number, completed: number,
   *             failed: number, cancelled: number,
   *             implementation: number, review: number, retry: number}} */
  function normalizeExecutionCounts(counts) {
    var zero = {
      total: 0, running: 0, completed: 0, failed: 0, cancelled: 0,
      implementation: 0, review: 0, retry: 0
    };
    if (counts == null) {
      return zero;
    }
    if (typeof counts === 'number' || typeof counts === 'string') {
      return { total: toCount(counts), running: 0, completed: 0,
        failed: 0, cancelled: 0, implementation: 0, review: 0, retry: 0 };
    }
    // Outcome-state vocabulary (canonical summary `executions`).
    if (counts.running != null || counts.completed != null ||
        counts.failed != null || counts.cancelled != null) {
      var running = toCount(counts.running);
      var completed = toCount(counts.completed);
      var failed = toCount(counts.failed);
      var cancelled = toCount(counts.cancelled);
      var total = toCount(counts.total);
      if (total === 0 && (running + completed + failed + cancelled) > 0) {
        total = running + completed + failed + cancelled;
      }
      return {
        total: total, running: running, completed: completed,
        failed: failed, cancelled: cancelled,
        implementation: 0, review: 0, retry: 0
      };
    }
    // Purpose vocabulary (detail adapter aggregation).
    var impl = toCount(counts.implementation);
    var review = toCount(counts.review);
    var retry = toCount(counts.retry);
    total = toCount(counts.total);
    if (total === 0 && (impl + review + retry) > 0) {
      total = impl + review + retry;
    }
    return {
      total: total, running: 0, completed: 0, failed: 0, cancelled: 0,
      implementation: impl, review: review, retry: retry
    };
  }

  // ── Summary adapter ────────────────────────────────────────────────────
  // One summary contract row -> one stable UI view model.  The Gateway
  // aggregates AWX executions into a single row per
  // (provider, repository, external_id) and carries the outcome-state
  // execution counts (`executions: { total, running, completed, failed,
  // cancelled }`, issue #610); the adapter preserves that aggregation and
  // never creates duplicate top-level rows.

  /** Adapt one change-request summary contract row into a stable UI view
   *  model.  Pure — data composition only, no rendering, no joins.
   *  @param {Object|null} item - one summary row from the API
   *  @returns {Object} stable view model */
  function adaptChangeRequestSummary(item) {
    item = item || {};
    var identity = crIdentity(item);
    var providerState = providerStateValue(item);
    var afkState = afkStateValue(item);
    var costUsd = crCostUsd(item);
    var counts = normalizeExecutionCounts(
      item.executions != null ? item.executions
      : (item.execution_count != null ? item.execution_count : null)
    );
    return {
      identity: identity,
      displayId: crDisplayId(item),
      providerTerm: crProviderTerm(identity.provider),
      title: item.title || '',
      providerState: {
        value: providerState,
        label: providerStateLabel(providerState),
        badgeClass: providerStateBadgeClass(providerState)
      },
      afkAutomationState: {
        value: afkState,
        label: afkStateLabel(afkState),
        badgeClass: afkStateBadgeClass(afkState)
      },
      cost: {
        available: costUsd !== null,
        usd: costUsd,
        label: fmtCrCost(costUsd)
      },
      latestActivityAt: crLatestActivityAt(item),
      executionCounts: counts,
      runCount: toCount(item.run_count)
    };
  }

  /** Adapt a change-request summary list response into stable view models.
   *  Accepts the `{ items: [...] }` envelope or a bare array.  Order is
   *  preserved exactly as the Gateway returned it — ordering policy belongs
   *  to the query layer, never to the browser.
   *  @param {Object|Array|null} data
   *  @returns {Array} stable view models */
  function adaptChangeRequestSummaryList(data) {
    var items = Array.isArray(data) ? data : ((data && data.items) || []);
    if (!Array.isArray(items)) return [];
    return items.map(adaptChangeRequestSummary);
  }

  // ── Detail adapter ─────────────────────────────────────────────────────
  // The provider-scoped detail contract (one change request) carries its
  // linked executions, sessions, usage, costs, statuses, and optional
  // timeline data as ONE composite payload.  The adapter preserves those
  // relationships exactly as the Gateway composed them — it never joins
  // unrelated partial payloads in the browser.

  /** Flatten the Gateway timeline contract into the event array the render
   *  layer iterates.  The backend wraps the events in a
   *  `{ timeline: { events: [...] } }` object (ChangeRequestTimeline, issue
   *  #611); the renderer (renderChangeRequestTimeline) calls
   *  `timeline.forEach(...)` directly, so the adapter surfaces the ARRAY —
   *  never the envelope.  Null/absent timelines and empty event lists all
   *  normalize to `[]` (the renderer's empty state); a legacy bare-array
   *  timeline is passed through untouched.
   *  @param {Object|Array|null} timeline - `{events: [...]}`, a bare array,
   *      or null/undefined
   *  @returns {Array} the event array (empty when the envelope is absent) */
  function timelineEvents(timeline) {
    if (Array.isArray(timeline)) return timeline;
    if (timeline && Array.isArray(timeline.events)) return timeline.events;
    return [];
  }

  /** Adapt one execution binding / provenance execution into a stable view
   *  model.  Accepts both the execution-binding shape (`awx_job.job_id`,
   *  `outcome`) and the provenance-timeline shape (`awx_job_id`, `status`,
   *  `phase`, per-execution token/cost fields).  Duplicate attempts are
   *  preserved — each AWX job stays one execution entry (ADR 0024).
   *  @param {Object|null} execution
   *  @returns {Object} stable execution view model */
  function adaptExecution(execution) {
    execution = execution || {};
    var awx = execution.awx_job || {};
    var awxJobId = execution.awx_job_id != null ? execution.awx_job_id
      : (awx.job_id != null ? awx.job_id : null);
    var purpose = executionPurpose(
      execution.purpose != null ? execution.purpose : execution.phase
    );
    var status = execution.status != null ? execution.status : execution.outcome;
    var costUsd = execution.estimated_cost_usd != null ? execution.estimated_cost_usd
      : execution.cost_usd;
    var startedAt = execution.started_at || null;
    var finishedAt = execution.finished_at || null;
    var inputTokens = execution.total_input_tokens != null ? execution.total_input_tokens
      : execution.input_tokens;
    var outputTokens = execution.total_output_tokens != null ? execution.total_output_tokens
      : execution.output_tokens;
    var cacheReadTokens = execution.total_cache_read_tokens != null ? execution.total_cache_read_tokens
      : execution.cache_read_tokens;
    var cacheWriteTokens = execution.total_cache_write_tokens != null ? execution.total_cache_write_tokens
      : execution.cache_write_tokens;
    var hasTokens = [inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens]
      .some(function (t) { return t != null && t !== '' && !isNaN(Number(t)) && Number(t) > 0; });
    return {
      awxJobId: awxJobId,
      jobTemplateId: awx.job_template_id != null ? awx.job_template_id : execution.job_template_id,
      externalSessionId: execution.external_session_id || null,
      afkRunId: execution.afk_run_id || null,
      triggerType: execution.trigger_type || null,
      purpose: {
        value: purpose,
        label: executionPurposeLabel(purpose),
        badgeClass: executionPurposeBadgeClass(purpose)
      },
      status: {
        value: status,
        label: executionStatusLabel(status),
        badgeClass: executionStatusBadgeClass(status)
      },
      outcome: execution.outcome != null ? String(execution.outcome) : null,
      startedAt: startedAt,
      finishedAt: finishedAt,
      duration: fmtCrDuration(startedAt, finishedAt),
      cost: {
        available: costUsd != null && !isNaN(Number(costUsd)),
        usd: costUsd != null && !isNaN(Number(costUsd)) ? Number(costUsd) : null,
        label: fmtCrCost(costUsd)
      },
      tokens: {
        available: hasTokens,
        inputTokens: inputTokens != null ? Number(inputTokens) : null,
        outputTokens: outputTokens != null ? Number(outputTokens) : null,
        cacheReadTokens: cacheReadTokens != null ? Number(cacheReadTokens) : null,
        cacheWriteTokens: cacheWriteTokens != null ? Number(cacheWriteTokens) : null
      },
      title: execution.title || null,
      branch: execution.branch || null,
      failureReason: execution.failure_reason || null,
      failureSummary: execution.failure_summary || null
    };
  }

  /** Resolve the aggregate cost for a change-request detail contract.  The
   *  Gateway-owned aggregate (`total_estimated_cost_usd` /
   *  `aggregate_cost_usd`) is authoritative; when the detail contract omits
   *  it, the adapter sums the per-execution costs WITHIN THE SAME payload
   *  (no browser-side join).  Missing everywhere → null (unavailable).
   *  @param {Object} detail
   *  @param {Array} executions - adapted execution view models
   *  @returns {number|null} */
  function aggregateCostUsd(detail, executions) {
    var v = detail.total_estimated_cost_usd != null ? detail.total_estimated_cost_usd
      : (detail.aggregate_cost_usd != null ? detail.aggregate_cost_usd
      : (detail.estimated_cost_usd != null ? detail.estimated_cost_usd : null));
    if (v != null && v !== '' && !isNaN(Number(v))) return Number(v);
    var sum = 0;
    var any = false;
    executions.forEach(function (e) {
      if (e.cost.available) {
        sum += e.cost.usd;
        any = true;
      }
    });
    return any ? sum : null;
  }

  /** Resolve the provider merge state from a detail contract.
   *  @param {Object} detail
   *  @param {Object} cr - the change-request block
   *  @returns {string|null} e.g. "merged" | "not_merged" | null */
  function mergeStateValue(detail, cr) {
    var ms = detail.merge_state;
    if (typeof ms === 'string' && ms !== '') return ms;
    if (ms && typeof ms === 'object' && ms.state) return String(ms.state);
    if (cr && cr.merged_at) return 'merged';
    return null;
  }

  /** Adapt a change-request detail contract into a stable UI view model,
   *  preserving linked executions (with duplicate attempts), sessions,
   *  usage, costs, status values, and optional timeline data.
   *  @param {Object|null} detail - provider-scoped detail contract
   *  @returns {Object|null} stable view model (null for empty input) */
  function adaptChangeRequestDetail(detail) {
    if (!detail) return null;
    var cr = detail.change_request || detail.summary || detail;
    var executionsRaw = detail.executions || detail.bindings || [];
    var executions = Array.isArray(executionsRaw) ? executionsRaw.map(adaptExecution) : [];
    var providerState = providerStateValue(cr);
    var afkState = afkStateValue(cr);
    var aggUsd = aggregateCostUsd(detail, executions);
    var purposeCounts = { implementation: 0, review: 0, retry: 0 };
    executions.forEach(function (e) {
      if (e.purpose.value === 'implementation') purposeCounts.implementation++;
      else if (e.purpose.value === 'review') purposeCounts.review++;
      else if (e.purpose.value === 'retry') purposeCounts.retry++;
    });
    var counts = normalizeExecutionCounts({
      implementation: purposeCounts.implementation,
      review: purposeCounts.review,
      retry: purposeCounts.retry,
      total: executions.length
    });
    return {
      identity: crIdentity(cr),
      displayId: crDisplayId(cr),
      providerTerm: crProviderTerm(crIdentity(cr).provider),
      title: cr.title || '',
      providerState: {
        value: providerState,
        label: providerStateLabel(providerState),
        badgeClass: providerStateBadgeClass(providerState)
      },
      afkAutomationState: {
        value: afkState,
        label: afkStateLabel(afkState),
        badgeClass: afkStateBadgeClass(afkState)
      },
      aggregateCost: {
        available: aggUsd !== null,
        usd: aggUsd,
        label: fmtCrCost(aggUsd)
      },
      executions: executions,
      executionCounts: counts,
      sessions: Array.isArray(detail.sessions) ? detail.sessions
        : (Array.isArray(detail.session_links) ? detail.session_links : []),
      usage: detail.usage || null,
      mergeState: mergeStateValue(detail, cr),
      timeline: timelineEvents(detail.timeline != null ? detail.timeline : detail.provenance)
    };
  }

  // ── Export ─────────────────────────────────────────────────────────────

  var api = {
    COST_UNAVAILABLE: COST_UNAVAILABLE,
    // identity
    crIdentity: crIdentity,
    crDisplayId: crDisplayId,
    crProviderTerm: crProviderTerm,
    // provider state
    providerStateValue: providerStateValue,
    providerStateLabel: providerStateLabel,
    providerStateBadgeClass: providerStateBadgeClass,
    // AFK automation state
    afkStateValue: afkStateValue,
    afkStateLabel: afkStateLabel,
    afkStateBadgeClass: afkStateBadgeClass,
    // cost
    crCostUsd: crCostUsd,
    crCostAvailable: crCostAvailable,
    fmtCrCost: fmtCrCost,
    // timestamps
    crLatestActivityAt: crLatestActivityAt,
    fmtCrTimestamp: fmtCrTimestamp,
    fmtCrDuration: fmtCrDuration,
    // execution tokens
    executionTokensAvailable: function (execution) {
      var t = (execution && execution.tokens) || {};
      return !!(t && (t.inputTokens != null || t.outputTokens != null ||
        t.cacheReadTokens != null || t.cacheWriteTokens != null));
    },
    // execution purpose + status
    executionPurpose: executionPurpose,
    executionPurposeLabel: executionPurposeLabel,
    executionPurposeBadgeClass: executionPurposeBadgeClass,
    executionStatusLabel: executionStatusLabel,
    executionStatusBadgeClass: executionStatusBadgeClass,
    // aggregate data
    normalizeExecutionCounts: normalizeExecutionCounts,
    // adapters
    adaptChangeRequestSummary: adaptChangeRequestSummary,
    adaptChangeRequestSummaryList: adaptChangeRequestSummaryList,
    adaptExecution: adaptExecution,
    adaptChangeRequestDetail: adaptChangeRequestDetail,
    timelineEvents: timelineEvents
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
  if (typeof window !== 'undefined') {
    window.ChangeRequestAdapters = api;
  }
})();
