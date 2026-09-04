/**
 * Unit tests for the Change-request execution detail view (issue #614).
 *
 * Run with: node frontend/tests/issue_614_tests.js
 *
 * These tests exercise the PRODUCTION app.js and the #612 adapter module
 * through a vm sandbox in the same load order as the browser (adapters
 * module first, then app.js) — the rendered markup is the real production
 * code, not a copy.  No DOM, no network access, no provider credentials,
 * no AWX.
 *
 * Coverage (issue #614 acceptance criteria, updated for issue #652):
 *   - Detail header: provider identity, provider lifecycle status (PR Status
 *     / MR Status / MR/PR Status by provider), and AFK Run Cost first; the
 *     redundant AFK Automation badge is gone.
 *   - Execution records grouped by purpose (implementation / review /
 *     retry) as distinct sections under one change request.
 *   - Per-execution AWX job ID, status, outcome, timestamps, duration,
 *     linked session, token usage, and cost (incl. 'Cost unavailable').
 *   - Aggregate cost includes every available implementation, review, and
 *     retry execution usage exactly once.
 *   - Session links navigate to the existing Agent Run detail experience
 *     (closing the change-request overlay first).
 *   - Provenance/activity timeline present but collapsed by default.
 *   - The provider lifecycle state is the only header status badge.
 *   - Detail loading / not-found / partial-data / error states.
 */

'use strict';

var fs = require('fs');
var vm = require('vm');
var path = require('path');

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

function assertContains(haystack, needle, label) {
  assert(String(haystack).indexOf(needle) !== -1, label + ' (missing ' + JSON.stringify(needle) + ')');
}

function assertNotContains(haystack, needle, label) {
  assert(String(haystack).indexOf(needle) === -1, label + ' (unexpected ' + JSON.stringify(needle) + ')');
}

// ── Fixtures (deterministic, cross-provider parity) ─────────────────────

var githubFixture = require(path.join(__dirname, '..', 'fixtures', 'change_request_detail_github.js'));
var gitlabFixture = require(path.join(__dirname, '..', 'fixtures', 'change_request_detail_gitlab.js'));

// ── VM sandbox (mirrors the production load order) ──────────────────────

var elementRegistry = {};

function makeFakeElement(id) {
  var listeners = {};
  return {
    id: id,
    value: '',
    disabled: false,
    innerHTML: '',
    textContent: '',
    style: {},
    className: '',
    classList: {
      _classes: {},
      add: function (c) { this._classes[c] = true; },
      remove: function (c) { delete this._classes[c]; },
      toggle: function (c, force) {
        var on = (force === undefined) ? !this._classes[c] : !!force;
        if (on) { this._classes[c] = true; } else { delete this._classes[c]; }
        return on;
      },
      contains: function (c) { return !!this._classes[c]; }
    },
    addEventListener: function (type, fn) { listeners[type] = fn; },
    _handlers: listeners,
    querySelectorAll: function () { return []; },
    attributes: {},
    setAttribute: function (name, value) { this.attributes[name] = String(value); },
    getAttribute: function (name) {
      return (name in this.attributes) ? this.attributes[name] : null;
    },
    removeAttribute: function (name) { delete this.attributes[name]; }
  };
}

// Detail-body-aware fake: renders session links (.afk-session-clickable)
// as clickable fakes so the drill-down wiring can be driven like a real
// DOM would.
function makeCrDetailBody(id) {
  var el = makeFakeElement(id);
  el._linksHtml = null;
  el._linksCache = null;
  el.querySelectorAll = function (selector) {
    if (selector !== '.afk-session-clickable') return [];
    if (this._linksHtml === this.innerHTML && this._linksCache) return this._linksCache;
    var links = [];
    var linkRe = /<div[^>]*class="[^"]*\bafk-session-clickable\b[^"]*"[^>]*data-session-id="([^"]*)"[^>]*>/g;
    var m;
    while ((m = linkRe.exec(this.innerHTML)) !== null) {
      var link = makeFakeElement(id + '-link-' + links.length);
      link.setAttribute('data-session-id', m[1]);
      links.push(link);
    }
    this._linksHtml = this.innerHTML;
    this._linksCache = links;
    return links;
  };
  return el;
}

