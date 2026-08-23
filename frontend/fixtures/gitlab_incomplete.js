/**
 * GitLab incomplete lifecycle fixture.
 *
 * Models a GitLab lifecycle that has not yet completed: issue opened,
 * merge request opened, some development activity, not yet merged,
 * not yet closed. Mirrors the GitHub incomplete fixture structure.
 */

'use strict';
var shared = require('./shared');

function build() {
  shared.resetSeq();

  var PROVIDER = 'gitlab';
  var REPO = 'opencode/gateway';

  var issues = [
    shared.buildEntityLink({
      entity_type: 'issue', external_id: '201', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0,
      correlation_method: 'issue_reference',
      evidence: [{ kind: 'issue_reference', source_entity_id: 'change_request:205', detail: 'resolves #201' }]
    })
  ];

  var change_requests = [
    shared.buildEntityLink({
      entity_type: 'change_request', external_id: '205', provider: PROVIDER, repository: REPO,
      role: 'resolved', correlation_confidence: 1.0,
      correlation_method: 'issue_reference',
      evidence: [{ kind: 'title_match', source_entity_id: 'change_request:205', detail: 'exact title match' }]
    })
  ];

  var sessions = [
    shared.buildSessionLink({
      external_session_id: 'ses_01J4T2P0000000000000000030',
      session_id: '4c2d3e4f-0000-4000-8000-000000000030',
      agent: 'code-editor-mid', inferred: true,
      message_count: 10,
      total_input_tokens: 1500, total_output_tokens: 800,
      total_cache_read_tokens: 0, total_cache_write_tokens: 0,
      total_estimated_cost_usd: 0.30,
      started_at: '2026-08-07T14:00:00Z', finished_at: null
    })
  ];

  var run = shared.buildRun({
    afk_run_id: '01KZDZSDP00000000000000010',
    provider: PROVIDER, status: 'running', outcome_status: 'open',
    title: 'Implement issue #201 — in progress',
    started_at: '2026-08-07T14:00:00Z', finished_at: null
  });

  var usage = shared.buildUsage({
    input_tokens: 1500, output_tokens: 800,
    cache_read_tokens: 0, cache_write_tokens: 0,
    total_estimated_cost_usd: 0.30
  });

  var outcome = shared.buildOutcome({
    status: 'open',
    change_request_ids: ['change_request:205'],
    resolved_issue_ids: ['issue:201']
  });

  return shared.assembleDetail({
    run: run, issues: issues, sessions: sessions,
    agents: ['code-editor-mid'], usage: usage,
    change_requests: change_requests, commits: [], reviews: [],
    merge_events: [], outcome: outcome
  });
}

module.exports = { build: build, scenario: 'gitlab_incomplete', provider: 'gitlab' };
