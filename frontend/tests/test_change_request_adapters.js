/**
 * Unit tests for the Change-request adapters and formatters (issue #612).
 *
 * Run with: node frontend/tests/test_change_request_adapters.js
 *
 * These tests exercise the PURE production adapter module
 * (frontend/adapters/change_request_adapters.js) directly via require() —
 * the same production code the browser loads.  No DOM, no fetch, no
 * provider credentials, no network access.
 *
 * Coverage: GitHub/GitLab parity, mixed execution outcomes, duplicate
 * attempts, missing cost data, malformed/partial optional data, and the
 * identity-preservation / no-browser-side-join guarantees.
 */

'use strict';

var path = require('path');

var A = require(path.join(__dirname, '..', 'adapters', 'change_request_adapters.js'));

var passed = 0;
var failed = 0;

function assert(condition, label) {
  if (condition) {
    passed++;
  } else {
    failed++;
    console.error('  \u2717 FAIL:', label);
  }
}

function assertEqual(actual, expected, label) {
  assert(actual === expected, label + ' (expected ' + JSON.stringify(expected) + ', got ' + JSON.stringify(actual) + ')');
}

function assertDeepEqual(actual, expected, label) {
  assert(JSON.stringify(actual) === JSON.stringify(expected),
    label + ' (expected ' + JSON.stringify(expected) + ', got ' + JSON.stringify(actual) + ')');
}

// ── Identity adapters ───────────────────────────────────────────────────

console.log('\u25B6 change-request identity');

(function () {
  assertDeepEqual(A.crIdentity({ provider: 'github', repository: 'acme/web-app', external_id: '142' }),
    { provider: 'github', repository: 'acme/web-app', external_id: '142' },
    'identity: canonical github tuple preserved');
  assertDeepEqual(A.crIdentity({ provider: 'gitlab', repository: 'group/project', external_id: '6' }),
    { provider: 'gitlab', repository: 'group/project', external_id: '6' },
    'identity: canonical gitlab tuple preserved');
  // Alternative field vocabulary
  assertDeepEqual(A.crIdentity({ provider: 'github', repository: 'acme/web-app', resource_number: 88 }),
    { provider: 'github', repository: 'acme/web-app', external_id: '88' },
    'identity: resource_number coerced to string external_id');
  assertDeepEqual(A.crIdentity({ provider: 'github', repository: 'acme/web-app', number: '77' }),
    { provider: 'github', repository: 'acme/web-app', external_id: '77' },
    'identity: number fallback accepted');
  // repository aliases
  assertDeepEqual(A.crIdentity({ provider: 'gitlab', repository_url: 'gitlab.example/group/proj', external_id: '9' }),
    { provider: 'gitlab', repository: 'gitlab.example/group/proj', external_id: '9' },
    'identity: repository_url alias');
  assertDeepEqual(A.crIdentity({ provider: 'github', repo: 'owner/repo', external_id: '1' }),
    { provider: 'github', repository: 'owner/repo', external_id: '1' },
    'identity: repo alias');
  // malformed / empty
  assertDeepEqual(A.crIdentity(null), { provider: 'unknown', repository: '', external_id: '' },
    'identity: null -> unknown provider, empty rest');
  assertDeepEqual(A.crIdentity({}), { provider: 'unknown', repository: '', external_id: '' },
    'identity: empty object -> unknown provider');
  assertDeepEqual(A.crIdentity({ provider: 'github' }), { provider: 'github', repository: '', external_id: '' },
    'identity: missing repo + external id defaults empty');
})();

console.log('\u25B6 crDisplayId + crProviderTerm');

(function () {
  assertEqual(A.crDisplayId({ provider: 'github', repository: 'acme/web-app', external_id: '142' }),
    'acme/web-app#142', 'display id: repo#external');
  assertEqual(A.crDisplayId({ provider: 'gitlab', repository: 'group/project', external_id: '6' }),
    'group/project#6', 'display id: gitlab repo#external');
  assertEqual(A.crDisplayId({ provider: 'github', external_id: '142' }), '142', 'display id: external only');
  assertEqual(A.crDisplayId({ provider: 'github', repository: 'acme/web-app' }), 'acme/web-app', 'display id: repo only');
  assertEqual(A.crDisplayId(null), '--', 'display id: null -> --');
  assertEqual(A.crProviderTerm('github'), 'PR', 'provider term: github -> PR');
  assertEqual(A.crProviderTerm('GitHub'), 'PR', 'provider term: GitHub (case) -> PR');
  assertEqual(A.crProviderTerm('gitlab'), 'MR', 'provider term: gitlab -> MR');
  assertEqual(A.crProviderTerm('GitLab'), 'MR', 'provider term: GitLab (case) -> MR');
  assertEqual(A.crProviderTerm('bitbucket'), 'CR', 'provider term: unknown -> CR');
  assertEqual(A.crProviderTerm(null), 'CR', 'provider term: null -> CR');
  assertEqual(A.crProviderTerm(''), 'CR', 'provider term: empty -> CR');
})();

console.log('\u25B6 crStatusHeaderLabel (issue #652)');

(function () {
  assertEqual(A.crStatusHeaderLabel('github'), 'PR Status', 'status header label: github -> PR Status');
  assertEqual(A.crStatusHeaderLabel('GitHub'), 'PR Status', 'status header label: GitHub (case) -> PR Status');
  assertEqual(A.crStatusHeaderLabel('gitlab'), 'MR Status', 'status header label: gitlab -> MR Status');
  assertEqual(A.crStatusHeaderLabel('GitLab'), 'MR Status', 'status header label: GitLab (case) -> MR Status');
  assertEqual(A.crStatusHeaderLabel('bitbucket'), 'MR/PR Status', 'status header label: unknown provider -> MR/PR Status');
  assertEqual(A.crStatusHeaderLabel('unknown'), 'MR/PR Status', 'status header label: unknown -> MR/PR Status');
  assertEqual(A.crStatusHeaderLabel(null), 'MR/PR Status', 'status header label: null -> MR/PR Status');
  assertEqual(A.crStatusHeaderLabel(''), 'MR/PR Status', 'status header label: empty -> MR/PR Status');
})();