function buildSandbox(withAdapters) {
  var registry = {};
  Object.keys(elementRegistry).forEach(function (id) {
    registry[id] = elementRegistry[id];
  });
  var documentStub = {
    readyState: 'loading',
    querySelector: function () { return null; },
    getElementById: function (id) { return registry[id] || null; },
    querySelectorAll: function () { return []; },
    addEventListener: function () {},
    createElement: function (tag) {
      var el = { className: '', textContent: '', style: {} };
      Object.defineProperty(el, 'outerHTML', {
        get: function () {
          return '<' + tag + ' class="' + el.className + '">' + el.textContent + '</' + tag + '>';
        }
      });
      return el;
    }
  };
  var sandboxWindow = {};
  var sandbox = {
    window: sandboxWindow,
    document: documentStub,
    console: {
      log: console.log.bind(console),
      error: console.error.bind(console),
      warn: console.warn.bind(console)
    },
    setTimeout: setTimeout,
    setInterval: setInterval,
    clearInterval: clearInterval,
    clearTimeout: clearTimeout,
    fetch: function (url) { return fetchImpl(url); },
    location: { href: '', search: '', pathname: '' },
    history: { pushState: function () {}, replaceState: function () {} },
    URLSearchParams: URLSearchParams,
    navigator: {}
  };
  sandbox.window = sandboxWindow;
  vm.createContext(sandbox);
  var adaptersSource = fs.readFileSync(
    path.join(__dirname, '..', 'adapters', 'change_request_adapters.js'), 'utf8');
  var appJsSource = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
  if (withAdapters) {
    vm.runInContext(adaptersSource, sandbox, { filename: 'change_request_adapters.js' });
  }
  vm.runInContext(appJsSource, sandbox, { filename: 'app.js' });
  return { sandbox: sandbox, window: sandboxWindow };
}

// Mutable fetch implementation shared by the sandbox and the tests.
var fetchImpl = function () {
  return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } });
};

function okJson(data) {
  return Promise.resolve({ ok: true, json: function () { return Promise.resolve(data); } });
}

function settle() {
  return new Promise(function (resolve) { setImmediate(resolve); });
}

function settleDeep(times) {
  var p = Promise.resolve();
  for (var i = 0; i < times; i++) {
    p = p.then(settle);
  }
  return p;
}

// ── Sandbox setup ────────────────────────────────────────────────────────

var crDetailOverlayEl = makeFakeElement('cr-list-detail-overlay');
var crDetailTitleEl = makeFakeElement('cr-list-detail-title');
var crDetailBodyEl = makeCrDetailBody('cr-list-detail-body');
var crDetailCloseEl = makeFakeElement('cr-list-detail-close');
var arDetailOverlayEl = makeFakeElement('ar-detail-overlay');
var arDetailTitleEl = makeFakeElement('ar-detail-title');
var arDetailBodyEl = makeFakeElement('ar-detail-body');
var arDetailCloseEl = makeFakeElement('ar-detail-close');
elementRegistry['cr-list-detail-overlay'] = crDetailOverlayEl;
elementRegistry['cr-list-detail-title'] = crDetailTitleEl;
elementRegistry['cr-list-detail-body'] = crDetailBodyEl;
elementRegistry['cr-list-detail-close'] = crDetailCloseEl;
elementRegistry['ar-detail-overlay'] = arDetailOverlayEl;
elementRegistry['ar-detail-title'] = arDetailTitleEl;
elementRegistry['ar-detail-body'] = arDetailBodyEl;
elementRegistry['ar-detail-close'] = arDetailCloseEl;

var main = buildSandbox(true);
var W = main.window;
var sandbox = main.sandbox;

// ── Test runner ──────────────────────────────────────────────────────────

var tests = [];
function test(name, fn) { tests.push({ name: name, fn: fn }); }

