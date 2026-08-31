/**
 * Change-request detail fixture for GitHub (issue #614).
 *
 * Deterministic: fixed identity tuples and timestamps — no Date.now(),
 * no Math.random(), no network access, no provider credentials.  Follows
 * the #611 composite detail contract consumed by the #612 detail adapter.
 *
 * Covers the execution-focused detail experience:
 *   - Implementation executions (one failed attempt retained as history,
 *     then a successful retry with purpose=retry)
 *   - Review executions
 *   - Per-execution token usage + per-run cost (some executions with NO
 *     cost telemetry → 'Cost unavailable')
 *   - AWX job metadata (job template, trigger type, branch)
 *   - Linked sessions with internal session ids (Agent Run drill-down)
 *   - Aggregate cost (Gateway-owned total_estimated_cost_usd)
 *   - Collapsed provenance timeline with observed-via provenance
 */

'use strict';

var TS = {
  PR_142_OPEN: '2026-08-17T08:05:00Z',
  PR_142_MERGE: '2026-08-17T10:35:00Z'
};

/** Build the GitHub change-request detail payload for acme/web-app#142:
 *  implementation + review + retry executions, linked sessions, per-run
 *  token/cost telemetry, aggregate cost, merge state, and timeline. */
function buildDetail() {
  return {
    change_request: {
      provider: 'github', repository: 'acme/web-app', external_id: '142',
      resource_type: 'change_request', title: 'feat: wire up web-app dashboard',
      provider_state: 'merged', automation_state: 'completed',
      merged_at: TS.PR_142_MERGE,
      provider_state_observed_at: TS.PR_142_MERGE
    },
    merge_state: { state: 'merged', merged_at: TS.PR_142_MERGE },
    total_estimated_cost_usd: 6.35,
    executions: [
      {
        awx_job: { job_id: 'awx-9001', job_template_id: 'tpl-implement' },
        purpose: 'implementation', status: 'completed', outcome: 'completed',
        trigger_type: 'eda', branch: 'feat/web-app-dashboard',
        external_session_id: 'ses_github_142_1',
        started_at: '2026-08-17T09:00:00Z', finished_at: '2026-08-17T09:40:00Z',
        total_input_tokens: 8000, total_output_tokens: 4000,
        total_cache_read_tokens: 1200, total_cache_write_tokens: 600,
        estimated_cost_usd: 2.10
      },
      {
        awx_job_id: 'awx-9002',
        purpose: 'implementation', status: 'failed', outcome: 'failed',
        trigger_type: 'eda', branch: 'feat/web-app-dashboard',
        external_session_id: 'ses_github_142_2',
        started_at: '2026-08-17T09:45:00Z', finished_at: '2026-08-17T09:55:00Z',
        total_input_tokens: 3000, total_output_tokens: 1200,
        estimated_cost_usd: 0.85,
        failure_summary: 'AWX runner lost connectivity'
      },
      {
        awx_job_id: 'awx-9004',
        purpose: 'retry', status: 'completed', outcome: 'completed',
        trigger_type: 'manual', branch: 'feat/web-app-dashboard',
        external_session_id: 'ses_github_142_4',
        started_at: '2026-08-17T10:00:00Z', finished_at: '2026-08-17T10:15:00Z',
        total_input_tokens: 2500, total_output_tokens: 900,
        total_cache_read_tokens: 400,
        estimated_cost_usd: 0.75
      },
      {
        awx_job_id: 'awx-9003',
        purpose: 'review', status: 'completed', outcome: 'completed',
        trigger_type: 'eda', branch: 'feat/web-app-dashboard',
        external_session_id: 'ses_github_142_3',
        started_at: '2026-08-17T10:00:00Z', finished_at: '2026-08-17T10:30:00Z',
        total_input_tokens: 6000, total_output_tokens: 2500,
        total_cache_read_tokens: 1500, total_cache_write_tokens: 300,
        estimated_cost_usd: 1.90
      },
      {
        awx_job_id: 'awx-9005',
        purpose: 'review', status: 'cancelled', outcome: 'cancelled',
        external_session_id: 'ses_github_142_5',
        started_at: '2026-08-17T10:32:00Z', finished_at: '2026-08-17T10:33:00Z',
        failure_summary: 'Review superseded by re-run'
      }
    ],
    sessions: [
      {
        external_session_id: 'ses_github_142_1',
        session_id: '2e0f1a2b-0000-4000-8000-000000000142',
        agent: 'code-editor-senior', inferred: false,
        message_count: 40,
        total_input_tokens: 8000, total_output_tokens: 4000,
        total_cache_read_tokens: 1200, total_cache_write_tokens: 600,
        total_estimated_cost_usd: 2.10,
        started_at: '2026-08-17T09:00:00Z', finished_at: '2026-08-17T09:40:00Z'
      },
      {
        external_session_id: 'ses_github_142_3',
        session_id: '2e0f1a2b-0000-4000-8000-000000000143',
        agent: 'code-editor-mid', inferred: false,
        message_count: 22,
        total_input_tokens: 6000, total_output_tokens: 2500,
        total_cache_read_tokens: 1500, total_cache_write_tokens: 300,
        total_estimated_cost_usd: 1.90,
        started_at: '2026-08-17T10:00:00Z', finished_at: '2026-08-17T10:30:00Z'
      },
      {
        external_session_id: 'ses_github_142_2',
        session_id: '2e0f1a2b-0000-4000-8000-000000000144',
        agent: 'code-editor-senior', inferred: true,
        message_count: 9,
        total_input_tokens: 3000, total_output_tokens: 1200,
        total_estimated_cost_usd: 0.85,
        started_at: '2026-08-17T09:45:00Z', finished_at: '2026-08-17T09:55:00Z'
      }
    ],
    timeline: [
      { event_type: 'change_request.opened', occurred_at: TS.PR_142_OPEN, observed_via: 'webhook', actor: 'alice', summary: 'PR opened' },
      { event_type: 'change_request.updated', occurred_at: '2026-08-17T09:00:00Z', observed_via: 'webhook', actor: 'alice', summary: 'Commit pushed' },
      { event_type: 'change_request.merged', occurred_at: TS.PR_142_MERGE, observed_via: 'webhook', actor: 'bob', summary: 'PR merged' }
    ]
  };
}

module.exports = {
  buildDetail: buildDetail,
  provider: 'github',
  scenario: 'detail-github'
};