// ── Provider state adapters ─────────────────────────────────────────────

console.log('\u25B6 provider state');

(function () {
  assertEqual(A.providerStateValue({ provider_state: 'merged' }), 'merged', 'value: merged');
  assertEqual(A.providerStateValue({ provider_state: 'opened' }), 'open', 'value: opened normalizes to open');
  assertEqual(A.providerStateValue({ state: 'closed' }), 'closed', 'value: state fallback');
  assertEqual(A.providerStateValue({ provider_state: 'reopened' }), 'reopened', 'value: reopened preserved');
  assertEqual(A.providerStateValue({}), '', 'value: missing -> empty string');
  assertEqual(A.providerStateValue(null), '', 'value: null -> empty string');

  assertEqual(A.providerStateLabel('merged'), 'merged', 'label: merged');
  assertEqual(A.providerStateLabel('open'), 'open', 'label: open');
  assertEqual(A.providerStateLabel(''), '--', 'label: empty -> --');
  assertEqual(A.providerStateLabel(null), '--', 'label: null -> --');

  assertEqual(A.providerStateBadgeClass('merged'), 'badge-merged', 'badge: merged');
  assertEqual(A.providerStateBadgeClass('open'), 'badge-open', 'badge: open');
  assertEqual(A.providerStateBadgeClass('reopened'), 'badge-open', 'badge: reopened -> open');
  assertEqual(A.providerStateBadgeClass('closed'), 'badge-closed', 'badge: closed');
  assertEqual(A.providerStateBadgeClass('whatever'), 'badge-unknown', 'badge: unknown value');
  assertEqual(A.providerStateBadgeClass(null), 'badge-unknown', 'badge: null');
})();

// ── AFK automation state adapters ───────────────────────────────────────

console.log('\u25B6 AFK automation state');

(function () {
  assertEqual(A.afkStateValue({ automation_state: 'completed' }), 'completed', 'value: completed');
  assertEqual(A.afkStateValue({ afk_state: 'running' }), 'running', 'value: afk_state alias');
  assertEqual(A.afkStateValue({ afk_status: 'failed' }), 'failed', 'value: afk_status alias');
  assertEqual(A.afkStateValue({ status: 'cancelled' }), 'cancelled', 'value: status fallback');
  assertEqual(A.afkStateValue({ run: { status: 'pending' } }), 'pending', 'value: nested run.status');
  assertEqual(A.afkStateValue({}), '', 'value: missing -> empty string');
  assertEqual(A.afkStateValue(null), '', 'value: null -> empty string');

  assertEqual(A.afkStateLabel('pending'), 'pending', 'label: pending');
  assertEqual(A.afkStateLabel('running'), 'running', 'label: running');
  assertEqual(A.afkStateLabel(''), '--', 'label: empty -> --');

  assertEqual(A.afkStateBadgeClass('running'), 'badge-running', 'badge: running');
  assertEqual(A.afkStateBadgeClass('completed'), 'badge-completed', 'badge: completed');
  assertEqual(A.afkStateBadgeClass('failed'), 'badge-failed', 'badge: failed');
  assertEqual(A.afkStateBadgeClass('cancelled'), 'badge-cancelled', 'badge: cancelled');
  assertEqual(A.afkStateBadgeClass('stale'), 'badge-stale', 'badge: stale');
  assertEqual(A.afkStateBadgeClass('timed_out'), 'badge-stale', 'badge: timed_out -> stale');
  assertEqual(A.afkStateBadgeClass('blocked'), 'badge-blocked', 'badge: blocked');
  assertEqual(A.afkStateBadgeClass('pending'), 'badge-blocked', 'badge: pending -> blocked (intentional wait)');
  assertEqual(A.afkStateBadgeClass('bogus'), 'badge-unknown', 'badge: unknown');
})();

// ── Cost adapters ───────────────────────────────────────────────────────

console.log('\u25B6 cost');

(function () {
  assertEqual(A.crCostUsd({ total_estimated_cost_usd: 4.5 }), 4.5, 'usd: total_estimated_cost_usd');
  assertEqual(A.crCostUsd({ estimated_cost_usd: 0.08 }), 0.08, 'usd: estimated_cost_usd');
  assertEqual(A.crCostUsd({ cost_usd: 0 }), 0, 'usd: explicit zero is a real cost');
  assertEqual(A.crCostUsd({ cost_usd: '1.25' }), 1.25, 'usd: numeric string parsed');
  assertEqual(A.crCostUsd({}), null, 'usd: missing -> null (unavailable)');
  assertEqual(A.crCostUsd(null), null, 'usd: null -> null');
  assertEqual(A.crCostUsd({ cost_usd: 'abc' }), null, 'usd: unparseable -> null');

  assertEqual(A.crCostAvailable({ total_estimated_cost_usd: 1.0 }), true, 'available: known amount');
  assertEqual(A.crCostAvailable({}), false, 'available: missing -> false');
  assertEqual(A.crCostAvailable({ cost_usd: 0 }), true, 'available: zero is available');

  assertEqual(A.fmtCrCost(1.5), '$1.50', 'format: 1.5 -> $1.50');
  assertEqual(A.fmtCrCost(0.005), '$0.0050', 'format: sub-cent uses 4 decimals');
  assertEqual(A.fmtCrCost(0), '$0.0000', 'format: zero renders as a known amount');
  assertEqual(A.fmtCrCost(null), 'Cost unavailable', 'format: null -> Cost unavailable');
  assertEqual(A.fmtCrCost(undefined), 'Cost unavailable', 'format: undefined -> Cost unavailable');
  assertEqual(A.fmtCrCost(''), 'Cost unavailable', 'format: empty string -> Cost unavailable');
  assertEqual(A.fmtCrCost('abc'), 'Cost unavailable', 'format: unparseable -> Cost unavailable');
  assertEqual(A.fmtCrCost(123.456), '$123.46', 'format: 2 decimals for normal amounts');
})();

