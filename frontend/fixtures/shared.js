/**
 * Shared deterministic fixture helpers for AFK Outcomes frontend tests.
 *
 * Provides entity/session/run/usage builder factories and a stable
 * reference timestamp base. All timestamps are fixed ISO strings;
 * no Date.now() or Math.random() is used.
 *
 * These helpers are consumed by the fixture data files and the
 * fixture-selection tests — they do NOT run in the browser.
 */

'use strict';

// Deterministic timestamp base — all fixture timestamps derive from this.
var TS_BASE = '2026-08-13T08:00:00Z';

// Synthetic sequence counter — deterministic, no real provider IDs.
var _idSeq = 0;

function resetSeq() { _idSeq = 0; }
function nextSeq() { return ++_idSeq; }

/** Build an entity link object matching the AFK chain detail shape. */
function buildEntityLink(opts) {
  return {
    entity_id: opts.entity_type + ':' + opts.external_id,
    entity_type: opts.entity_type,
    external_id: String(opts.external_id),
    provider: opts.provider,
    repository: opts.repository,
    role: opts.role || 'resolved',
    correlation_method: opts.correlation_method || 'issue_reference',
    correlation_confidence: opts.correlation_confidence != null ? opts.correlation_confidence : 1.0,
    evidence: opts.evidence || [],
    resolver_version: opts.resolver_version || '2',
    provisional: opts.provisional || false,
    owning_change_request_id: opts.owning_change_request_id || null,
    correlation_source: opts.correlation_source || 'direct'
  };
}

/** Build a session link object matching the AFK chain detail shape. */
function buildSessionLink(opts) {
  return {
    external_session_id: opts.external_session_id || 'ses_fixture_' + nextSeq(),
    session_id: opts.session_id || null,
    agent: opts.agent || 'code-editor-mid',
    inferred: opts.inferred !== false,
    message_count: opts.message_count || 0,
    total_input_tokens: opts.total_input_tokens || 0,
    total_output_tokens: opts.total_output_tokens || 0,
    total_cache_read_tokens: opts.total_cache_read_tokens || 0,
    total_cache_write_tokens: opts.total_cache_write_tokens || 0,
    total_estimated_cost_usd: opts.total_estimated_cost_usd || 0,
    started_at: opts.started_at || TS_BASE,
    finished_at: opts.finished_at || null,
    parent_session_id: opts.parent_session_id || null
  };
}

/** Build a run aggregate object. */
function buildRun(opts) {
  return {
    afk_run_id: opts.afk_run_id || '01KZX_' + String(nextSeq()).padStart(20, '0'),
    provider: opts.provider,
    status: opts.status || 'completed',
    title: opts.title || 'Fixture run',
    started_at: opts.started_at || TS_BASE,
    finished_at: opts.finished_at || null,
    outcome_status: opts.outcome_status || null
  };
}

/** Build a usage aggregate object. */
function buildUsage(opts) {
  var input = opts.input_tokens || 0;
  var output = opts.output_tokens || 0;
  return {
    active_tokens: opts.active_tokens || (input + output),
    input_tokens: input,
    output_tokens: output,
    cache_read_tokens: opts.cache_read_tokens || 0,
    cache_write_tokens: opts.cache_write_tokens || 0,
    total_estimated_cost_usd: opts.total_estimated_cost_usd || 0
  };
}

/** Build an outcome object. */
function buildOutcome(opts) {
  return {
    status: opts.status || 'open',
    change_request_ids: opts.change_request_ids || [],
    resolved_issue_ids: opts.resolved_issue_ids || [],
    merge_event_id: opts.merge_event_id || null,
    merged_at: opts.merged_at || null
  };
}

/** Assemble a complete detail object from components. */
function assembleDetail(components) {
  return {
    run: components.run,
    issues: components.issues || [],
    sessions: components.sessions || [],
    agents: components.agents || [],
    usage: components.usage || buildUsage({}),
    change_requests: components.change_requests || [],
    commits: components.commits || [],
    reviews: components.reviews || [],
    merge_events: components.merge_events || [],
    outcome: components.outcome || buildOutcome({ status: 'open' })
  };
}

module.exports = {
  TS_BASE: TS_BASE,
  resetSeq: resetSeq,
  nextSeq: nextSeq,
  buildEntityLink: buildEntityLink,
  buildSessionLink: buildSessionLink,
  buildRun: buildRun,
  buildUsage: buildUsage,
  buildOutcome: buildOutcome,
  assembleDetail: assembleDetail
};
