/**
 * GitLab complete lifecycle fixture.
 *
 * Models a full lifecycle equivalent to the GitHub complete fixture:
 * issue opened -> merge request opened -> develop/review executions ->
 * merge -> issue closed. Provider-specific identity: provider=gitlab,
 * repository_url, MR number. Normalized vocabulary: entity_type=change_request.
 */

'use strict';
var shared = require('./shared');

function build() {
  shared.resetSeq();

  var PROVIDER = 'gitlab';
  var REPO = 'opencode/gateway';

  var issues = [
    shared.buildEntityLink({
      entity_type: 'issue', external_id: '115', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0,
      correlation_method: 'issue_reference',
      evidence: [{ kind: 'issue_reference', source_entity_id: 'change_request:118', detail: 'resolves #115' }]
    }),
    shared.buildEntityLink({
      entity_type: 'issue', external_id: '116', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0,
      correlation_method: 'issue_reference',
      evidence: [{ kind: 'issue_reference', source_entity_id: 'change_request:118', detail: 'resolves #116' }]
    })
  ];

  var change_requests = [
    shared.buildEntityLink({
      entity_type: 'change_request', external_id: '118', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0,
      correlation_method: 'issue_reference',
      evidence: [{ kind: 'title_match', source_entity_id: 'change_request:118', detail: 'exact title match' }]
    })
  ];

  var commits = [
    shared.buildEntityLink({
      entity_type: 'commit', external_id: 'f1a2b3c', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0, correlation_method: 'commit_issue_reference',
      owning_change_request_id: '118', correlation_source: 'owning_change_request'
    }),
    shared.buildEntityLink({
      entity_type: 'commit', external_id: 'd4e5f6g', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0, correlation_method: 'commit_issue_reference',
      owning_change_request_id: '118', correlation_source: 'owning_change_request'
    })
  ];

  var reviews = [
    shared.buildEntityLink({
      entity_type: 'review', external_id: '2001', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 0.9, correlation_method: 'temporal_inference',
      owning_change_request_id: '118', correlation_source: 'owning_change_request'
    })
  ];

  var merge_events = [
    shared.buildEntityLink({
      entity_type: 'merge_event', external_id: '118', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0, correlation_method: 'issue_reference'
    })
  ];

  var sessions = [
    shared.buildSessionLink({
      external_session_id: 'ses_01J4T2P0000000000000000020',
      session_id: '3b1c2d3e-0000-4000-8000-000000000020',
      agent: 'code-editor-senior', inferred: true,
      message_count: 38,
      total_input_tokens: 4500, total_output_tokens: 2800,
      total_cache_read_tokens: 800, total_cache_write_tokens: 400,
      total_estimated_cost_usd: 1.10,
      started_at: '2026-08-06T09:00:00Z', finished_at: '2026-08-06T10:05:00Z'
    })
  ];

  var run = shared.buildRun({
    afk_run_id: '01KZDZSDP00000000000000001',
    provider: PROVIDER, status: 'completed', outcome_status: 'merged',
    title: 'Implement issue #115 and #116',
    started_at: '2026-08-06T09:00:00Z', finished_at: '2026-08-06T10:05:00Z'
  });

  var usage = shared.buildUsage({
    input_tokens: 4500, output_tokens: 2800,
    cache_read_tokens: 800, cache_write_tokens: 400,
    total_estimated_cost_usd: 1.10
  });

  var outcome = shared.buildOutcome({
    status: 'merged',
    change_request_ids: ['change_request:118'],
    resolved_issue_ids: ['issue:115', 'issue:116'],
    merge_event_id: 'merge_event:118',
    merged_at: '2026-08-06T10:05:00Z'
  });

  return shared.assembleDetail({
    run: run, issues: issues, sessions: sessions,
    agents: ['code-editor-senior'], usage: usage,
    change_requests: change_requests, commits: commits,
    reviews: reviews, merge_events: merge_events, outcome: outcome
  });
}

module.exports = { build: build, scenario: 'gitlab_complete', provider: 'gitlab' };