// ── Timestamp adapters ──────────────────────────────────────────────────

console.log('\u25B6 timestamps');

(function () {
  assertEqual(A.crLatestActivityAt({ latest_activity_at: '2026-08-05T14:30:00Z' }),
    '2026-08-05T14:30:00Z', 'latest: latest_activity_at');
  assertEqual(A.crLatestActivityAt({ last_activity_at: '2026-08-05T14:30:00Z' }),
    '2026-08-05T14:30:00Z', 'latest: last_activity_at alias');
  assertEqual(A.crLatestActivityAt({ last_seen_at: '2026-08-05T14:30:00Z' }),
    '2026-08-05T14:30:00Z', 'latest: last_seen_at alias');
  assertEqual(A.crLatestActivityAt({ updated_at: '2026-08-05T14:30:00Z' }),
    '2026-08-05T14:30:00Z', 'latest: updated_at alias');
  assertEqual(A.crLatestActivityAt({}), null, 'latest: missing -> null');
  assertEqual(A.crLatestActivityAt(null), null, 'latest: null -> null');

  var ts = A.fmtCrTimestamp('2026-08-05T14:30:00Z');
  assert(ts !== '--' && ts.length > 0, 'timestamp: valid ISO renders a non-dash label (' + ts + ')');
  assertEqual(A.fmtCrTimestamp(null), '--', 'timestamp: null -> --');
  assertEqual(A.fmtCrTimestamp(''), '--', 'timestamp: empty -> --');
  assertEqual(A.fmtCrTimestamp('not-a-date'), '--', 'timestamp: unparseable -> --');

  assertEqual(A.fmtCrDuration('2026-01-01T00:00:00Z', '2026-01-01T00:05:00Z'), '5m', 'duration: 5m');
  assertEqual(A.fmtCrDuration('2026-01-01T00:00:00Z', '2026-01-01T01:30:00Z'), '1h 30m', 'duration: 1h 30m');
  assertEqual(A.fmtCrDuration('2026-01-01T00:00:00Z', '2026-01-03T05:00:00Z'), '2d 5h', 'duration: 2d 5h');
  assertEqual(A.fmtCrDuration(null, '2026-01-01T01:00:00Z'), '--', 'duration: missing start -> --');
  assertEqual(A.fmtCrDuration('2026-01-03T05:00:00Z', '2026-01-01T00:00:00Z'), '--', 'duration: reversed -> --');
  assertEqual(A.fmtCrDuration('bad', 'also-bad'), '--', 'duration: unparseable -> --');
})();

// ── Execution purpose + status formatters ───────────────────────────────

console.log('\u25B6 execution purpose + status');

(function () {
  assertEqual(A.executionPurpose('implementation'), 'implementation', 'purpose: implementation');
  assertEqual(A.executionPurpose('impl'), 'implementation', 'purpose: impl -> implementation');
  assertEqual(A.executionPurpose('develop'), 'implementation', 'purpose: develop (phase) -> implementation');
  assertEqual(A.executionPurpose('review'), 'review', 'purpose: review');
  assertEqual(A.executionPurpose('retry'), 'retry', 'purpose: retry');
  assertEqual(A.executionPurpose('retry_execution'), 'retry', 'purpose: retry_execution -> retry');
  assertEqual(A.executionPurpose('reattempt'), 'retry', 'purpose: reattempt -> retry');
  assertEqual(A.executionPurpose(null), 'unknown', 'purpose: null -> unknown');
  assertEqual(A.executionPurpose(''), 'unknown', 'purpose: empty -> unknown');
  assertEqual(A.executionPurpose('something-else'), 'something-else', 'purpose: passthrough unknown vocab');

  assertEqual(A.executionPurposeLabel('implementation'), 'Implementation', 'purpose label: Implementation');
  assertEqual(A.executionPurposeLabel('review'), 'Review', 'purpose label: Review');
  assertEqual(A.executionPurposeLabel('retry'), 'Retry', 'purpose label: Retry');
  assertEqual(A.executionPurposeLabel(null), 'Unknown', 'purpose label: null -> Unknown');

  assertEqual(A.executionPurposeBadgeClass('implementation'), 'badge-completed', 'purpose badge: implementation');
  assertEqual(A.executionPurposeBadgeClass('review'), 'badge-open', 'purpose badge: review');
  assertEqual(A.executionPurposeBadgeClass('retry'), 'badge-cancelled', 'purpose badge: retry');
  assertEqual(A.executionPurposeBadgeClass(null), 'badge-unknown', 'purpose badge: unknown');

  assertEqual(A.executionStatusLabel('pending'), 'pending', 'status label: pending');
  assertEqual(A.executionStatusLabel('running'), 'running', 'status label: running');
  assertEqual(A.executionStatusLabel(''), '--', 'status label: empty -> --');

  assertEqual(A.executionStatusBadgeClass('pending'), 'badge-blocked', 'status badge: pending');
  assertEqual(A.executionStatusBadgeClass('running'), 'badge-running', 'status badge: running');
  assertEqual(A.executionStatusBadgeClass('completed'), 'badge-completed', 'status badge: completed');
  assertEqual(A.executionStatusBadgeClass('failed'), 'badge-failed', 'status badge: failed');
  assertEqual(A.executionStatusBadgeClass('cancelled'), 'badge-cancelled', 'status badge: cancelled');
  assertEqual(A.executionStatusBadgeClass('stale'), 'badge-stale', 'status badge: stale');
  assertEqual(A.executionStatusBadgeClass('bogus'), 'badge-unknown', 'status badge: unknown');
})();

