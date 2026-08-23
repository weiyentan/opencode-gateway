/**
 * GitHub repeated develop/review cycles fixture.
 *
 * Models multiple chronologically-ordered executions preserving every
 * iteration of a develop/review cycle. Two sessions represent two
 * distinct development iterations on the same change request.
 */

'use strict';
var shared = require('./shared');

function build() {
  shared.resetSeq();

  var PROVIDER = 'github';
  var REPO = 'weiyentan/opencode-gateway';

  var issues = [
    shared.buildEntityLink({
      entity_type: 'issue', external_id: '601', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0,
      correlation_method: 'issue_reference',
      evidence: [{ kind: 'issue_reference', source_entity_id: 'change_request:605', detail: 'resolves #601' }]
    })
  ];

  var change_requests = [
    shared.buildEntityLink({
      entity_type: 'change_request', external_id: '605', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0,
      correlation_method: 'issue_reference',
      evidence: [{ kind: 'title_match', source_entity_id: 'change_request:605', detail: 'exact title match' }]
    })
  ];

  var commits = [
    shared.buildEntityLink({
      entity_type: 'commit', external_id: 'c000001', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0, correlation_method: 'commit_issue_reference',
      owning_change_request_id: '605', correlation_source: 'owning_change_request'
    }),
    shared.buildEntityLink({
      entity_type: 'commit', external_id: 'c000002', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0, correlation_method: 'commit_issue_reference',
      owning_change_request_id: '605', correlation_source: 'owning_change_request'
    }),
    shared.buildEntityLink({
      entity_type: 'commit', external_id: 'c000003', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0, correlation_method: 'commit_issue_reference',
      owning_change_request_id: '605', correlation_source: 'owning_change_request'
    })
  ];

  var reviews = [
    shared.buildEntityLink({
      entity_type: 'review', external_id: '3001', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 0.9, correlation_method: 'temporal_inference',
      owning_change_request_id: '605', correlation_source: 'owning_change_request'
    }),
    shared.buildEntityLink({
      entity_type: 'review', external_id: '3002', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 0.9, correlation_method: 'temporal_inference',
      owning_change_request_id: '605', correlation_source: 'owning_change_request'
    })
  ];

  var sessions = [
    shared.buildSessionLink({
      external_session_id: 'ses_01J4T2P0000000000000000040',
      session_id: '5d3e4f5a-0000-4000-8000-000000000040',
      agent: 'code-editor-senior', inferred: true,
      message_count: 25,
      total_input_tokens: 3000, total_output_tokens: 2000,
      total_cache_read_tokens: 500, total_cache_write_tokens: 200,
      total_estimated_cost_usd: 0.80,
      started_at: '2026-08-15T09:00:00Z', finished_at: '2026-08-15T09:45:00Z'
    }),
    shared.buildSessionLink({
      external_session_id: 'ses_01J4T2P0000000000000000041',
      session_id: '6e4f5a6b-0000-4000-8000-000000000041',
      agent: 'code-editor-senior', inferred: true,
      message_count: 30,
      total_input_tokens: 4000, total_output_tokens: 2500,
      total_cache_read_tokens: 600, total_cache_write_tokens: 300,
      total_estimated_cost_usd: 1.05,
      started_at: '2026-08-15T10:00:00Z', finished_at: '2026-08-15T10:50:00Z'
    })
  ];

  var run = shared.buildRun({
    afk_run_id: '01KZX9M4G80000000000000020',
    provider: PROVIDER, status: 'completed', outcome_status: 'merged',
    title: 'Implement issue #601 with review cycles',
    started_at: '2026-08-15T09:00:00Z', finished_at: '2026-08-15T10:50:00Z'
  });

  var usage = shared.buildUsage({
    input_tokens: 7000, output_tokens: 4500,
    cache_read_tokens: 1100, cache_write_tokens: 500,
    total_estimated_cost_usd: 1.85
  });

  var outcome = shared.buildOutcome({
    status: 'merged',
    change_request_ids: ['change_request:605'],
    resolved_issue_ids: ['issue:601'],
    merge_event_id: 'merge_event:605',
    merged_at: '2026-08-15T10:55:00Z'
  });

  return shared.assembleDetail({
    run: run, issues: issues, sessions: sessions,
    agents: ['code-editor-senior'], usage: usage,
    change_requests: change_requests, commits: commits,
    reviews: reviews, merge_events: [], outcome: outcome
  });
}

module.exports = { build: build, scenario: 'github_cycles', provider: 'github' };
