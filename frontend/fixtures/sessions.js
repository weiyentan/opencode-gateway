/**
 * Root and child session fixtures.
 *
 * Models a root session with two child (subagent) sessions, plus a
 * missing-parent case and a provisional session link. Tests the
 * parent_session_id linkage and nested tree structure.
 */

'use strict';
var shared = require('./shared');

function build() {
  shared.resetSeq();

  var PROVIDER = 'github';
  var REPO = 'weiyentan/opencode-gateway';

  var issues = [
    shared.buildEntityLink({
      entity_type: 'issue', external_id: '701', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0,
      correlation_method: 'issue_reference',
      evidence: [{ kind: 'issue_reference', source_entity_id: 'change_request:705', detail: 'resolves #701' }]
    })
  ];

  var change_requests = [
    shared.buildEntityLink({
      entity_type: 'change_request', external_id: '705', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0,
      correlation_method: 'issue_reference',
      evidence: [{ kind: 'title_match', source_entity_id: 'change_request:705', detail: 'exact title match' }]
    })
  ];

  var sessions = [
    // Root session (no parent)
    shared.buildSessionLink({
      external_session_id: 'ses_01J4T2P0000000000000000060',
      session_id: '9b7c8d9e-0000-4000-8000-000000000060',
      agent: 'code-editor-senior', inferred: false,
      message_count: 50,
      total_input_tokens: 6000, total_output_tokens: 4000,
      total_cache_read_tokens: 1200, total_cache_write_tokens: 600,
      total_estimated_cost_usd: 1.50,
      started_at: '2026-08-16T09:00:00Z', finished_at: '2026-08-16T11:00:00Z',
      parent_session_id: null
    }),
    // Child session 1 (subagent of root)
    shared.buildSessionLink({
      external_session_id: 'ses_01J4T2P0000000000000000061',
      session_id: '0c8d9e0f-0000-4000-8000-000000000061',
      agent: 'code-editor-mid', inferred: true,
      message_count: 20,
      total_input_tokens: 2500, total_output_tokens: 1500,
      total_cache_read_tokens: 400, total_cache_write_tokens: 200,
      total_estimated_cost_usd: 0.60,
      started_at: '2026-08-16T09:15:00Z', finished_at: '2026-08-16T09:45:00Z',
      parent_session_id: 'ses_01J4T2P0000000000000000060'
    }),
    // Child session 2 (subagent of root, runs after child 1)
    shared.buildSessionLink({
      external_session_id: 'ses_01J4T2P0000000000000000062',
      session_id: '1d9e0f1a-0000-4000-8000-000000000062',
      agent: 'test-debugger', inferred: true,
      message_count: 15,
      total_input_tokens: 1800, total_output_tokens: 1200,
      total_cache_read_tokens: 300, total_cache_write_tokens: 100,
      total_estimated_cost_usd: 0.45,
      started_at: '2026-08-16T10:00:00Z', finished_at: '2026-08-16T10:30:00Z',
      parent_session_id: 'ses_01J4T2P0000000000000000060'
    }),
    // Provisional session link (no internal session_id, inferred)
    shared.buildSessionLink({
      external_session_id: 'ses_01J4T2P0000000000000000063',
      session_id: null,
      agent: 'unknown', inferred: true,
      message_count: 5,
      total_input_tokens: 500, total_output_tokens: 300,
      total_cache_read_tokens: 0, total_cache_write_tokens: 0,
      total_estimated_cost_usd: 0.10,
      started_at: '2026-08-16T10:40:00Z', finished_at: '2026-08-16T10:50:00Z',
      parent_session_id: 'ses_01J4T2P0000000000000000060'
    })
  ];

  var run = shared.buildRun({
    afk_run_id: '01KZX9M4G80000000000000030',
    provider: PROVIDER, status: 'completed', outcome_status: 'merged',
    title: 'Implement issue #701 with root/child sessions',
    started_at: '2026-08-16T09:00:00Z', finished_at: '2026-08-16T11:00:00Z'
  });

  var usage = shared.buildUsage({
    input_tokens: 10800, output_tokens: 7000,
    cache_read_tokens: 1900, cache_write_tokens: 900,
    total_estimated_cost_usd: 2.65
  });

  var outcome = shared.buildOutcome({
    status: 'merged',
    change_request_ids: ['change_request:705'],
    resolved_issue_ids: ['issue:701'],
    merge_event_id: 'merge_event:705',
    merged_at: '2026-08-16T11:05:00Z'
  });

  return shared.assembleDetail({
    run: run, issues: issues, sessions: sessions,
    agents: ['code-editor-senior', 'code-editor-mid', 'test-debugger', 'unknown'],
    usage: usage,
    change_requests: change_requests, commits: [], reviews: [],
    merge_events: [], outcome: outcome
  });
}

module.exports = { build: build, scenario: 'sessions', provider: 'github' };