// ── Aggregate data helpers ──────────────────────────────────────────────

console.log('\u25B6 aggregate data');

(function () {
  assertDeepEqual(A.normalizeExecutionCounts(null),
    { total: 0, running: 0, completed: 0, failed: 0, cancelled: 0,
      implementation: 0, review: 0, retry: 0 }, 'counts: null -> zeros');
  assertDeepEqual(A.normalizeExecutionCounts(5),
    { total: 5, running: 0, completed: 0, failed: 0, cancelled: 0,
      implementation: 0, review: 0, retry: 0 }, 'counts: plain number -> total only');
  // Outcome-state vocabulary (canonical summary `executions`, issue #610)
  assertDeepEqual(A.normalizeExecutionCounts({ total: 6, running: 1, completed: 3, failed: 1, cancelled: 1 }),
    { total: 6, running: 1, completed: 3, failed: 1, cancelled: 1,
      implementation: 0, review: 0, retry: 0 },
    'counts: outcome-state vocabulary preserved');
  assertDeepEqual(A.normalizeExecutionCounts({ running: 1, completed: 3, failed: 1, cancelled: 1 }),
    { total: 6, running: 1, completed: 3, failed: 1, cancelled: 1,
      implementation: 0, review: 0, retry: 0 },
    'counts: outcome-state total derived from buckets');
  // Purpose vocabulary (detail adapter aggregation)
  assertDeepEqual(A.normalizeExecutionCounts({ implementation: 2, review: 1, retry: 3, total: 6 }),
    { total: 6, running: 0, completed: 0, failed: 0, cancelled: 0,
      implementation: 2, review: 1, retry: 3 }, 'counts: explicit buckets preserved');
  assertDeepEqual(A.normalizeExecutionCounts({ implementation: 2, review: 1, retry: 3 }),
    { total: 6, running: 0, completed: 0, failed: 0, cancelled: 0,
      implementation: 2, review: 1, retry: 3 }, 'counts: total derived from buckets');
  assertDeepEqual(A.normalizeExecutionCounts({ implementation: -1, review: 0, retry: 0 }),
    { total: 0, running: 0, completed: 0, failed: 0, cancelled: 0,
      implementation: 0, review: 0, retry: 0 }, 'counts: negative clamped to 0');
  assertDeepEqual(A.normalizeExecutionCounts({}),
    { total: 0, running: 0, completed: 0, failed: 0, cancelled: 0,
      implementation: 0, review: 0, retry: 0 }, 'counts: empty object -> zeros');
})();

// ── Summary adapter ─────────────────────────────────────────────────────

console.log('\u25B6 summary adapter');