function runTests() {
  return tests.reduce(function (p, t) {
    return p.then(function () {
      console.log('\u25B6 ' + t.name);
      return Promise.resolve(t.fn());
    });
  }, Promise.resolve()).then(function () {
    console.log('');
    console.log('\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550');
    console.log('  Passed:', passed, ' / Failed:', failed);
    console.log('\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550');
    process.exit(failed > 0 ? 1 : 0);
  });
}

// ═════════════════════════════════════════════════════════════════════════
// Header: identity + dual statuses + aggregate cost (GitHub)
// ═════════════════════════════════════════════════════════════════════════

test('header: identity, lifecycle status, and AFK Run Cost render first (GitHub, issue #652)', function () {
  W.renderChangeRequestDetail(githubFixture.buildDetail());
  var html = crDetailBodyEl.innerHTML;
  assertContains(html, 'acme/web-app#142', 'header: display identity rendered');
  assertContains(html, 'feat: wire up web-app dashboard', 'header: title rendered');
  assertContains(html, 'badge-merged', 'header: provider lifecycle state badge');
  assertContains(html, 'AFK Run Cost', 'header: Gateway-owned AFK Run Cost label rendered');
  assertContains(html, '$6.35', 'header: AFK Run Cost value rendered');
  assertNotContains(html, '>Total Cost<', 'header: legacy Total Cost label retired');
  // Issue #652: the lifecycle badge is labeled PR Status for GitHub and the
  // redundant AFK Automation badge is gone.
  assertContains(html, '>PR Status<', 'header: GitHub lifecycle status label PR Status');
  assertNotContains(html, '>Provider<', 'header: legacy generic Provider label retired');
  assertNotContains(html, '>AFK Automation<', 'header: AFK Automation badge removed');
  // The header block carries exactly one lifecycle badge (merged) — the
  // automation completed badge must not appear within the header markup
  // (execution rows below legitimately keep their own badge-completed).
  var headerBlock = html.slice(html.indexOf('afk-cr-detail-header'), html.indexOf('afk-cr-section'));
  assertNotContains(headerBlock, 'badge-completed',
    'header: automation completed badge removed from the detail header');
});

test('header: GitLab parity — MR Status label, no AFK Automation badge (issue #652)', function () {
  W.renderChangeRequestDetail(gitlabFixture.buildDetail());
  var html = crDetailBodyEl.innerHTML;
  assertContains(html, 'group/cloudnative#6', 'header: GitLab MR identity');
  assertContains(html, 'badge-merged', 'header: GitLab provider lifecycle state badge');
  assertContains(html, '>MR Status<', 'header: GitLab lifecycle status label MR Status');
  assertContains(html, 'AFK Run Cost', 'header: GitLab AFK Run Cost label');
  assertContains(html, '$4.80', 'header: GitLab AFK Run Cost value');
  assertNotContains(html, '>AFK Automation<', 'header: GitLab AFK Automation badge removed');
  var headerBlock = html.slice(html.indexOf('afk-cr-detail-header'), html.indexOf('afk-cr-section'));
  assertNotContains(headerBlock, 'badge-completed',
    'header: GitLab automation completed badge removed from the detail header');
});

// ═════════════════════════════════════════════════════════════════════════
// Execution grouping by purpose
// ═════════════════════════════════════════════════════════════════════════

test('executions: grouped by implementation / review / retry purpose', function () {
  W.renderChangeRequestDetail(githubFixture.buildDetail());
  var html = crDetailBodyEl.innerHTML;
  assertContains(html, 'Executions (5)', 'executions: total count in section title');
  assertContains(html, '>Implementation<', 'executions: implementation group present');
  assertContains(html, '>Review<', 'executions: review group present');
  assertContains(html, '>Retry<', 'executions: retry group present');
  // Each execution entry is distinct (duplicate attempts preserved).
  assertContains(html, 'awx-9001', 'executions: implementation awx job 1');
  assertContains(html, 'awx-9002', 'executions: failed implementation attempt retained');
  assertContains(html, 'awx-9004', 'executions: retry awx job');
  assertContains(html, 'awx-9003', 'executions: review awx job');
  assertContains(html, 'awx-9005', 'executions: cancelled review awx job');
});

