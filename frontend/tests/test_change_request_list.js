/**
 * Unit tests for the Change Request list view (issue #613).
 *
 * Run with: node frontend/tests/test_change_request_list.js
 *
 * These tests exercise the PRODUCTION app.js and the #612 adapter module
 * through a vm sandbox in the same load order as the browser (adapters
 * module first, then app.js) — the rendered markup and the URL builders are
 * the real production code, not a copy.  No DOM, no network access, no
 * provider credentials, no AWX.
 *
 * Coverage: summary list rendering (one row per change request), GitHub/
 * GitLab parity (PR vs MR), dual statuses, cost display (USD and
 * 'Cost unavailable'), contract-served filtering, ordering preservation,
 * identity-keyed selection, detail flow, and loading/empty/stale/error
 * state handling.
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

var githubFixture = require(path.join(__dirname, '..', 'fixtures', 'change_request_summary_github.js'));
var gitlabFixture = require(path.join(__dirname, '..', 'fixtures', 'change_request_summary_gitlab.js'));

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

// Row-aware tbody fake: parses the rendered <tr data-provider=...> rows into
// clickable fakes so the selection wiring (renderChangeRequestSummaryTable)
// can be driven the same way a real table would be.
function makeCrListTbody(id) {
  var el = makeFakeElement(id);
  el._rowsHtml = null;
  el._rowsCache = null;
  el.querySelectorAll = function (selector) {
    if (selector !== '.afk-cr-list-row') return [];
    if (this._rowsHtml === this.innerHTML && this._rowsCache) return this._rowsCache;
    var rows = [];
    var rowRe = /<tr\b([^>]*)>[\s\S]*?<\/tr>/g;
    var m;
    while ((m = rowRe.exec(this.innerHTML)) !== null) {
      var row = makeFakeElement(id + '-row-' + rows.length);
      var attrRe = /data-(provider|repository|external-id)="([^"]*)"/g;
      var am;
      while ((am = attrRe.exec(m[1])) !== null) {
        row.setAttribute('data-' + am[1], am[2]);
      }
      rows.push(row);
    }
    this._rowsHtml = this.innerHTML;
    this._rowsCache = rows;
    return rows;
  };
  return el;
}

// Button-aware pagination nav fake: parses the rendered pagination buttons
// (<button data-page=N ...>) into clickable fakes so the pagination wiring
// (renderChangeRequestPagination) can be driven like a real <nav>.
function makePaginationNav(id) {
  var el = makeFakeElement(id);
  el._buttonsHtml = null;
  el._buttonsCache = null;
  el.querySelectorAll = function (selector) {
    if (selector !== 'button') return [];
    if (this._buttonsHtml === this.innerHTML && this._buttonsCache) return this._buttonsCache;
    var buttons = [];
    var btnRe = /<button\b([^>]*)>/g;
    var m;
    while ((m = btnRe.exec(this.innerHTML)) !== null) {
      var btn = makeFakeElement(id + '-btn-' + buttons.length);
      var attrRe = /(data-page|aria-label|aria-current)="([^"]*)"/g;
      var am;
      while ((am = attrRe.exec(m[1])) !== null) {
        if (am[1] === 'data-page') {
          btn.setAttribute('data-page', am[2]);
        } else if (am[1] === 'aria-current') {
          btn.setAttribute('aria-current', am[2]);
        } else {
          btn.setAttribute('aria-label', am[2]);
        }
      }
      if (/disabled/.test(m[1])) btn.disabled = true;
      buttons.push(btn);
    }
    this._buttonsHtml = this.innerHTML;
    this._buttonsCache = buttons;
    return buttons;
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

var crListTbodyEl = makeCrListTbody('afk-cr-list-tbody');
var crPaginationNavEl = makePaginationNav('afk-cr-pagination');
var crFilterProviderEl = makeFakeElement('afk-cr-filter-provider');
var crFilterRepositoryEl = makeFakeElement('afk-cr-filter-repository');
var crFilterProviderStateEl = makeFakeElement('afk-cr-filter-provider-state');
var crFilterAutomationStateEl = makeFakeElement('afk-cr-filter-automation-state');
var crFilterApplyEl = makeFakeElement('afk-cr-filter-apply');
var crFilterClearEl = makeFakeElement('afk-cr-filter-clear');
var crFreshnessEl = makeFakeElement('freshness-afk-cr-list');
var crDetailOverlayEl = makeFakeElement('cr-list-detail-overlay');
var crDetailTitleEl = makeFakeElement('cr-list-detail-title');
var crDetailBodyEl = makeFakeElement('cr-list-detail-body');
var crDetailCloseEl = makeFakeElement('cr-list-detail-close');
var afkRunsTbodyEl = makeFakeElement('afk-runs-tbody');
elementRegistry['afk-cr-list-tbody'] = crListTbodyEl;
elementRegistry['afk-cr-pagination'] = crPaginationNavEl;
elementRegistry['afk-cr-filter-provider'] = crFilterProviderEl;
elementRegistry['afk-cr-filter-repository'] = crFilterRepositoryEl;
elementRegistry['afk-cr-filter-provider-state'] = crFilterProviderStateEl;
elementRegistry['afk-cr-filter-automation-state'] = crFilterAutomationStateEl;
elementRegistry['afk-cr-filter-apply'] = crFilterApplyEl;
elementRegistry['afk-cr-filter-clear'] = crFilterClearEl;
elementRegistry['freshness-afk-cr-list'] = crFreshnessEl;
elementRegistry['cr-list-detail-overlay'] = crDetailOverlayEl;
elementRegistry['cr-list-detail-title'] = crDetailTitleEl;
elementRegistry['cr-list-detail-body'] = crDetailBodyEl;
elementRegistry['cr-list-detail-close'] = crDetailCloseEl;
elementRegistry['afk-runs-tbody'] = afkRunsTbodyEl;

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
// URL builders + identity key (contract-served filtering)
// ═════════════════════════════════════════════════════════════════════════

test('URL builder: empty filters emit only the activity window + limit', function () {
  var url = W.buildChangeRequestListUrl({}, { preset: 'custom', customStartDate: '2026-08-01', customEndDate: '2026-08-15' }, 100);
  assert(url.indexOf('/api/v1/afk-outcomes/change-requests?') === 0, 'URL: correct endpoint prefix');
  assertNotContains(url, 'provider=', 'URL: no provider param when empty');
  assertNotContains(url, 'repository=', 'URL: no repository param when empty');
  assertNotContains(url, 'provider_state=', 'URL: no provider_state param when empty');
  assertNotContains(url, 'automation_state=', 'URL: no automation_state param when empty');
  assertContains(url, 'activity_from=2026-08-01T00%3A00%3A00.000Z', 'URL: activity_from from the shared date range');
  assertContains(url, 'activity_to=2026-08-15T23%3A59%3A59.000Z', 'URL: activity_to includes the full selected day');
  assertContains(url, 'limit=100', 'URL: default limit applied');
});

test('URL builder: filters map to the summary contract query params', function () {
  var url = W.buildChangeRequestListUrl(
    { provider: 'gitlab', repository: 'group/cloudnative', providerState: 'open', automationState: 'failed' },
    { preset: 'custom', customStartDate: '2026-08-01', customEndDate: '2026-08-15' },
    50, 10
  );
  assertContains(url, 'provider=gitlab', 'URL: provider filter');
  assertContains(url, 'repository=' + encodeURIComponent('group/cloudnative'), 'URL: repository filter (encoded)');
  assertContains(url, 'provider_state=open', 'URL: provider-state filter');
  assertContains(url, 'automation_state=failed', 'URL: automation-state filter');
  assertContains(url, 'limit=50', 'URL: explicit limit');
  assertContains(url, 'offset=10', 'URL: explicit offset');
});

test('detail path: keyed by change-request identity, never an AFK Run ID', function () {
  var path = W.buildChangeRequestDetailPath('github', 'acme/web-app', '142');
  assertEqual(path, '/api/v1/afk-outcomes/change-requests/github/acme%2Fweb-app/142',
    'detail path: provider/repository/external-id segments');
  assertNotContains(path, 'afk_run_id', 'detail path: no internal AFK Run ID in the navigation key');
  assertEqual(W.changeRequestKey('github', 'acme/web-app', '142'), 'github/acme/web-app/142',
    'identity key: flat provider/repository/external-id tuple');
});

// ═════════════════════════════════════════════════════════════════════════
// Summary list rendering — GitHub
// ═════════════════════════════════════════════════════════════════════════

test('summary table: one row per change request (GitHub)', function () {
  W.renderChangeRequestSummaryTable(githubFixture.buildSummaryList());
  var html = crListTbodyEl.innerHTML;
  var rows = crListTbodyEl.querySelectorAll('.afk-cr-list-row');
  assertEqual(rows.length, 3, 'summary table: three change requests, three rows (not one row per AFK run)');
  assertContains(html, 'data-provider="github"', 'summary table: provider identity carried on the row');
  assertContains(html, 'acme/web-app', 'summary table: repository visible');
  assertContains(html, 'PR #142', 'summary table: provider-specific PR identity');
  assertContains(html, 'badge-provider', 'summary table: provider badge');
});

test('summary table: dual statuses render independently (GitHub)', function () {
  W.renderChangeRequestSummaryTable(githubFixture.buildSummaryList());
  var html = crListTbodyEl.innerHTML;
  assertContains(html, 'badge-merged', 'dual statuses: merged provider state badge');
  assertContains(html, 'badge-completed', 'dual statuses: completed AFK automation badge');
  assertContains(html, 'badge-open', 'dual statuses: open provider state badge');
  assertContains(html, 'badge-running', 'dual statuses: running AFK automation badge');
  assertContains(html, 'badge-closed', 'dual statuses: closed provider state badge');
  assertContains(html, 'badge-failed', 'dual statuses: failed AFK automation badge');
  assert(html.indexOf('badge-merged') !== html.indexOf('badge-completed'),
    'dual statuses: provider state and AFK automation state are distinct badges');
});

test('summary table: cost display — USD and Cost unavailable', function () {
  W.renderChangeRequestSummaryTable(githubFixture.buildSummaryList());
  var html = crListTbodyEl.innerHTML;
  assertContains(html, '$4.85', 'cost: known USD amount rendered');
  assertContains(html, '$1.25', 'cost: second known USD amount rendered');
  assertContains(html, 'Cost unavailable', 'cost: missing telemetry renders Cost unavailable');
  assertNotContains(html, '$0.00', 'cost: missing telemetry never renders as zero');
});

test('summary table: order preserved (Gateway owns newest-activity-first ordering)', function () {
  W.renderChangeRequestSummaryTable(githubFixture.buildSummaryList());
  var html = crListTbodyEl.innerHTML;
  assert(html.indexOf('#142') !== -1 && html.indexOf('#138') !== -1 && html.indexOf('#7') !== -1,
    'ordering: all three rows present');
  assert(html.indexOf('#142') < html.indexOf('#138'), 'ordering: newest linked activity first');
  assert(html.indexOf('#138') < html.indexOf('#7'), 'ordering: contract order preserved (no browser re-sort)');
});

test('summary table: latest linked activity rendered; null renders --', function () {
  W.renderChangeRequestSummaryTable(githubFixture.buildSummaryList());
  var html = crListTbodyEl.innerHTML;
  var m = html.match(/data-label="Latest Activity">([^<]*)</);
  assert(m && m[1] !== '--', 'latest activity: newest row shows its timestamp');
  var dashes = (html.match(/data-label="Latest Activity">--</g) || []).length;
  assertEqual(dashes, 1, 'latest activity: exactly the null-activity row renders --');
});

// ═════════════════════════════════════════════════════════════════════════
// Summary list rendering — GitLab parity
// ═════════════════════════════════════════════════════════════════════════

test('summary table: GitLab parity — MR identity, same lifecycle semantics', function () {
  W.renderChangeRequestSummaryTable(gitlabFixture.buildSummaryList());
  var html = crListTbodyEl.innerHTML;
  var rows = crListTbodyEl.querySelectorAll('.afk-cr-list-row');
  assertEqual(rows.length, 2, 'GitLab parity: two change requests, two rows');
  assertContains(html, 'MR #6', 'GitLab parity: provider-specific MR identity');
  assertContains(html, 'group/cloudnative', 'GitLab parity: repository visible');
  assertNotContains(html, 'PR #', 'GitLab parity: no PR terminology for GitLab rows');
  assertContains(html, 'badge-merged', 'GitLab parity: merged provider state');
  assertContains(html, 'badge-completed', 'GitLab parity: completed AFK automation state');
  assertContains(html, '$3.40', 'GitLab parity: known USD cost');
  assertContains(html, 'Cost unavailable', 'GitLab parity: missing cost renders Cost unavailable');
  assertContains(html, 'badge-open', 'GitLab parity: open provider state');
  assertContains(html, 'badge-failed', 'GitLab parity: failed AFK automation state');
});

test('summary table: independent dual-status combinations', function () {
  W.renderChangeRequestSummaryTable({
    items: [
      { provider: 'github', repository: 'r/x', external_id: '1', provider_state: 'open', automation_state: 'completed', total_estimated_cost_usd: 1.0, latest_linked_activity: '2026-08-17T10:00:00Z', executions: { total: 1, running: 0, completed: 1, failed: 0, cancelled: 0 } },
      { provider: 'github', repository: 'r/x', external_id: '2', provider_state: 'merged', automation_state: 'running', total_estimated_cost_usd: 2.0, latest_linked_activity: '2026-08-17T11:00:00Z', executions: { total: 1, running: 1, completed: 0, failed: 0, cancelled: 0 } },
      { provider: 'github', repository: 'r/x', external_id: '3', provider_state: 'closed', automation_state: 'failed', total_estimated_cost_usd: null, latest_linked_activity: null, executions: { total: 1, running: 0, completed: 0, failed: 1, cancelled: 0 } }
    ],
    total: 3, limit: 100, offset: 0
  });
  var html = crListTbodyEl.innerHTML;
  assertContains(html, 'badge-open', 'combinations: open provider state');
  assertContains(html, 'badge-completed', 'combinations: completed automation with open provider state');
  assertContains(html, 'badge-running', 'combinations: running automation with merged provider state');
  assertContains(html, 'badge-closed', 'combinations: closed provider state');
  assertContains(html, 'badge-failed', 'combinations: failed automation with closed provider state');
});

test('summary table: XSS-safe escaping of identity values', function () {
  W.renderChangeRequestSummaryTable({
    items: [
      { provider: 'github', repository: '<script>alert(1)</script>/x', external_id: '9" onmouseover="x', provider_state: 'open', automation_state: 'running', total_estimated_cost_usd: null, latest_linked_activity: null, executions: { total: 0, running: 0, completed: 0, failed: 0, cancelled: 0 } }
    ],
    total: 1, limit: 100, offset: 0
  });
  var html = crListTbodyEl.innerHTML;
  assertContains(html, '&lt;script&gt;', 'escaping: repository escaped');
  assertNotContains(html, '<script>alert(1)</script>', 'escaping: no raw script tag injected');
  assertContains(html, '&quot;', 'escaping: external id attribute escaped');
});

// ═════════════════════════════════════════════════════════════════════════
// Empty / error / stale state handling
// ═════════════════════════════════════════════════════════════════════════

test('state handling: empty result renders the empty state', function () {
  W.renderChangeRequestSummaryTable({ items: [], total: 0, limit: 100, offset: 0 });
  assertContains(crListTbodyEl.innerHTML, 'No change requests', 'empty: no-change-requests message');
  assertContains(crListTbodyEl.innerHTML, 'colspan="7"', 'empty: empty state spans all 7 columns');
});

test('state handling: failed fetch without previous data shows the error state', function () {
  fetchImpl = function () {
    return Promise.reject(new Error('API /api/v1/afk-outcomes/change-requests returned 500'));
  };
  return W.fetchChangeRequestsAndRender().then(function () {
    var html = crListTbodyEl.innerHTML;
    assertContains(html, 'No change requests', 'error: empty state rendered on first failure');
    assertContains(html, 'Fetch error', 'error: fetch-error indicator shown');
  });
});

test('state handling: failed refresh keeps the previous rows (stale)', function () {
  fetchImpl = function () {
    return okJson({ status: 'ok', data: githubFixture.buildSummaryList() });
  };
  return W.fetchChangeRequestsAndRender().then(function () {
    var before = crListTbodyEl.innerHTML;
    assertEqual(crListTbodyEl.querySelectorAll('.afk-cr-list-row').length, 3,
      'stale: previous successful rows on screen');
    fetchImpl = function () {
      return Promise.reject(new Error('API down'));
    };
    return W.fetchChangeRequestsAndRender().then(function () {
      assertEqual(crListTbodyEl.innerHTML, before, 'stale: previous rows retained after failed refresh');
      assertContains(crFreshnessEl.textContent, 'Showing previous data', 'stale: freshness label swaps in');
    });
  });
});

// ═════════════════════════════════════════════════════════════════════════
// Selection — identity-keyed detail flow
// ═════════════════════════════════════════════════════════════════════════

test('selection: clicking a row opens the identity-keyed detail flow', function () {
  fetchImpl = function () { return okJson({ status: 'ok', data: githubFixture.buildSummaryList() }); };
  return W.fetchChangeRequestsAndRender().then(function () {
    var rows = crListTbodyEl.querySelectorAll('.afk-cr-list-row');
    assertEqual(rows.length, 3, 'selection: rows wired');
    var lastUrl = null;
    fetchImpl = function (url) {
      lastUrl = url;
      return okJson({ status: 'ok', data: githubFixture.buildDetail() });
    };
    rows[0]._handlers.click();
    return settleDeep(4).then(function () {
      assertEqual(lastUrl, '/api/v1/afk-outcomes/change-requests/github/acme%2Fweb-app/142',
        'selection: detail fetched by change-request identity');
      assertNotContains(lastUrl, 'afk_run_id', 'selection: no internal AFK Run ID required for navigation');
      var selected = W.getSelectedChangeRequest();
      assert(selected && selected.provider === 'github' && selected.repository === 'acme/web-app' &&
        selected.externalId === '142', 'selection: selected identity recorded');
      assert(crDetailOverlayEl.classList.contains('visible'), 'selection: detail overlay visible');
      assertEqual(crDetailTitleEl.textContent, 'acme/web-app#142', 'selection: title shows the display identity');
      var html = crDetailBodyEl.innerHTML;
      assertContains(html, 'acme/web-app#142', 'detail: display identity first');
      assertContains(html, 'badge-merged', 'detail: provider state badge');
      assertContains(html, 'badge-completed', 'detail: AFK automation state badge');
      assertContains(html, '$4.85', 'detail: aggregate cost rendered');
      assertContains(html, 'Implementation', 'detail: implementation executions grouped');
      assertContains(html, 'Review', 'detail: review executions grouped');
      assertContains(html, 'awx-9001', 'detail: AWX job id rendered');
      assertContains(html, 'awx-9002', 'detail: failed attempt retained as history');
      assertContains(html, 'AWX runner lost connectivity', 'detail: bounded failure summary rendered');
      assertContains(html, 'data-session-id=', 'detail: session drill-down link present');
      assertContains(html, 'Activity timeline', 'detail: timeline section present');
      assertNotContains(html, '<details class="afk-cr-timeline" open', 'detail: timeline collapsed by default');
    });
  });
});

test('selection: unknown change request renders the not-found state', function () {
  fetchImpl = function () {
    return Promise.reject(new Error('API /api/v1/afk-outcomes/change-requests/github/acme/web-app/999 returned 404'));
  };
  return W.openChangeRequestDetail('github', 'acme/web-app', '999').then(function () {
    assertContains(crDetailBodyEl.innerHTML, 'Change request not found', 'not-found: distinct empty state');
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
  assertContains(html, 'badge-open', 'partial: provider state badge');
  assertContains(html, 'badge-failed', 'partial: automation state badge');
  assertContains(html, 'Cost unavailable', 'partial: missing aggregate cost renders Cost unavailable');
  assertContains(html, 'No linked executions', 'partial: empty executions section');
  assertContains(html, 'No sessions linked', 'partial: empty sessions section');
  assertContains(html, 'No timeline data', 'partial: empty timeline section');
});

// ═════════════════════════════════════════════════════════════════════════
// Filtering — served through the summary contract
// ═════════════════════════════════════════════════════════════════════════

test('filtering: apply sends filter values through the contract and re-fetches', function () {
  crFilterProviderEl.value = 'gitlab';
  crFilterRepositoryEl.value = 'group/cloudnative';
  crFilterProviderStateEl.value = 'open';
  crFilterAutomationStateEl.value = 'failed';
  var lastUrl = null;
  fetchImpl = function (url) {
    lastUrl = url;
    return okJson({ status: 'ok', data: { items: [], total: 0, limit: 100, offset: 0 } });
  };
  return W.applyChangeRequestFilters().then(function () {
    assertContains(lastUrl, 'provider=gitlab', 'filtering: provider filter through the contract');
    assertContains(lastUrl, 'repository=' + encodeURIComponent('group/cloudnative'), 'filtering: repository filter through the contract');
    assertContains(lastUrl, 'provider_state=open', 'filtering: provider-state filter through the contract');
    assertContains(lastUrl, 'automation_state=failed', 'filtering: automation-state filter through the contract');
    assertContains(crListTbodyEl.innerHTML, 'No change requests', 'filtering: filtered empty result rendered');
  });
});

test('filtering: clear resets filters, syncs controls, and re-fetches', function () {
  crFilterProviderEl.value = 'gitlab';
  crFilterRepositoryEl.value = 'group/cloudnative';
  crFilterProviderStateEl.value = 'open';
  crFilterAutomationStateEl.value = 'failed';
  var lastUrl = null;
  fetchImpl = function (url) {
    lastUrl = url;
    return okJson({ status: 'ok', data: githubFixture.buildSummaryList() });
  };
  return W.clearChangeRequestFilters().then(function () {
    assertNotContains(lastUrl, 'provider=', 'filtering: cleared provider param omitted');
    assertNotContains(lastUrl, 'provider_state=', 'filtering: cleared provider-state param omitted');
    assertNotContains(lastUrl, 'automation_state=', 'filtering: cleared automation-state param omitted');
    assertEqual(crFilterProviderEl.value, '', 'filtering: provider control reset');
    assertEqual(crFilterRepositoryEl.value, '', 'filtering: repository control reset');
    assertEqual(crFilterProviderStateEl.value, '', 'filtering: provider-state control reset');
    assertEqual(crFilterAutomationStateEl.value, '', 'filtering: automation-state control reset');
  });
});

// ═════════════════════════════════════════════════════════════════════════
// Regressions — existing AFK run views + adapter absence
// ═════════════════════════════════════════════════════════════════════════

test('regression: the run-per-row AFK Outcomes table remains functional', function () {
  W.renderAfkOutcomesTable({
    items: [
      { afk_run_id: 'reg-1', provider: 'github', status: 'completed', title: 'Legacy run',
        outcome_status: 'merged', started_at: '2026-08-13T09:00:00Z', last_seen_at: '2026-08-13T10:00:00Z' }
    ],
    total: 1
  });
  var html = afkRunsTbodyEl.innerHTML;
  assertContains(html, 'data-id="reg-1"', 'regression: AFK run row still rendered');
  assertContains(html, 'badge-completed', 'regression: run status badge still rendered');
  assertContains(html, 'badge-merged', 'regression: outcome badge still rendered');
});

test('defensive: app.js renders the empty state when the adapter module is absent', function () {
  var noAdapters = buildSandbox(false);
  var W2 = noAdapters.window;
  var tbody2 = makeCrListTbody('afk-cr-list-tbody');
  var registry2 = {};
  registry2['afk-cr-list-tbody'] = tbody2;
  // Re-run app.js with the element available and no adapters loaded.
  var sandbox2 = noAdapters.sandbox;
  sandbox2.document.getElementById = function (id) { return registry2[id] || null; };
  vm.runInContext(
    fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8'),
    sandbox2, { filename: 'app.js (no adapters)' }
  );
  var threw = false;
  try {
    W2.renderChangeRequestSummaryTable({
      items: [{ provider: 'github', repository: 'a/b', external_id: '1' }], total: 1
    });
  } catch (e) {
    threw = true;
  }
  assert(threw === false, 'defensive: no crash without the adapter module');
  assertContains(tbody2.innerHTML, 'No change requests', 'defensive: empty state without the adapter module');
});

// ═════════════════════════════════════════════════════════════════════════
// Pagination — previous/next, page count, limit/offset URL, edge cases
// ═════════════════════════════════════════════════════════════════════════

function buildPagedSummaryList(total, offset) {
  // Builds `total` rows (page-sized by limit) so the pagination renderer has
  // deterministic totals to derive the page count from.
  var items = [];
  for (var i = 1; i <= total; i++) {
    items.push({
      provider: 'github', repository: 'acme/web-app', external_id: String(i),
      resource_type: 'change_request',
      provider_state: 'open', automation_state: 'running',
      total_estimated_cost_usd: 1.0,
      latest_linked_activity: null,
      executions: { total: 0, running: 0, completed: 0, failed: 0, cancelled: 0 }
    });
  }
  return { items: items, total: total, limit: 100, offset: offset || 0 };
}

test('pagination: URL builder emits offset when supplied', function () {
  var url = W.buildChangeRequestListUrl({}, { preset: 'this-month' }, 100, 250);
  assertContains(url, 'limit=100', 'pagination URL: limit present');
  assertContains(url, 'offset=250', 'pagination URL: offset present');
});

test('pagination: parseChangeRequestPagination derives page from limit/offset', function () {
  var p0 = W.parseChangeRequestPagination('');
  assertEqual(p0.page, 1, 'pagination parse: empty query defaults to page 1');
  assertEqual(p0.pageSize, 100, 'pagination parse: empty query defaults to AFK_CR_LIMIT page size');
  var p1 = W.parseChangeRequestPagination('limit=100&offset=100');
  assertEqual(p1.page, 2, 'pagination parse: offset 100 with limit 100 is page 2');
  assertEqual(p1.pageSize, 100, 'pagination parse: limit carried');
  var p2 = W.parseChangeRequestPagination('limit=50&offset=125');
  assertEqual(p2.page, 3, 'pagination parse: offset 125 with limit 50 is page 3 (floor(125/50)+1)');
  assertEqual(p2.pageSize, 50, 'pagination parse: custom limit carried');
  var p3 = W.parseChangeRequestPagination('limit=0&offset=abc');
  assertEqual(p3.page, 1, 'pagination parse: malformed values fall back to page 1');
});

test('pagination: changeRequestsUrlWithPagination round-trips limit/offset', function () {
  var url = W.changeRequestsUrlWithPagination(3, 100);
  assertContains(url, 'limit=100', 'URL with pagination: limit param');
  assertContains(url, 'offset=200', 'URL with pagination: offset = (page-1) * size');
});

test('pagination: nearestValidChangeRequestPage clamps to the result set', function () {
  assertEqual(W.nearestValidChangeRequestPage(0, 5, 100), 1, 'nearest page: empty result resolves to page 1');
  assertEqual(W.nearestValidChangeRequestPage(250, 5, 100), 3, 'nearest page: overshoot clamps to last page');
  assertEqual(W.nearestValidChangeRequestPage(250, 2, 100), 2, 'nearest page: valid page unchanged');
});

test('pagination: renderChangeRequestPagination shows prev/next + page numbers', function () {
  W.setChangeRequestPage(1);
  W.renderChangeRequestPagination(buildPagedSummaryList(250, 0));
  var html = crPaginationNavEl.innerHTML;
  assertContains(html, 'Previous', 'pagination render: Previous button');
  assertContains(html, 'Next', 'pagination render: Next button');
  assertContains(html, 'data-page="1"', 'pagination render: page 1 button');
  assertContains(html, 'data-page="2"', 'pagination render: page 2 button');
  assertContains(html, 'data-page="3"', 'pagination render: page 3 button (250 total / 100 size)');
  assertContains(html, 'aria-current="page"', 'pagination render: current page marked');
});

test('pagination: first page disables Previous; last page disables Next', function () {
  // First page: page 1 of 250 total → Previous disabled, Next enabled.
  W.setChangeRequestPage(1);
  W.renderChangeRequestPagination(buildPagedSummaryList(250, 0));
  var buttons = crPaginationNavEl.querySelectorAll('button');
  var prev = buttons[0];
  var next = buttons[buttons.length - 1];
  assert(prev.disabled === true, 'pagination first page: Previous disabled on page 1');
  assert(prev.getAttribute('data-page') === '0', 'pagination first page: Previous data-page is page 0');
  assert(next.disabled === false, 'pagination first page: Next enabled on page 1');
});

test('pagination: last page disables Next', function () {
  // Move to page 3 (the last of 250 @ 100) via the state hook then render.
  W.setChangeRequestPage(3);
  W.renderChangeRequestPagination(buildPagedSummaryList(250, 200));
  var buttons = crPaginationNavEl.querySelectorAll('button');
  var prev = buttons[0];
  var next = buttons[buttons.length - 1];
  assert(prev.disabled === false, 'pagination last page: Previous enabled on page 3');
  assert(next.disabled === true, 'pagination last page: Next disabled on the final page');
  assert(prev.getAttribute('data-page') === '2', 'pagination last page: Previous points at page 2');
});

test('pagination: no pages renders empty control (no results)', function () {
  W.setChangeRequestPage(1);
  W.renderChangeRequestPagination({ items: [], total: 0, limit: 100, offset: 0 });
  assertEqual(crPaginationNavEl.innerHTML, '', 'pagination empty: control block cleared for no results');
});

test('pagination: single-page result renders one page, both controls disabled', function () {
  W.setChangeRequestPage(1);
  W.renderChangeRequestPagination(buildPagedSummaryList(3, 0));
  var buttons = crPaginationNavEl.querySelectorAll('button');
  assertEqual(buttons.length, 3, 'pagination single page: prev + one page + next = 3 buttons');
  assert(buttons[0].disabled === true, 'pagination single page: Previous disabled');
  assert(buttons[2].disabled === true, 'pagination single page: Next disabled');
  assertContains(buttons[1].getAttribute('aria-label') || '', 'Page 1, current page', 'pagination single page: aria-current label');
});

test('pagination: clicking Next fetches the next page with offset', function () {
  W.setChangeRequestPage(1);
  W.renderChangeRequestPagination(buildPagedSummaryList(250, 0));
  var buttons = crPaginationNavEl.querySelectorAll('button');
  var next = buttons[buttons.length - 1];
  var lastUrl = null;
  fetchImpl = function (url) {
    lastUrl = url;
    // total 250 covers page 2 (offset 100) — no fallback refetch.
    return okJson({ status: 'ok', data: buildPagedSummaryList(250, 100) });
  };
  next._handlers.click();
  return settleDeep(4).then(function () {
    assertContains(lastUrl, 'limit=100', 'pagination next click: limit carried');
    assertContains(lastUrl, 'offset=100', 'pagination next click: offset advanced to 100');
  });
});

test('pagination: clicking Previous fetches the previous page with offset', function () {
  W.setChangeRequestPage(3);
  W.renderChangeRequestPagination(buildPagedSummaryList(250, 200));
  var buttons = crPaginationNavEl.querySelectorAll('button');
  var prev = buttons[0];
  var lastUrl = null;
  fetchImpl = function (url) {
    lastUrl = url;
    return okJson({ status: 'ok', data: buildPagedSummaryList(250, 100) });
  };
  prev._handlers.click();
  return settleDeep(4).then(function () {
    assertContains(lastUrl, 'limit=100', 'pagination prev click: limit carried');
    assertContains(lastUrl, 'offset=100', 'pagination prev click: offset rewound to 100');
  });
});

test('pagination: clicking the current page is a no-op (no refetch, no history entry)', function () {
  W.setChangeRequestPage(2);
  W.renderChangeRequestPagination(buildPagedSummaryList(250, 100));
  var buttons = crPaginationNavEl.querySelectorAll('button');
  var current = buttons[2]; // page 2 of [prev,1,2,3,next]
  var fetchCount = 0;
  fetchImpl = function () {
    fetchCount++;
    return okJson({ status: 'ok', data: buildPagedSummaryList(100, 100) });
  };
  current._handlers.click();
  return settleDeep(4).then(function () {
    assertEqual(fetchCount, 0, 'pagination no-op: current-page click never refetches');
  });
});

test('pagination: fetch path applies the page fallback when the total shrank', function () {
  W.setChangeRequestPage(3); // page 3 of an assumed 250
  fetchImpl = function (url) {
    if (url.indexOf('offset=200') !== -1) {
      // The fetched total (150) only covers 2 pages — page 3 is invalid.
      return okJson({ status: 'ok', data: { items: [], total: 150, limit: 100, offset: 200 } });
    }
    return okJson({ status: 'ok', data: { items: buildPagedSummaryList(50, 100).items, total: 150, limit: 100, offset: 100 } });
  };
  return W.fetchChangeRequestsAndRender().then(function () {
    assertEqual(crListTbodyEl.querySelectorAll('.afk-cr-list-row').length, 50,
      'pagination fallback: corrected page 2 rows rendered');
  });
});

test('pagination: filter apply resets to page 1 and replaces URL state', function () {
  W.setChangeRequestPage(3);
  crFilterProviderEl.value = 'gitlab';
  var lastUrl = null;
  fetchImpl = function (url) {
    lastUrl = url;
    return okJson({ status: 'ok', data: { items: [], total: 0, limit: 100, offset: 0 } });
  };
  return W.applyChangeRequestFilters().then(function () {
    assertContains(lastUrl, 'provider=gitlab', 'pagination filter apply: filters carried');
    assertContains(lastUrl, 'offset=0', 'pagination filter apply: offset reset to 0 (page 1)');
  });
});

// ═════════════════════════════════════════════════════════════════════════
// Dashboard refresh — page-size consistency (issue #617)
// The dashboard refresh data path (fetchAll, the fetch core of
// refreshDashboard) builds the change-request list URL from the CURRENT
// pagination state — page size afkCrPageSize, offset (page - 1) * size — so
// a deep link like ?limit=50&offset=50 survives an auto-refresh instead of
// silently reverting to the hardcoded AFK_CR_LIMIT (100).  Before the fix
// the refresh used AFK_CR_LIMIT as the limit but afkCrPageSize for the
// offset, so a non-default page size produced a mismatched limit/offset
// pair (e.g. limit=100&offset=50) on every refresh cycle.
// ═════════════════════════════════════════════════════════════════════════

test('dashboard refresh: non-default page size preserved on refresh (issue #617)', function () {
  if (typeof W.fetchAll !== 'function') {
    assert(false, 'app.js: fetchAll exposed on the window test seam');
    return;
  }
  // URL state: page 2 of 50 rows (?limit=50&offset=50).
  main.sandbox.location.search = '?limit=50&offset=50';
  W.readChangeRequestPaginationFromUrl();
  var crUrl = null;
  fetchImpl = function (url) {
    if (String(url).indexOf('/api/v1/afk-outcomes/change-requests') === 0) {
      crUrl = url;
    }
    return okJson({ status: 'ok', data: { items: [], total: 0, limit: 50, offset: 50 } });
  };
  return W.fetchAll().then(function () {
    assert(crUrl !== null, 'dashboard refresh: change-request summary fetched in the refresh cycle');
    assertContains(crUrl, 'limit=50&offset=50',
      'dashboard refresh: page size 50 carried as limit=50&offset=50');
    assertNotContains(crUrl, 'limit=100&offset=50',
      'dashboard refresh: refresh never falls back to the hardcoded AFK_CR_LIMIT (100)');
  });
});

test('dashboard refresh: deep page with non-default page size preserved on refresh (issue #617)', function () {
  if (typeof W.fetchAll !== 'function') {
    assert(false, 'app.js: fetchAll exposed on the window test seam');
    return;
  }
  // URL state: page 3 of 25 rows (?limit=25&offset=50 → floor(50/25)+1 = 3).
  main.sandbox.location.search = '?limit=25&offset=50';
  W.readChangeRequestPaginationFromUrl();
  var crUrl = null;
  fetchImpl = function (url) {
    if (String(url).indexOf('/api/v1/afk-outcomes/change-requests') === 0) {
      crUrl = url;
    }
    return okJson({ status: 'ok', data: { items: [], total: 0, limit: 25, offset: 50 } });
  };
  return W.fetchAll().then(function () {
    assert(crUrl !== null, 'dashboard refresh: change-request summary fetched in the refresh cycle');
    assertContains(crUrl, 'limit=25&offset=50',
      'dashboard refresh: deep-page size 25 carried as limit=25&offset=50');
  });
});

// ═════════════════════════════════════════════════════════════════════════

runTests();
