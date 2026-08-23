/**
 * GitHub complete lifecycle fixture.
 *
 * Models a full lifecycle: issue opened -> change request opened ->
 * develop/review executions -> merge -> issue closed.
 * Provider-specific identity: provider=github, repository_url, PR number.
 * Normalized vocabulary: entity_type=change_request (not "pull_request").
 */

'use strict';
var shared = require('./shared');

function build() {
  shared.resetSeq();

  var PROVIDER = 'github';
  var REPO = 'weiyentan/opencode-gateway';

  var issues = [
    shared.buildEntityLink({
      entity_type: 'issue', external_id: '437', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0,
      correlation_method: 'issue_reference',
      evidence: [{ kind: 'issue_reference', source_entity_id: 'change_request:442', detail: 'resolves #437' }]
    }),
    shared.buildEntityLink({
      entity_type: 'issue', external_id: '438', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0,
      correlation_method: 'issue_reference',
      evidence: [{ kind: 'issue_reference', source_entity_id: 'change_request:442', detail: 'resolves #438' }]
    })
  ];

  var change_requests = [
    shared.buildEntityLink({
      entity_type: 'change_request', external_id: '442', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0,
      correlation_method: 'issue_reference',
      evidence: [{ kind: 'title_match', source_entity_id: 'change_request:442', detail: 'exact title match' }]
    })
  ];

  var commits = [
    shared.buildEntityLink({
      entity_type: 'commit', external_id: 'a1b2c3d', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0, correlation_method: 'commit_issue_reference',
      owning_change_request_id: '442', correlation_source: 'owning_change_request'
    }),
    shared.buildEntityLink({
      entity_type: 'commit', external_id: 'e4f5g6h', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0, correlation_method: 'commit_issue_reference',
      owning_change_request_id: '442', correlation_source: 'owning_change_request'
    })
  ];

  var reviews = [
    shared.buildEntityLink({
      entity_type: 'review', external_id: '1001', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 0.9, correlation_method: 'temporal_inference',
      owning_change_request_id: '442', correlation_source: 'owning_change_request'
    })
  ];

  var merge_events = [
    shared.buildEntityLink({
      entity_type: 'merge_event', external_id: '442', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0, correlation_method: 'issue_reference'
    })
  ];

  var sessions = [
    shared.buildSessionLink({
      external_session_id: 'ses_01J4T2P0000000000000000001',
      session_id: '1f9c3a6e-0000-4000-8000-000000000001',
      agent: 'code-editor-senior', inferred: true,
      message_count: 42,
      total_input_tokens: 5000, total_output_tokens: 3000,
      total_cache_read_tokens: 1000, total_cache_write_tokens: 500,
      total_estimated_cost_usd: 1.2345,
      started_at: '2026-08-13T09:00:00Z', finished_at: '2026-08-13T10:10:29Z'
    })
  ];

  var run = shared.buildRun({
    afk_run_id: '01KZX9M4G80000000000000001',
    provider: PROVIDER, status: 'completed', outcome_status: 'merged',
    title: 'Implement issue #437 and #438',
    started_at: '2026-08-13T09:00:00Z', finished_at: '2026-08-13T10:10:29Z'
  });

  var usage = shared.buildUsage({
    input_tokens: 5000, output_tokens: 3000,
    cache_read_tokens: 1000, cache_write_tokens: 500,
    total_estimated_cost_usd: 1.2345
  });

  var outcome = shared.buildOutcome({
    status: 'merged',
    change_request_ids: ['change_request:442'],
    resolved_issue_ids: ['issue:437', 'issue:438'],
    merge_event_id: 'merge_event:442',
    merged_at: '2026-08-13T10:10:29Z'
  });

  return shared.assembleDetail({
    run: run, issues: issues, sessions: sessions,
    agents: ['code-editor-senior'], usage: usage,
    change_requests: change_requests, commits: commits,
    reviews: reviews, merge_events: merge_events, outcome: outcome
  });
}

module.exports = { build: build, scenario: 'github_complete', provider: 'github' };