test('executions: unknown-purpose executions preserved under Other', function () {
  W.renderChangeRequestDetail({
    change_request: { provider: 'github', repository: 'r/x', external_id: '1', provider_state: 'open', automation_state: 'running' },
    executions: [
      { awx_job: { job_id: 'ow-1' }, purpose: 'some-future-purpose', outcome: 'running' }
    ]
  });
  var html = crDetailBodyEl.innerHTML;
  assertContains(html, '>Other<', 'executions: unknown purpose preserved under Other');
  assertContains(html, 'ow-1', 'executions: unknown-purpose job id rendered');
});

test('executions: Gateway order preserved within each purpose group', function () {
  // The Gateway owns execution ordering (newest activity first per the
  // detail contract); the renderer must NOT re-sort — a failed first
  // attempt stays first in its group, with the retry after it.
  W.renderChangeRequestDetail({
    change_request: { provider: 'github', repository: 'r/x', external_id: '2', provider_state: 'open', automation_state: 'running' },
    executions: [
      { awx_job: { job_id: 'first-attempt' }, purpose: 'implementation', outcome: 'failed', started_at: '2026-08-01T09:00:00Z', finished_at: '2026-08-01T09:10:00Z' },
      { awx_job: { job_id: 'second-attempt' }, purpose: 'implementation', outcome: 'completed', started_at: '2026-08-01T09:20:00Z', finished_at: '2026-08-01T09:40:00Z' }
    ]
  });
  var html = crDetailBodyEl.innerHTML;
  var firstIdx = html.indexOf('first-attempt');
  var secondIdx = html.indexOf('second-attempt');
  assert(firstIdx !== -1 && secondIdx !== -1 && firstIdx < secondIdx,
    'executions: failed first attempt rendered before the retry (order preserved)');
  assertContains(html, 'badge-failed', 'executions: first attempt failed badge');
  assertContains(html, 'badge-completed', 'executions: retry completed badge');
});

// ═════════════════════════════════════════════════════════════════════════
// Per-execution metadata: AWX job ID, status, outcome, timestamps, tokens
// ═════════════════════════════════════════════════════════════════════════

test('execution: AWX job metadata, status, outcome, timestamps, tokens, cost', function () {
  W.renderChangeRequestDetail(githubFixture.buildDetail());
  var html = crDetailBodyEl.innerHTML;
  // AWX job metadata
  assertContains(html, 'awx-9001', 'exec: awx job id');
  assertContains(html, 'template: tpl-implement', 'exec: job template id rendered');
  assertContains(html, 'trigger: eda', 'exec: trigger type rendered');
  assertContains(html, 'branch: feat/web-app-dashboard', 'exec: branch rendered');
  // Status + outcome badges (status == outcome here, so outcome badge hidden
  // to avoid duplication; status badge present)
  assertContains(html, 'badge-completed', 'exec: status badge rendered');
  // Timestamps + duration
  assertContains(html, 'started', 'exec: started timestamp rendered');
  assertContains(html, 'finished', 'exec: finished timestamp rendered');
  assertContains(html, 'duration: 40m', 'exec: duration computed');
  // Per-execution AWX Execution Cost (Gateway-computed subtotal — issue #629)
  assertContains(html, 'AWX Execution Cost: $2.10', 'exec: AWX Execution Cost subtotal rendered');
  // The retired 'cost:' label must not appear in any EXECUTION block (session
  // links legitimately keep their own session-cost 'cost:' line).
  var execBlocks = html.split('afk-cr-execution-meta').slice(1);
  var retiredInExec = execBlocks.some(function (b) {
    var meta = b.split('</div>')[0];
    return / &middot; cost: /.test(meta);
  });
  assert(!retiredInExec, 'exec: legacy per-run cost label retired from execution meta');
  // Token usage via compact Token Breakdown
  assertContains(html, '13.8K total', 'exec: token breakdown total');
  assertContains(html, '8.0K in | 4.0K out', 'exec: token breakdown input/output');
  assertContains(html, '1.2K cache read + 600 cache write', 'exec: token breakdown cache line');
});