(function () {
  var gh = A.adaptChangeRequestSummary({
    provider: 'github', repository: 'acme/web-app', external_id: '142',
    title: 'Implement auth',
    provider_state: 'merged',
    automation_state: 'completed',
    total_estimated_cost_usd: 4.5,
    latest_activity_at: '2026-08-05T14:30:00Z',
    executions: { total: 4, running: 0, completed: 3, failed: 1, cancelled: 0 },
    run_count: 1
  });
  assertDeepEqual(gh.identity, { provider: 'github', repository: 'acme/web-app', external_id: '142' },
    'summary: github identity preserved');
  assertEqual(gh.displayId, 'acme/web-app#142', 'summary: display id');
  assertEqual(gh.providerTerm, 'PR', 'summary: provider term PR');
  assertEqual(gh.statusHeaderLabel, 'PR Status', 'summary: GitHub status header label PR Status');
  assertEqual(gh.title, 'Implement auth', 'summary: title');
  assertEqual(gh.providerState.value, 'merged', 'summary: provider state merged');
  assertEqual(gh.providerState.badgeClass, 'badge-merged', 'summary: provider state badge');
  assertEqual(gh.afkAutomationState.value, 'completed', 'summary: afk state completed');
  assertEqual(gh.afkAutomationState.badgeClass, 'badge-completed', 'summary: afk state badge');
  assertEqual(gh.cost.available, true, 'summary: cost available');
  assertEqual(gh.cost.usd, 4.5, 'summary: cost usd');
  assertEqual(gh.cost.label, '$4.50', 'summary: cost label');
  assertEqual(gh.latestActivityAt, '2026-08-05T14:30:00Z', 'summary: latest activity');
  assertDeepEqual(gh.executionCounts, { total: 4, running: 0, completed: 3, failed: 1, cancelled: 0,
    implementation: 0, review: 0, retry: 0 },
    'summary: execution counts from canonical executions property');
  assertEqual(gh.runCount, 1, 'summary: run count');

  // GitLab parity: same shape, MR term, gitlab identity
  var gl = A.adaptChangeRequestSummary({
    provider: 'gitlab', repository: 'group/project', external_id: '6',
    title: 'Add config',
    provider_state: 'opened',
    automation_state: 'running',
    estimated_cost_usd: null,
    execution_count: 3
  });
  assertDeepEqual(gl.identity, { provider: 'gitlab', repository: 'group/project', external_id: '6' },
    'summary: gitlab identity preserved');
  assertEqual(gl.providerTerm, 'MR', 'summary: provider term MR');
  assertEqual(gl.statusHeaderLabel, 'MR Status', 'summary: GitLab status header label MR Status');
  assertEqual(gl.providerState.value, 'open', 'summary: opened normalizes to open');
  assertEqual(gl.afkAutomationState.value, 'running', 'summary: afk running');
  assertEqual(gl.cost.available, false, 'summary: missing cost unavailable');
  assertEqual(gl.cost.label, 'Cost unavailable', 'summary: missing cost label');
  assertEqual(gl.cost.usd, null, 'summary: missing cost usd null');
  assertDeepEqual(gl.executionCounts, { total: 3, running: 0, completed: 0, failed: 0, cancelled: 0,
    implementation: 0, review: 0, retry: 0 },
    'summary: plain execution_count -> total only');

  // Zero cost is available, never unavailable
  var zero = A.adaptChangeRequestSummary({ provider: 'github', cost_usd: 0 });
  assertEqual(zero.cost.available, true, 'summary: zero cost is available');
  assertEqual(zero.cost.label, '$0.0000', 'summary: zero cost label');

  // Identity + statuses + cost survive partial payloads (malformed/partial)
  var partial = A.adaptChangeRequestSummary({ provider: 'github', repository: 'r' });
  assertEqual(partial.displayId, 'r', 'summary: partial payload display id');
  assertEqual(partial.providerState.value, '', 'summary: partial provider state empty');
  assertEqual(partial.afkAutomationState.value, '', 'summary: partial afk state empty');
  assertEqual(partial.cost.available, false, 'summary: partial cost unavailable');
  assertDeepEqual(partial.executionCounts, { total: 0, running: 0, completed: 0, failed: 0, cancelled: 0,
    implementation: 0, review: 0, retry: 0 },
    'summary: partial counts zeros');

  assertEqual(A.adaptChangeRequestSummary(null).identity.provider, 'unknown',
    'summary: null item -> unknown identity');
  assertEqual(A.adaptChangeRequestSummary(null).statusHeaderLabel, 'MR/PR Status',
    'summary: null item -> MR/PR Status fallback');

  // The canonical summary contract carries `executions` (outcome-state
  // counts, issue #610).  The legacy aliases (`execution_counts` / `counts`)
  // are NOT part of the contract and must never be consumed — an item that
  // carries ONLY the wrong aliases has no canonical counts (zeros).
  var onlyAliases = A.adaptChangeRequestSummary({
    provider: 'github', repository: 'acme/web-app', external_id: '1',
    execution_counts: { implementation: 2, review: 1, retry: 1, total: 4 },
    counts: { total: 9 }
  });
  assertDeepEqual(onlyAliases.executionCounts,
    { total: 0, running: 0, completed: 0, failed: 0, cancelled: 0,
      implementation: 0, review: 0, retry: 0 },
    'summary: legacy aliases are not consumed (canonical executions only)');

  // The canonical `executions` property wins over the aliases when both are
  // present — the adapter reads the Gateway-owned contract, never a legacy
  // side-channel.
  var canonicalWins = A.adaptChangeRequestSummary({
    provider: 'github', repository: 'acme/web-app', external_id: '2',
    executions: { total: 5, running: 1, completed: 3, failed: 1, cancelled: 0 },
    execution_counts: { implementation: 2, review: 1, retry: 1, total: 4 }
  });
  assertDeepEqual(canonicalWins.executionCounts,
    { total: 5, running: 1, completed: 3, failed: 1, cancelled: 0,
      implementation: 0, review: 0, retry: 0 },
    'summary: canonical executions property wins over legacy aliases');
})();

console.log('\u25B6 summary list adapter');

(function () {
  var list = A.adaptChangeRequestSummaryList({
    items: [
      { provider: 'github', repository: 'acme/web-app', external_id: '142' },
      { provider: 'gitlab', repository: 'group/project', external_id: '6' }
    ]
  });
  assertEqual(list.length, 2, 'list: envelope items adapted');
  assertEqual(list[0].providerTerm, 'PR', 'list: first is github PR');
  assertEqual(list[1].providerTerm, 'MR', 'list: second is gitlab MR');

  assertEqual(A.adaptChangeRequestSummaryList([{ provider: 'github', external_id: '1' }]).length, 1,
    'list: bare array accepted');
  assertEqual(A.adaptChangeRequestSummaryList([]).length, 0, 'list: empty array -> []');
  assertEqual(A.adaptChangeRequestSummaryList(null).length, 0, 'list: null -> []');
  assertEqual(A.adaptChangeRequestSummaryList({}).length, 0, 'list: empty envelope -> []');
})();

// ── Detail adapter: executions ──────────────────────────────────────────

console.log('\u25B6 detail adapter — executions');

