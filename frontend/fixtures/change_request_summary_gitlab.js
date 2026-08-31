/**
 * Change-request summary + detail fixtures for GitLab (issue #613).
 *
 * Deterministic and SEMANTICALLY EQUIVALENT to the GitHub fixture
 * (change_request_summary_github.js) so provider-parity tests can assert
 * identical lifecycle semantics under both providers: a merged+completed MR
 * with known cost, and an open+failed MR with NO cost telemetry.  Only the
 * provider identity and provider-specific MR numbers differ.
 */

'use strict';

var TS = {
  MR_6_OPEN: '2026-08-17T08:05:00Z',
  MR_6_MERGE: '2026-08-17T10:35:00Z',
  MR_4_OPEN: '2026-08-16T09:00:00Z'
};

/** Build the GitLab change-request summary list (two rows: merged+completed
 *  with cost, open+failed with NO cost telemetry — mirroring the GitHub
 *  fixture's first and third rows for parity). */
function buildSummaryList() {
  return {
    items: [
      {
        provider: 'gitlab', repository: 'group/cloudnative', external_id: '6',
        resource_type: 'change_request',
        provider_state: 'merged', automation_state: 'completed',
        total_estimated_cost_usd: 3.40,
        latest_linked_activity: TS.MR_6_MERGE,
        executions: { total: 2, running: 0, completed: 2, failed: 0, cancelled: 0 }
      },
      {
        provider: 'gitlab', repository: 'group/cloudnative', external_id: '4',
        resource_type: 'change_request',
        provider_state: 'open', automation_state: 'failed',
        total_estimated_cost_usd: null,
        latest_linked_activity: TS.MR_4_OPEN,
        executions: { total: 2, running: 0, completed: 0, failed: 2, cancelled: 0 }
      }
    ],
    total: 2,
    limit: 100,
    offset: 0
  };
}

/** Build the GitLab change-request detail payload for group/cloudnative!6:
 *  implementation + review executions, a linked session, aggregate cost,
 *  merge state, and timeline. */
function buildDetail() {
  return {
    change_request: {
      provider: 'gitlab', repository: 'group/cloudnative', external_id: '6',
      resource_type: 'change_request', title: 'feat: operator upgrade path',
      provider_state: 'merged', automation_state: 'completed'
    },
    merge_state: 'merged',
    total_estimated_cost_usd: 3.40,
    executions: [
      {
        awx_job: { job_id: 'awx-9101', job_template_id: 'tpl-implement' },
        purpose: 'implementation', status: 'completed', outcome: 'completed',
        external_session_id: 'ses_gitlab_6_1',
        started_at: '2026-08-17T09:00:00Z', finished_at: '2026-08-17T09:40:00Z',
        estimated_cost_usd: 2.15
      },
      {
        awx_job_id: 'awx-9102',
        purpose: 'review', status: 'completed', outcome: 'completed',
        external_session_id: 'ses_gitlab_6_2',
        started_at: '2026-08-17T10:00:00Z', finished_at: '2026-08-17T10:30:00Z',
        estimated_cost_usd: 1.25
      }
    ],
    sessions: [
      {
        external_session_id: 'ses_gitlab_6_1',
        session_id: '2e0f1a2b-0000-4000-8000-000000000006',
        agent: 'code-editor-mid', inferred: false,
        message_count: 32,
        total_input_tokens: 6000, total_output_tokens: 3000,
        total_cache_read_tokens: 900, total_cache_write_tokens: 400,
        total_estimated_cost_usd: 2.15,
        started_at: '2026-08-17T09:00:00Z', finished_at: '2026-08-17T09:40:00Z'
      }
    ],
    timeline: [
      { event_type: 'change_request.opened', occurred_at: TS.MR_6_OPEN, summary: 'MR opened' },
      { event_type: 'change_request.merged', occurred_at: TS.MR_6_MERGE, summary: 'MR merged' }
    ]
  };
}

module.exports = {
  buildSummaryList: buildSummaryList,
  buildDetail: buildDetail,
  provider: 'gitlab',
  scenario: 'summary-gitlab'
};