test('execution: missing cost telemetry renders Cost unavailable', function () {
  var html = W.renderChangeRequestExecution({
    awxJobId: 'awx-x1',
    purpose: { value: 'implementation', label: 'Implementation', badgeClass: 'badge-completed' },
    status: { value: 'completed', label: 'completed', badgeClass: 'badge-completed' },
    outcome: 'completed',
    tokens: {},
    duration: '--',
    cost: { available: false, usd: null, label: 'Cost unavailable' }
  });
  assertContains(html, 'Cost unavailable', 'exec: missing cost renders Cost unavailable');
  assertNotContains(html, '$0.00', 'exec: missing cost never renders as zero');
});

test('execution: distinct outcome badge renders when different from status', function () {
  // status 'stale' with outcome 'failed' → both badges visible.
  var html = W.renderChangeRequestExecution({
    awxJobId: 'awx-x2',
    purpose: { value: 'review', label: 'Review', badgeClass: 'badge-open' },
    status: { value: 'stale', label: 'stale', badgeClass: 'badge-stale' },
    outcome: 'failed',
    tokens: {},
    duration: '--',
    cost: { available: false, usd: null, label: 'Cost unavailable' }
  });
  assertContains(html, 'badge-stale', 'exec: status badge');
  assertContains(html, 'badge-failed', 'exec: distinct outcome badge rendered');
});

// ═════════════════════════════════════════════════════════════════════════
// AFK Run Cost vs AWX Execution Cost (issue #629)
// ═════════════════════════════════════════════════════════════════════════

test('run cost: gateway-owned AFK Run Cost wins over per-execution sum', function () {
  W.renderChangeRequestDetail(githubFixture.buildDetail());
  var html = crDetailBodyEl.innerHTML;
  assertContains(html, '$6.35', 'run cost: Gateway-owned AFK Run Cost rendered');
  // The run total equals the sum of all available per-execution subtotals
  // exactly once: 2.10 (impl) + 0.85 (failed impl) + 0.75 (retry) + 1.90
  // (review) = 5.60 — the cancelled review has no cost telemetry.  The
  // browser must never derive that sum itself; it displays the Gateway value.
  assertNotContains(html, '$5.60', 'run cost: per-execution sum not substituted when gateway total exists');
});

test('run cost: missing gateway total is unavailable, never a per-execution sum', function () {
  W.renderChangeRequestDetail({
    change_request: { provider: 'github', repository: 'r/x', external_id: '2', provider_state: 'open', automation_state: 'running' },
    executions: [
      { awx_job: { job_id: '1' }, purpose: 'implementation', outcome: 'completed', estimated_cost_usd: 0.30 },
      { awx_job: { job_id: '2' }, purpose: 'review', outcome: 'completed', estimated_cost_usd: 0.20 },
      { awx_job: { job_id: '3' }, purpose: 'retry', outcome: 'failed', estimated_cost_usd: null },
      { awx_job: { job_id: '4' }, purpose: 'implementation', outcome: 'completed', estimated_cost_usd: 0.10 }
    ]
  });
  var html = crDetailBodyEl.innerHTML;
  // The Gateway AFK Run Cost is authoritative: without it the run total is
  // unavailable (null) — the adapter never invents a browser-side sum that
  // could double-count cost (issue #617 review finding HIGH-1).
  assertNotContains(html, '$0.60', 'run cost: per-execution sum not substituted when gateway total missing');
  assertContains(html, 'Cost unavailable', 'run cost: unavailable run total labeled');
  // Known lower-level costs stay visible alongside the unavailable total.
  assertContains(html, 'AWX Execution Cost: $0.30', 'run cost: known execution subtotal stays visible');
  assertContains(html, 'AWX Execution Cost: $0.20', 'run cost: second known subtotal stays visible');
  assertContains(html, 'AWX Execution Cost: $0.10', 'run cost: third known subtotal stays visible');
  assertContains(html, 'AWX Execution Cost: Cost unavailable', 'run cost: unknown subtotal labeled, never zero');
  assertNotContains(html, 'AWX Execution Cost: $0.00', 'run cost: unknown subtotal never coerced to zero');
});

