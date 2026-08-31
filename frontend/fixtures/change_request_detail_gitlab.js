/**
 * Change-request detail fixture for GitLab (issue #614).
 *
 * Deterministic and SEMANTICALLY EQUIVALENT to the GitHub detail fixture
 * (change_request_detail_github.js) so provider-parity tests can assert
 * identical lifecycle semantics under both providers.  Only the provider
 * identity and provider-specific MR numbers differ.
 */

'use strict';

var TS = {
  MR_6_OPEN: '2026-08-17T08:05:00Z',
  MR_6_MERGE: '2026-08-17T10:35:00Z'
};

/** Build the GitLab change-request detail payload for group/cloudnative!6:
 *  implementation + review + retry executions, linked sessions, per-run
 *  token/cost telemetry, aggregate cost, merge state, and timeline. */
function buildDetail() {
  return {
    change_request: {
      provider: 'gitlab', repository: 'group/cloudnative', external_id: '6',
      resource_type: 'change_request', title: 'feat: operator upgrade path',
      provider_state: 'merged', automation_state: 'completed',
      merged_at: TS.MR_6_MERGE,
      provider_state_observed_at: TS.MR_6_MERGE
    },
    merge_state: { state: 'merged', merged_at: TS.MR_6_MERGE },
    total_estimated_cost_usd: 4.80,
    executions: [
      {
        awx_job: { job_id: 'awx-9101', job_template_id: 'tpl-implement' },
        purpose: 'implementation', status: 'completed', outcome: 'completed',
        trigger_type: 'eda', branch: 'feat/operator-upgrade',
        external_session_id: 'ses_gitlab_6_1',
        started_at: '2026-08-17T09:00:00Z', finished_at: '2026-08-17T09:40:00Z',
        total_input_tokens: 7000, total_output_tokens: 3500,
        total_cache_read_tokens: 1000, total_cache_write_tokens: 500,
        estimated_cost_usd: 2.15
      },
      {
        awx_job_id: 'awx-9104',
        purpose: 'retry', status: 'completed', outcome: 'completed',
        trigger_type: 'manual', branch: 'feat/operator-upgrade',
        external_session_id: 'ses_gitlab_6_4',
        started_at: '2026-08-17T10:00:00Z', finished_at: '2026-08-17T10:15:00Z',
        total_input_tokens: 2200, total_output_tokens: 800,
        total_cache_read_tokens: 300,
        estimated_cost_usd: 0.70
      },
      {
        awx_job_id: 'awx-9102',
        purpose: 'review', status: 'completed', outcome: 'completed',
        trigger_type: 'eda', branch: 'feat/operator-upgrade',
        external_session_id: 'ses_gitlab_6_2',
        started_at: '2026-08-17T10:00:00Z', finished_at: '2026-08-17T10:30:00Z',
        total_input_tokens: 5500, total_output_tokens: 2000,
        total_cache_read_tokens: 1300, total_cache_write_tokens: 200,
        estimated_cost_usd: 1.25
      },
      {
        awx_job_id: 'awx-9105',
        purpose: 'review', status: 'cancelled', outcome: 'cancelled',
        external_session_id: 'ses_gitlab_6_5',
        started_at: '2026-08-17T10:32:00Z', finished_at: '2026-08-17T10:33:00Z',
        failure_summary: 'Review superseded by re-run'
      }
    ],
    sessions: [
      {
        external_session_id: 'ses_gitlab_6_1',
        session_id: '2e0f1a2b-0000-4000-8000-000000000006',
        agent: 'code-editor-mid', inferred: false,
        message_count: 32,
        total_input_tokens: 7000, total_output_tokens: 3500,
        total_cache_read_tokens: 1000, total_cache_write_tokens: 500,
        total_estimated_cost_usd: 2.15,
        started_at: '2026-08-17T09:00:00Z', finished_at: '2026-08-17T09:40:00Z'
      },
      {
        external_session_id: 'ses_gitlab_6_2',
        session_id: '2e0f1a2b-0000-4000-8000-000000000007',
        agent: 'code-editor-junior', inferred: false,
        message_count: 18,
        total_input_tokens: 5500, total_output_tokens: 2000,
        total_cache_read_tokens: 1300, total_cache_write_tokens: 200,
        total_estimated_cost_usd: 1.25,
        started_at: '2026-08-17T10:00:00Z', finished_at: '2026-08-17T10:30:00Z'
      }
    ],
    timeline: [
      { event_type: 'change_request.opened', occurred_at: TS.MR_6_OPEN, observed_via: 'webhook', actor: 'carol', summary: 'MR opened' },
      { event_type: 'change_request.updated', occurred_at: '2026-08-17T09:00:00Z', observed_via: 'webhook', actor: 'carol', summary: 'Commit pushed' },
      { event_type: 'change_request.merged', occurred_at: TS.MR_6_MERGE, observed_via: 'webhook', actor: 'dave', summary: 'MR merged' }
    ]
  };
}

module.exports = {
  buildDetail: buildDetail,
  provider: 'gitlab',
  scenario: 'detail-gitlab'
};
