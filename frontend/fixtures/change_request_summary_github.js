/**
 * Change-request summary + detail fixtures for GitHub (issue #613).
 *
 * Deterministic: fixed identity tuples and timestamps — no Date.now(),
 * no Math.random(), no network access, no provider credentials.  The
 * summary list follows the #610 summary contract exactly (one row per
 * provider/repository/external-id with provider_state, automation_state,
 * total_estimated_cost_usd, latest_linked_activity, and execution counts,
 * ordered newest-linked-activity first as the query layer delivers it).
 * The detail payload follows the planned #611 composite shape consumed by
 * the #612 detail adapter.
 */

'use strict';

var TS = {
  PR_142_OPEN: '2026-08-17T08:05:00Z',
  PR_142_MERGE: '2026-08-17T10:35:00Z',
  PR_138_OPEN: '2026-08-16T09:00:00Z',
  PR_7_OPEN: '2026-08-15T14:00:00Z'
};

/** Build the GitHub change-request summary list (three rows: merged+completed
 *  with cost, open+running with cost, closed+failed with NO cost telemetry). */
function buildSummaryList() {
  return {
    items: [
      {
        provider: 'github', repository: 'acme/web-app', external_id: '142',
        resource_type: 'change_request',
        provider_state: 'merged', automation_state: 'completed',
        total_estimated_cost_usd: 4.85,
        latest_linked_activity: TS.PR_142_MERGE,
        provider_state_observed_at: TS.PR_142_MERGE,
        executions: { total: 3, running: 0, completed: 2, failed: 1, cancelled: 0 }
      },
      {
        provider: 'github', repository: 'acme/web-app', external_id: '138',
        resource_type: 'change_request',
        provider_state: 'open', automation_state: 'running',
        total_estimated_cost_usd: 1.25,
        latest_linked_activity: TS.PR_138_OPEN,
        provider_state_observed_at: TS.PR_138_OPEN,
        executions: { total: 1, running: 1, completed: 0, failed: 0, cancelled: 0 }
      },
      {
        provider: 'github', repository: 'acme/tooling', external_id: '7',
        resource_type: 'change_request',
        provider_state: 'closed', automation_state: 'failed',
        total_estimated_cost_usd: null,
        latest_linked_activity: null,
        provider_state_observed_at: null,
        executions: { total: 2, running: 0, completed: 0, failed: 2, cancelled: 0 }
      }
    ],
    total: 3,
    limit: 100,
    offset: 0
  };
}

/** Build the GitHub change-request detail payload for acme/web-app#142:
 *  implementation + review executions (one failed attempt retained as
 *  history), a linked session, aggregate cost, merge state, and timeline. */
function buildDetail() {
  return {
    change_request: {
      provider: 'github', repository: 'acme/web-app', external_id: '142',
      resource_type: 'change_request', title: 'feat: wire up web-app dashboard',
      provider_state: 'merged', automation_state: 'completed'
    },
    merge_state: 'merged',
    total_estimated_cost_usd: 4.85,
    executions: [
      {
        awx_job: { job_id: 'awx-9001', job_template_id: 'tpl-implement' },
        purpose: 'implementation', status: 'completed', outcome: 'completed',
        external_session_id: 'ses_github_142_1',
        started_at: '2026-08-17T09:00:00Z', finished_at: '2026-08-17T09:40:00Z',
        estimated_cost_usd: 2.10
      },
      {
        awx_job_id: 'awx-9002',
        purpose: 'implementation', status: 'failed', outcome: 'failed',
        external_session_id: 'ses_github_142_2',
        started_at: '2026-08-17T09:45:00Z', finished_at: '2026-08-17T09:55:00Z',
        estimated_cost_usd: 0.85,
        failure_summary: 'AWX runner lost connectivity'
      },
      {
        awx_job_id: 'awx-9003',
        purpose: 'review', status: 'completed', outcome: 'completed',
        external_session_id: 'ses_github_142_3',
        started_at: '2026-08-17T10:00:00Z', finished_at: '2026-08-17T10:30:00Z',
        estimated_cost_usd: 1.90
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
      }
    ],
    timeline: {
      events: [
        { event_type: 'change_request.opened', occurred_at: TS.PR_142_OPEN, summary: 'PR opened' },
        { event_type: 'change_request.merged', occurred_at: TS.PR_142_MERGE, summary: 'PR merged' }
      ]
    }
  };
}

module.exports = {
  buildSummaryList: buildSummaryList,
  buildDetail: buildDetail,
  provider: 'github',
  scenario: 'summary-github'
};