test('run cost: header shows AFK Run Cost while executions show AWX Execution Cost', function () {
  W.renderChangeRequestDetail(githubFixture.buildDetail());
  var html = crDetailBodyEl.innerHTML;
  // The two cost scopes are distinct and visibly labeled (issue #629):
  // the header carries the lifecycle-total (AFK Run Cost); each execution
  // carries its own AWX Execution Cost subtotal.  Neither is derived in the
  // browser from the other.
  assertContains(html, 'AFK Run Cost', 'cost scopes: AFK Run Cost in the header');
  assertContains(html, 'AWX Execution Cost', 'cost scopes: AWX Execution Cost on executions');
  assertContains(html, '$6.35', 'cost scopes: run total rendered');
  assertContains(html, 'AWX Execution Cost: $2.10', 'cost scopes: execution subtotal rendered');
});

test('run cost: one unknown session cost renders unavailable total, subtotals stay visible', function () {
  // A run whose sessions include one unknown cost has NO authoritative run
  // total (the Gateway poisons the aggregate to null) — the UI must not
  // display a partial total, and must not coerce anything to $0.00.
  W.renderChangeRequestDetail({
    change_request: { provider: 'gitlab', repository: 'g/p', external_id: '7', provider_state: 'merged', automation_state: 'completed' },
    total_estimated_cost_usd: null,
    executions: [
      { awx_job: { job_id: 'a1' }, purpose: 'implementation', outcome: 'completed', estimated_cost_usd: 1.25 },
      { awx_job: { job_id: 'a2' }, purpose: 'review', outcome: 'completed', estimated_cost_usd: null }
    ]
  });
  var html = crDetailBodyEl.innerHTML;
  assertContains(html, 'AFK Run Cost', 'partial run: header still labeled AFK Run Cost');
  assertContains(html, 'Cost unavailable', 'partial run: unavailable run total labeled');
  assertNotContains(html, '$1.25 total', 'partial run: no partial authoritative total fabricated');
  assertContains(html, 'AWX Execution Cost: $1.25', 'partial run: known subtotal visible');
  assertContains(html, 'AWX Execution Cost: Cost unavailable', 'partial run: unknown subtotal visible');
});

test('run cost: all cost data missing renders Cost unavailable everywhere', function () {
  W.renderChangeRequestDetail({
    change_request: { provider: 'github', repository: 'r/x', external_id: '8', provider_state: 'open', automation_state: 'running' },
    executions: [
      { awx_job: { job_id: 'b1' }, purpose: 'implementation', outcome: 'completed' },
      { awx_job: { job_id: 'b2' }, purpose: 'review', outcome: 'failed' }
    ]
  });
  var html = crDetailBodyEl.innerHTML;
  assertContains(html, 'AFK Run Cost', 'all missing: header label present');
  assertContains(html, 'AWX Execution Cost: Cost unavailable', 'all missing: execution subtotals labeled');
  assertNotContains(html, '$0.00', 'all missing: no coerced zero anywhere');
});

test('run cost: legacy aggregateCost alias maps to the same gateway run total', function () {
  // Legacy payloads carry no #629 vocabulary; the legacy `aggregateCost`
  // view-model alias must expose the SAME Gateway-owned run total — never a
  // recomputed or independently summed value.
  var legacy = {
    change_request: { provider: 'github', repository: 'r/x', external_id: '9', provider_state: 'merged', automation_state: 'completed' },
    total_estimated_cost_usd: 3.75,
    executions: [
      { awx_job: { job_id: 'c1' }, purpose: 'implementation', outcome: 'completed', estimated_cost_usd: 3.75 },
      { awx_job: { job_id: 'c2' }, purpose: 'review', outcome: 'completed', estimated_cost_usd: 1.00 }
    ]
  };
  W.renderChangeRequestDetail(legacy);
  var html = crDetailBodyEl.innerHTML;
  assertContains(html, '$3.75', 'legacy: gateway run total rendered as-is');
  assertNotContains(html, '$4.75', 'legacy: no browser-side sum of execution subtotals');
});