(function () {
  // Execution-binding shape (awx_job nested)
  var binding = A.adaptExecution({
    awx_job: { job_id: '1001', job_template_id: 42 },
    external_session_id: 'ses-dev-001',
    afk_run_id: '01KZX9M4G80000000000000001',
    trigger_type: 'eda',
    purpose: 'implementation',
    outcome: 'completed',
    started_at: '2026-08-01T09:05:00Z',
    finished_at: '2026-08-01T09:45:00Z',
    estimated_cost_usd: 0.08
  });
  assertEqual(binding.awxJobId, '1001', 'exec: awx job id from nested shape');
  assertEqual(binding.jobTemplateId, 42, 'exec: job template id');
  assertEqual(binding.externalSessionId, 'ses-dev-001', 'exec: session id');
  assertEqual(binding.afkRunId, '01KZX9M4G80000000000000001', 'exec: afk run id');
  assertEqual(binding.triggerType, 'eda', 'exec: trigger type');
  assertEqual(binding.purpose.value, 'implementation', 'exec: purpose implementation');
  assertEqual(binding.purpose.label, 'Implementation', 'exec: purpose label');
  assertEqual(binding.status.value, 'completed', 'exec: status from outcome');
  assertEqual(binding.status.badgeClass, 'badge-completed', 'exec: status badge');
  assertEqual(binding.outcome, 'completed', 'exec: raw outcome preserved');
  assertEqual(binding.duration, '40m', 'exec: duration computed');
  assertEqual(binding.cost.available, true, 'exec: cost available');
  assertEqual(binding.cost.usd, 0.08, 'exec: cost usd');
  assertEqual(binding.cost.label, '$0.08', 'exec: sub-cent cost label');

  // Provenance-timeline shape (flat awx_job_id, phase, status)
  var prov = A.adaptExecution({
    phase: 'review',
    status: 'completed',
    outcome: 'changes_requested',
    awx_job_id: 'awx-job-1002',
    session_id: 'ses-review-001',
    started_at: '2026-08-01T10:00:00Z',
    finished_at: '2026-08-01T10:15:00Z',
    input_tokens: 5000, output_tokens: 800,
    estimated_cost_usd: 0.02
  });
  assertEqual(prov.awxJobId, 'awx-job-1002', 'exec: flat awx_job_id');
  assertEqual(prov.purpose.value, 'review', 'exec: phase review -> purpose review');
  assertEqual(prov.purpose.label, 'Review', 'exec: purpose label Review');
  assertEqual(prov.status.value, 'completed', 'exec: status from status field');
  assertEqual(prov.duration, '15m', 'exec: flat duration');
  // Per-execution token telemetry (flat token fields — #614)
  assertEqual(prov.tokens.available, true, 'exec: flat token telemetry available');
  assertEqual(prov.tokens.inputTokens, 5000, 'exec: input tokens');
  assertEqual(prov.tokens.outputTokens, 800, 'exec: output tokens');
  assertEqual(prov.tokens.cacheReadTokens, null, 'exec: absent cache read tokens null');
  assertEqual(prov.tokens.cacheWriteTokens, null, 'exec: absent cache write tokens null');

  // Retry purpose
  var retry = A.adaptExecution({ purpose: 'retry', outcome: 'failed', awx_job: { job_id: '3004' } });
  assertEqual(retry.purpose.value, 'retry', 'exec: retry purpose');
  assertEqual(retry.purpose.badgeClass, 'badge-cancelled', 'exec: retry badge');
  assertEqual(retry.status.value, 'failed', 'exec: failed status');
  assertEqual(retry.status.badgeClass, 'badge-failed', 'exec: failed badge');

  // Pending / running / cancelled / stale
  assertEqual(A.adaptExecution({ awx_job: { job_id: 'x' }, outcome: 'running' }).status.value, 'running', 'exec: running');
  assertEqual(A.adaptExecution({ awx_job: { job_id: 'x' }, outcome: 'cancelled' }).status.value, 'cancelled', 'exec: cancelled');
  assertEqual(A.adaptExecution({ awx_job: { job_id: 'x' }, status: 'stale' }).status.value, 'stale', 'exec: stale');

  // Missing cost on an execution
  var noCost = A.adaptExecution({ awx_job: { job_id: 'x' }, outcome: 'completed' });
  assertEqual(noCost.cost.available, false, 'exec: missing cost unavailable');
  assertEqual(noCost.cost.label, 'Cost unavailable', 'exec: missing cost label');
  assertEqual(noCost.tokens.available, false, 'exec: missing token telemetry unavailable');
  assertEqual(noCost.tokens.inputTokens, null, 'exec: missing input tokens null');

  // Malformed execution
  var bad = A.adaptExecution(null);
  assertEqual(bad.awxJobId, null, 'exec: null execution -> null job id');
  assertEqual(bad.purpose.value, 'unknown', 'exec: null execution -> unknown purpose');
  assertEqual(bad.status.label, '--', 'exec: null execution -> -- status');
  assertEqual(bad.cost.available, false, 'exec: null execution -> cost unavailable');
  assertEqual(bad.duration, '--', 'exec: null execution -> no duration');
  assertEqual(bad.tokens.available, false, 'exec: null execution -> no token telemetry');
})();

console.log('\u25B6 detail adapter — AFK Run Cost + sessions + usage');

