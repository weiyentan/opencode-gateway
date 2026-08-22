/**
 * GitLab repeated develop/review cycles fixture.
 *
 * Mirrors the GitHub cycles fixture structure for cross-provider parity:
 * two sessions representing two distinct development iterations on the
 * same merge request.
 */

'use strict';
var shared = require('./shared');

function build() {
  shared.resetSeq();

  var PROVIDER = 'gitlab';
  var REPO = 'opencode/gateway';

  var issues = [
    shared.buildEntityLink({
      entity_type: 'issue', external_id: '301', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0,
      correlation_method: 'issue_reference',
      evidence: [{ kind: 'issue_reference', source_entity_id: 'change_request:305', detail: 'resolves #301' }]
    })
  ];

  var change_requests = [
    shared.buildEntityLink({
      entity_type: 'change_request', external_id: '305', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0,
      correlation_method: 'issue_reference',
      evidence: [{ kind: 'title_match', source_entity_id: 'change_request:305', detail: 'exact title match' }]
    })
  ];

  var commits = [
    shared.buildEntityLink({
      entity_type: 'commit', external_id: 'd000001', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0, correlation_method: 'commit_issue_reference',
      owning_change_request_id: '305', correlation_source: 'owning_change_request'
    }),
    shared.buildEntityLink({
      entity_type: 'commit', external_id: 'd000002', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0, correlation_method: 'commit_issue_reference',
      owning_change_request_id: '305', correlation_source: 'owning_change_request'
    }),
    shared.buildEntityLink({
      entity_type: 'commit', external_id: 'd000003', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0, correlation_method: 'commit_issue_reference',
      owning_change_request_id: '305', correlation_source: 'owning_change_request'
    })
  ];

  var reviews = [
    shared.buildEntityLink({
      entity_type: 'review', external_id: '4001', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 0.9, correlation_method: 'temporal_inference',
      owning_change_request_id: '305', correlation_source: 'owning_change_request'
    }),
    shared.buildEntityLink({
      entity_type: 'review', external_id: '4002', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 0.9, correlation_method: 'temporal_inference',
      owning_change_request_id: '305', correlation_source: 'owning_change_request'
    })
  ];

  var sessions = [
    shared.buildSessionLink({
      external_session_id: 'ses_01J4T2P0000000000000000050',
      session_id: '7f5a6b7c-0000-4000-8000-000000000050',
      agent: 'code-editor-senior', inferred: true,
      message_count: 22,
      total_input_tokens: 2800, total_output_tokens: 1800,
      total_cache_read_tokens: 450, total_cache_write_tokens: 180,
      total_estimated_cost_usd: 0.72,
      started_at: '2026-08-08T09:00:00Z', finished_at: '2026-08-08T09:40:00Z'
    }),
    shared.buildSessionLink({
      external_session_id: 'ses_01J4T2P0000000000000000051',
      session_id: '8a6b7c8d-0000-4000-8000-000000000051',
      agent: 'code-editor-senior', inferred: true,
      message_count: 28,
      total_input_tokens: 3500, total_output_tokens: 2200,
      total_cache_read_tokens: 550, total_cache_write_tokens: 250,
      total_estimated_cost_usd: 0.95,
      started_at: '2026-08-08T10:00:00Z', finished_at: '2026-08-08T10:45:00Z'
    })
  ];

  var run = shared.buildRun({
    afk_run_id: '01KZDZSDP00000000000000020',
    provider: PROVIDER, status: 'completed', outcome_status: 'merged',
    title: 'Implement issue #301 with review cycles',
    started_at: '2026-08-08T09:00:00Z', finished_at: '2026-08-08T10:45:00Z'
  });

  var usage = shared.buildUsage({
    input_tokens: 6300, output_tokens: 4000,
    cache_read_tokens: 1000, cache_write_tokens: 430,
    total_estimated_cost_usd: 1.67
  });

  var outcome = shared.buildOutcome({
    status: 'merged',
    change_request_ids: ['change_request:305'],
    resolved_issue_ids: ['issue:301'],
    merge_event_id: 'merge_event:305',
    merged_at: '2026-08-08T10:50:00Z'
  });

  return shared.assembleDetail({
    run: run, issues: issues, sessions: sessions,
    agents: ['code-editor-senior'], usage: usage,
    change_requests: change_requests, commits: commits,
    reviews: reviews, merge_events: [], outcome: outcome
  });
}

module.exports = { build: build, scenario: 'gitlab_cycles', provider: 'gitlab' };