// ═════════════════════════════════════════════════════════════════════════
// Session links → Agent Run drill-down
// ═════════════════════════════════════════════════════════════════════════

test('sessions: linked sessions render with drill-down links', function () {
  W.renderChangeRequestDetail(githubFixture.buildDetail());
  var html = crDetailBodyEl.innerHTML;
  assertContains(html, 'Sessions (3)', 'sessions: count in section title');
  assertContains(html, 'data-session-id=', 'sessions: drill-down link attribute present');
  assertContains(html, '2e0f1a2b-0000-4000-8000-000000000142', 'sessions: internal session id');
  assertContains(html, 'open run', 'sessions: open-run affordance rendered');
});

test('sessions: click closes the change-request overlay and opens Agent Run detail', function () {
  crDetailOverlayEl.classList.add('visible');
  W.renderChangeRequestDetail(githubFixture.buildDetail());
  var links = crDetailBodyEl.querySelectorAll('.afk-session-clickable');
  assert(links.length >= 1, 'sessions: at least one clickable session link');
  var sid = links[0].getAttribute('data-session-id');
  assert(sid && sid.length > 0, 'sessions: first link carries a session id');
  links[0]._handlers.click();
  return settleDeep(3).then(function () {
    assert(!crDetailOverlayEl.classList.contains('visible'),
      'sessions: change-request overlay closed before drill-down');
    assert(arDetailOverlayEl.classList.contains('visible'),
      'sessions: Agent Run detail overlay opened');
  });
});

test('sessions: session without internal id is not clickable', function () {
  W.renderChangeRequestDetail({
    change_request: { provider: 'github', repository: 'r/x', external_id: '3', provider_state: 'open', automation_state: 'running' },
    sessions: [
      { external_session_id: 'ses-no-uuid', agent: 'code-editor-junior', inferred: true }
    ]
  });
  var html = crDetailBodyEl.innerHTML;
  assertNotContains(html, 'data-session-id=', 'sessions: no internal id → no drill-down attribute');
  assertContains(html, 'ses-no-uuid', 'sessions: external id still rendered');
});

// ═════════════════════════════════════════════════════════════════════════
// Provenance / activity timeline (collapsed by default)
// ═════════════════════════════════════════════════════════════════════════

test('timeline: present but collapsed by default', function () {
  W.renderChangeRequestDetail(githubFixture.buildDetail());
  var html = crDetailBodyEl.innerHTML;
  assertContains(html, 'Activity timeline', 'timeline: section present');
  assertContains(html, 'change_request.opened', 'timeline: event type rendered');
  assertContains(html, 'change_request.merged', 'timeline: merged event rendered');
  assertContains(html, 'PR merged', 'timeline: event summary rendered');
  assertNotContains(html, '<details class="afk-cr-timeline" open', 'timeline: collapsed by default');
});

test('timeline: empty data renders empty state', function () {
  W.renderChangeRequestDetail({
    change_request: { provider: 'github', repository: 'r/x', external_id: '4', provider_state: 'open', automation_state: 'running' },
    executions: [],
    sessions: []
  });
  var html = crDetailBodyEl.innerHTML;
  assertContains(html, 'No timeline data', 'timeline: empty state rendered');
});

// ═════════════════════════════════════════════════════════════════════════
// State handling: loading / not-found / partial / error
// ═════════════════════════════════════════════════════════════════════════

test('openChangeRequestDetail: loading state then not-found state', function () {
  fetchImpl = function (url) {
    return Promise.reject(new Error('API ' + url + ' returned 404'));
  };
  crDetailOverlayEl.classList.remove('visible');
  return W.openChangeRequestDetail('github', 'acme/web-app', '999').then(function () {
    assertContains(crDetailBodyEl.innerHTML, 'Change request not found', 'not-found: distinct empty state');
  });
});