(function () {
  var detail = A.adaptChangeRequestDetail({
    change_request: {
      provider: 'gitlab', repository: 'group/project', external_id: '6',
      title: 'Add config', provider_state: 'merged', automation_state: 'completed'
    },
    executions: [
      { awx_job: { job_id: '2001' }, purpose: 'implementation', outcome: 'completed', estimated_cost_usd: 0.06 },
      { awx_job: { job_id: '2002' }, purpose: 'review', outcome: 'completed', estimated_cost_usd: 0.02 },
      { awx_job: { job_id: '2003' }, purpose: 'retry', outcome: 'failed', estimated_cost_usd: null },
      { awx_job: { job_id: '2004' }, purpose: 'implementation', outcome: 'running', estimated_cost_usd: 0.04 }
    ],
    total_estimated_cost_usd: 0.12,
    sessions: [{ external_session_id: 'ses-1' }, { external_session_id: 'ses-2' }],
    usage: { input_tokens: 1000, output_tokens: 500 },
    merge_state: 'merged',
    timeline: { events: [] }
  });

  assertDeepEqual(detail.identity, { provider: 'gitlab', repository: 'group/project', external_id: '6' },
    'detail: identity preserved');
  assertEqual(detail.providerTerm, 'MR', 'detail: MR term');
  assertEqual(detail.statusHeaderLabel, 'MR Status', 'detail: GitLab status header label MR Status');
  assertEqual(detail.providerState.value, 'merged', 'detail: provider state merged');
  assertEqual(detail.afkAutomationState.value, 'completed', 'detail: afk state completed');
  assertEqual(detail.runCost.available, true, 'detail: AFK Run Cost available');
  assertEqual(detail.runCost.usd, 0.12, 'detail: AFK Run Cost uses gateway value');
  assertEqual(detail.executions.length, 4, 'detail: 4 executions preserved (duplicates kept)');
  assertDeepEqual(detail.executionCounts, { total: 4, running: 0, completed: 0, failed: 0, cancelled: 0,
    implementation: 2, review: 1, retry: 1 },
    'detail: purpose buckets aggregated from same-payload executions');
  assertEqual(detail.sessions.length, 2, 'detail: sessions preserved');
  assertEqual(detail.usage.input_tokens, 1000, 'detail: usage preserved');
  assertEqual(detail.mergeState, 'merged', 'detail: merge state');
  // The Gateway wraps the timeline in a `{ events: [...] }` object
  // (ChangeRequestTimeline); the adapter must flatten it to the event array
  // the renderer iterates (renderChangeRequestTimeline calls timeline.forEach).
  assertEqual(detail.timeline.length, 0, 'detail: timeline envelope flattened to event array');

  // Duplicate attempts: two AWX jobs for the same change request are kept
  // as separate executions (never collapsed into one row).
  var dup = A.adaptChangeRequestDetail({
    change_request: { provider: 'github', repository: 'r', external_id: '1' },
    executions: [
      { awx_job: { job_id: '3001' }, purpose: 'implementation', outcome: 'failed', estimated_cost_usd: 0.05 },
      { awx_job: { job_id: '3002' }, purpose: 'implementation', outcome: 'completed', estimated_cost_usd: 0.10 }
    ]
  });
  assertEqual(dup.executions.length, 2, 'detail: duplicate attempts preserved as history');
  assertEqual(dup.executions[0].awxJobId, '3001', 'detail: first attempt kept');
  assertEqual(dup.executions[1].awxJobId, '3002', 'detail: retry attempt kept');

  // Mixed execution outcomes: completed + failed + cancelled + running
  var mixed = A.adaptChangeRequestDetail({
    change_request: { provider: 'github', repository: 'r', external_id: '2' },
    executions: [
      { awx_job: { job_id: '1' }, purpose: 'implementation', outcome: 'completed' },
      { awx_job: { job_id: '2' }, purpose: 'implementation', outcome: 'failed' },
      { awx_job: { job_id: '3' }, purpose: 'review', outcome: 'cancelled' },
      { awx_job: { job_id: '4' }, purpose: 'review', outcome: 'running' }
    ]
  });
  assertDeepEqual(mixed.executions.map(function (e) { return e.status.value; }),
    ['completed', 'failed', 'cancelled', 'running'], 'detail: mixed outcomes preserved');

  // Missing cost telemetry: no gateway run total, per-execution costs absent
  var noCost = A.adaptChangeRequestDetail({
    change_request: { provider: 'github', repository: 'r', external_id: '3' },
    executions: [
      { awx_job: { job_id: '1' }, purpose: 'implementation', outcome: 'completed' }
    ]
  });
  assertEqual(noCost.runCost.available, false, 'detail: missing run cost unavailable');
  assertEqual(noCost.runCost.label, 'Cost unavailable', 'detail: missing run cost label');
  assertEqual(noCost.runCost.usd, null, 'detail: missing run cost usd null');

  // Missing gateway run total: per-execution (AWX Execution Cost) subtotals
  // present, but the Gateway aggregate is null -> the run total is
  // unavailable (null), never a browser-side sum that could invent or
  // double-count cost (issue #617 review finding HIGH-1).
  var partial = A.adaptChangeRequestDetail({
    change_request: { provider: 'github', repository: 'r', external_id: '4' },
    executions: [
      { awx_job: { job_id: '1' }, purpose: 'implementation', outcome: 'completed', estimated_cost_usd: 0.30 },
      { awx_job: { job_id: '2' }, purpose: 'review', outcome: 'failed', estimated_cost_usd: null }
    ]
  });
  assertEqual(partial.runCost.available, false, 'detail: missing gateway run total -> unavailable');
  assertEqual(partial.runCost.usd, null, 'detail: no browser-side fallback sum');
  assertEqual(partial.runCost.label, 'Cost unavailable', 'detail: missing gateway run total label');
  // Known lower-level costs remain visible when the run total is unavailable:
  // each execution keeps its Gateway-computed AWX Execution Cost subtotal,
  // and the execution with unknown cost renders 'Cost unavailable' (never $0).
  assertEqual(partial.executions[0].cost.available, true, 'detail: known execution subtotal stays available');
  assertEqual(partial.executions[0].cost.usd, 0.30, 'detail: known execution subtotal value preserved');
  assertEqual(partial.executions[0].cost.label, '$0.30', 'detail: known execution subtotal label');
  assertEqual(partial.executions[1].cost.available, false, 'detail: unknown execution subtotal unavailable');
  assertEqual(partial.executions[1].cost.label, 'Cost unavailable', 'detail: unknown execution subtotal label');
  assertEqual(partial.executions[1].cost.usd, null, 'detail: unknown execution subtotal never coerced to zero');

  // Summary-nested detail contract (detail = { summary: {...} })
  var nested = A.adaptChangeRequestDetail({
    summary: { provider: 'gitlab', repository: 'g/p', external_id: '9', provider_state: 'open' },
    executions: []
  });
  assertEqual(nested.identity.external_id, '9', 'detail: summary-nested identity');
  assertEqual(nested.providerState.value, 'open', 'detail: summary-nested provider state');

  // Unknown provider → MR/PR Status fallback in the detail view model.
  var unknownDetail = A.adaptChangeRequestDetail({
    change_request: { repository: 'r', external_id: '10', provider_state: 'open' }
  });
  assertEqual(unknownDetail.statusHeaderLabel, 'MR/PR Status',
    'detail: unknown provider -> MR/PR Status fallback');

  // null/empty detail
  assertEqual(A.adaptChangeRequestDetail(null), null, 'detail: null -> null');
  var empty = A.adaptChangeRequestDetail({ change_request: null, executions: null });
  assertEqual(empty.executions.length, 0, 'detail: null executions -> empty array');
  assertEqual(empty.identity.provider, 'unknown', 'detail: missing change_request -> unknown identity');
  assertEqual(empty.sessions.length, 0, 'detail: missing sessions -> empty array');
})();

