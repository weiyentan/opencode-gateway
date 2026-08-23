/**
 * Provisional links and unresolved relationship fixtures.
 *
 * Models provisional entity links (provisional=true) and unresolved
 * relationship states (ambiguous, unmatched, parked). These test the
 * visual distinction between resolved and provisional links.
 */

'use strict';
var shared = require('./shared');

function build() {
  shared.resetSeq();

  var PROVIDER = 'github';
  var REPO = 'weiyentan/opencode-gateway';

  var issues = [
    shared.buildEntityLink({
      entity_type: 'issue', external_id: '801', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0,
      correlation_method: 'issue_reference',
      evidence: [{ kind: 'issue_reference', source_entity_id: 'change_request:805', detail: 'resolves #801' }]
    }),
    // Provisional issue link — low confidence, referenced not resolved
    shared.buildEntityLink({
      entity_type: 'issue', external_id: '802', provider: PROVIDER, repository: REPO,
      role: 'referenced', correlation_confidence: 0.1, provisional: true,
      correlation_method: 'issue_reference',
      evidence: [{ kind: 'issue_reference', source_entity_id: 'change_request:805', detail: 'mentioned #802' }]
    }),
    // Noise-level issue link — very low confidence
    shared.buildEntityLink({
      entity_type: 'issue', external_id: '803', provider: PROVIDER, repository: REPO,
      role: 'noise', correlation_confidence: 0.0, provisional: true,
      correlation_method: 'temporal_inference',
      evidence: []
    })
  ];

  var change_requests = [
    shared.buildEntityLink({
      entity_type: 'change_request', external_id: '805', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0,
      correlation_method: 'issue_reference',
      evidence: [{ kind: 'title_match', source_entity_id: 'change_request:805', detail: 'exact title match' }]
    })
  ];

  var sessions = [
    shared.buildSessionLink({
      external_session_id: 'ses_01J4T2P0000000000000000070',
      session_id: '2e0f1a2b-0000-4000-8000-000000000070',
      agent: 'code-editor-senior', inferred: false,
      message_count: 35,
      total_input_tokens: 4000, total_output_tokens: 2500,
      total_cache_read_tokens: 800, total_cache_write_tokens: 400,
      total_estimated_cost_usd: 1.00,
      started_at: '2026-08-17T09:00:00Z', finished_at: '2026-08-17T10:30:00Z'
    }),
    // Provisional session link — inferred, no resolved internal id
    shared.buildSessionLink({
      external_session_id: 'ses_01J4T2P0000000000000000071',
      session_id: null,
      agent: 'unknown', inferred: true,
      message_count: 3,
      total_input_tokens: 200, total_output_tokens: 100,
      total_cache_read_tokens: 0, total_cache_write_tokens: 0,
      total_estimated_cost_usd: 0.05,
      started_at: '2026-08-17T10:35:00Z', finished_at: '2026-08-17T10:40:00Z'
    })
  ];

  var run = shared.buildRun({
    afk_run_id: '01KZX9M4G80000000000000040',
    provider: PROVIDER, status: 'completed', outcome_status: 'merged',
    title: 'Implement issue #801 with provisional links',
    started_at: '2026-08-17T09:00:00Z', finished_at: '2026-08-17T10:30:00Z'
  });

  var usage = shared.buildUsage({
    input_tokens: 4200, output_tokens: 2600,
    cache_read_tokens: 800, cache_write_tokens: 400,
    total_estimated_cost_usd: 1.05
  });

  var outcome = shared.buildOutcome({
    status: 'merged',
    change_request_ids: ['change_request:805'],
    resolved_issue_ids: ['issue:801'],
    merge_event_id: 'merge_event:805',
    merged_at: '2026-08-17T10:35:00Z'
  });

  return shared.assembleDetail({
    run: run, issues: issues, sessions: sessions,
    agents: ['code-editor-senior', 'unknown'], usage: usage,
    change_requests: change_requests, commits: [], reviews: [],
    merge_events: [], outcome: outcome
  });
}

module.exports = { build: build, scenario: 'relationships', provider: 'github' };
