/**
 * GitHub incomplete lifecycle fixture.
 *
 * Models a lifecycle that has not yet completed: issue opened,
 * change request opened, some development activity but not yet merged,
 * not yet closed. Covers the "incomplete" acceptance criteria.
 */

'use strict';
var shared = require('./shared');

function build() {
  shared.resetSeq();

  var PROVIDER = 'github';
  var REPO = 'weiyentan/opencode-gateway';

  var issues = [
    shared.buildEntityLink({
      entity_type: 'issue', external_id: '501', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0,
      correlation_method: 'issue_reference',
      evidence: [{ kind: 'issue_reference', source_entity_id: 'change_request:505', detail: 'resolves #501' }]
    })
  ];

  var change_requests = [
    shared.buildEntityLink({
      entity_type: 'change_request', external_id: '505', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0,
      correlation_method: 'issue_reference',
      evidence: [{ kind: 'title_match', source_entity_id: 'change_request:505', detail: 'exact title match' }]
    })
  ];

  var sessions = [
    shared.buildSessionLink({
      external_session_id: 'ses_01J4T2P0000000000000000010',
      session_id: '2a0b1c2d-0000-4000-8000-000000000010',
      agent: 'code-editor-mid', inferred: true,
      message_count: 15,
      total_input_tokens: 2000, total_output_tokens: 1000,
      total_cache_read_tokens: 0, total_cache_write_tokens: 0,
      total_estimated_cost_usd: 0.45,
      started_at: '2026-08-14T11:00:00Z', finished_at: null
    })
  ];

  var run = shared.buildRun({
    afk_run_id: '01KZX9M4G80000000000000010',
    provider: PROVIDER, status: 'running', outcome_status: 'open',
    title: 'Implement issue #501 — in progress',
    started_at: '2026-08-14T11:00:00Z', finished_at: null
  });

  var usage = shared.buildUsage({
    input_tokens: 2000, output_tokens: 1000,
    cache_read_tokens: 0, cache_write_tokens: 0,
    total_estimated_cost_usd: 0.45
  });

  var outcome = shared.buildOutcome({
    status: 'open',
    change_request_ids: ['change_request:505'],
    resolved_issue_ids: ['issue:501']
  });

  return shared.assembleDetail({
    run: run, issues: issues, sessions: sessions,
    agents: ['code-editor-mid'], usage: usage,
    change_requests: change_requests, commits: [], reviews: [],
    merge_events: [], outcome: outcome
  });
}

module.exports = { build: build, scenario: 'github_incomplete', provider: 'github' };