test('openChangeRequestDetail: generic error state renders', function () {
  fetchImpl = function () {
    return Promise.reject(new Error('network down'));
  };
  return W.openChangeRequestDetail('github', 'acme/web-app', '142').then(function () {
    assertContains(crDetailBodyEl.innerHTML, 'Failed to load change-request detail', 'error: generic failure message');
  });
});

test('detail: partial data renders unavailable cost and empty sections', function () {
  W.renderChangeRequestDetail({
    change_request: { provider: 'gitlab', repository: 'g/p', external_id: '3', provider_state: 'open', automation_state: 'failed' },
    executions: [],
    sessions: []
  });
  var html = crDetailBodyEl.innerHTML;
  assertContains(html, 'g/p#3', 'partial: identity rendered');
  assertContains(html, 'badge-open', 'partial: provider lifecycle badge');
  assertContains(html, 'MR Status', 'partial: GitLab lifecycle status label');
  assertNotContains(html, 'badge-failed', 'partial: automation failed badge not rendered (issue #652)');
  assertNotContains(html, 'AFK Automation', 'partial: no AFK Automation badge (issue #652)');
  assertContains(html, 'Cost unavailable', 'partial: missing run cost renders Cost unavailable');
  assertContains(html, 'No linked executions', 'partial: empty executions section');
  assertContains(html, 'No sessions linked', 'partial: empty sessions section');
  assertContains(html, 'No timeline data', 'partial: empty timeline section');
});

// ═════════════════════════════════════════════════════════════════════════
// Status combinations + lifecycle-only independence (issue #652)
// ═════════════════════════════════════════════════════════════════════════

test('status combinations: provider merged renders merged lifecycle badge only', function () {
  W.renderChangeRequestDetail(githubFixture.buildDetail());
  var html = crDetailBodyEl.innerHTML;
  assertContains(html, 'badge-merged', 'status combo: provider merged');
  var headerBlock = html.slice(html.indexOf('afk-cr-detail-header'), html.indexOf('afk-cr-section'));
  assertNotContains(headerBlock, 'badge-completed',
    'status combo: automation completed badge not rendered in the header');
});

test('status combinations: provider open renders open lifecycle badge only', function () {
  W.renderChangeRequestDetail({
    change_request: { provider: 'github', repository: 'r/x', external_id: '5', provider_state: 'open', automation_state: 'running' },
    executions: [],
    sessions: []
  });
  var html = crDetailBodyEl.innerHTML;
  assertContains(html, 'badge-open', 'status combo: provider open');
  assertNotContains(html, 'badge-running', 'status combo: automation running badge not rendered');
});

test('status combinations: provider closed renders closed lifecycle badge only', function () {
  W.renderChangeRequestDetail({
    change_request: { provider: 'github', repository: 'r/x', external_id: '6', provider_state: 'closed', automation_state: 'failed' },
    executions: [],
    sessions: []
  });
  var html = crDetailBodyEl.innerHTML;
  assertContains(html, 'badge-closed', 'status combo: provider closed');
  assertNotContains(html, 'badge-failed', 'status combo: automation failed badge not rendered');
});

test('status label: unknown provider falls back to MR/PR Status (issue #652)', function () {
  W.renderChangeRequestDetail({
    change_request: { repository: 'r/x', external_id: '11', provider_state: 'open' },
    executions: [],
    sessions: []
  });
  var html = crDetailBodyEl.innerHTML;
  assertContains(html, '>MR/PR Status<', 'unknown provider: lifecycle status label MR/PR Status');
  assertContains(html, 'badge-open', 'unknown provider: lifecycle badge still rendered');
});

test('overlay lifecycle: close button hides the overlay', function () {
  W.setupAfkOutcomesEventHandlers();
  crDetailOverlayEl.classList.add('visible');
  crDetailCloseEl._handlers.click();
  assert(!crDetailOverlayEl.classList.contains('visible'), 'overlay: close button hides overlay');
});

// ── Run ─────────────────────────────────────────────────────────────────

runTests();