console.log('\u25B6 detail adapter — timeline envelope flattening');

(function () {
  // Backend contract: timeline is `{ events: [...] }` or null (issue #611
  // ChangeRequestTimeline).  The adapter must surface the EVENTS ARRAY to the
  // renderer, which iterates view.timeline.forEach(...) directly.
  var wrapped = A.adaptChangeRequestDetail({
    change_request: { provider: 'github', repository: 'r', external_id: '5' },
    timeline: {
      events: [
        { event_type: 'change_request.opened', occurred_at: '2026-08-17T08:05:00Z', actor: 'alice', summary: 'PR opened' },
        { event_type: 'change_request.merged', occurred_at: '2026-08-17T10:35:00Z', actor: 'bob', summary: 'PR merged' }
      ]
    }
  });
  assert(Array.isArray(wrapped.timeline), 'detail: timeline flattened to an array');
  assertEqual(wrapped.timeline.length, 2, 'detail: timeline array preserves every event');
  assertEqual(wrapped.timeline[0].event_type, 'change_request.opened', 'detail: first timeline event preserved');
  assertEqual(wrapped.timeline[1].event_type, 'change_request.merged', 'detail: last timeline event preserved');
  assertEqual(typeof wrapped.timeline.forEach, 'function',
    'detail: flattened timeline is iterable by the renderer (forEach)');

  // Empty events array stays an empty array (renderer shows the empty state).
  var emptyEvents = A.adaptChangeRequestDetail({
    change_request: { provider: 'github', repository: 'r', external_id: '6' },
    timeline: { events: [] }
  });
  assert(Array.isArray(emptyEvents.timeline), 'detail: empty events array stays an array');
  assertEqual(emptyEvents.timeline.length, 0, 'detail: empty events array length 0');

  // timeline null/undefined must not throw and must yield [] for the renderer.
  var nullTimeline = A.adaptChangeRequestDetail({
    change_request: { provider: 'github', repository: 'r', external_id: '7' },
    timeline: null
  });
  assert(Array.isArray(nullTimeline.timeline), 'detail: null timeline -> empty array (no throw)');
  assertEqual(nullTimeline.timeline.length, 0, 'detail: null timeline -> length 0');

  var absentTimeline = A.adaptChangeRequestDetail({
    change_request: { provider: 'github', repository: 'r', external_id: '8' }
  });
  assert(Array.isArray(absentTimeline.timeline), 'detail: absent timeline -> empty array (no throw)');
  assertEqual(absentTimeline.timeline.length, 0, 'detail: absent timeline -> length 0');

  // Defensive fallback: a legacy bare-array timeline is passed through as-is.
  var bare = A.adaptChangeRequestDetail({
    change_request: { provider: 'github', repository: 'r', external_id: '9' },
    timeline: [
      { event_type: 'change_request.opened', occurred_at: '2026-08-17T08:05:00Z', summary: 'PR opened' }
    ]
  });
  assert(Array.isArray(bare.timeline), 'detail: bare-array timeline accepted (legacy)');
  assertEqual(bare.timeline.length, 1, 'detail: bare-array timeline length preserved');
})();

// ── No browser-side join / purity guarantees ────────────────────────────

console.log('\u25B6 purity + no browser-side joins');

(function () {
  assert(typeof A.adaptChangeRequestSummary === 'function', 'adapter: summary function exported');
  assert(typeof A.adaptChangeRequestDetail === 'function', 'adapter: detail function exported');
  assert(typeof A.adaptExecution === 'function', 'adapter: execution function exported');
  assert(typeof A.fmtCrCost === 'function', 'adapter: cost formatter exported');
  assert(typeof A.providerStateBadgeClass === 'function', 'adapter: provider state badge exported');
  assert(typeof A.afkStateBadgeClass === 'function', 'adapter: afk state badge exported');

  // The module source must not contain DOM/fetch/join patterns.
  var source = require('fs').readFileSync(
    path.join(__dirname, '..', 'adapters', 'change_request_adapters.js'), 'utf8');
  assert(source.indexOf('document.') === -1, 'purity: no document access');
  assert(source.indexOf('getElementById') === -1, 'purity: no getElementById');
  assert(source.indexOf('innerHTML') === -1, 'purity: no innerHTML');
  assert(source.indexOf('fetch(') === -1, 'purity: no fetch');
  assert(source.indexOf('XMLHttpRequest') === -1, 'purity: no XHR');
  assert(source.indexOf('window.addEventListener') === -1, 'purity: no DOM listeners');
})();

// ── Summary ─────────────────────────────────────────────────────────────

console.log('');
console.log('\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550');
console.log('  Passed:', passed, ' / Failed:', failed);
console.log('\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550');
process.exit(failed > 0 ? 1 : 0);
