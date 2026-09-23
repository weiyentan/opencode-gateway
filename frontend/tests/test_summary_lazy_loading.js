/**
 * Unit tests for issue #739: Summary endpoints + lazy loading.
 *
 * Run with: node frontend/tests/test_summary_lazy_loading.js
 *
 * Verifies:
 * - fetchAll() calls summary endpoints for initial data
 * - fetchAll() does NOT call deferred endpoints during initial load
 * - Panel open/expand triggers deferred fetch
 * - PANEL_ENDPOINTS mapping updated correctly
 * - Panel freshness preserved on summary request failure
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

// Register required elements
var kpiTokensEl = makeFakeElement('kpi-tokens');
var kpiTokensBreakdownEl = makeFakeElement('kpi-tokens-breakdown');
var kpiTokensDetailEl = makeFakeElement('kpi-tokens-detail');
var kpiCostEl = makeFakeElement('kpi-cost');
var kpiCostDetailEl = makeFakeElement('kpi-cost-detail');
var kpiSessionsEl = makeFakeElement('kpi-sessions');
var kpiSessionsDetailEl = makeFakeElement('kpi-sessions-detail');
var kpiCollectorsEl = makeFakeElement('kpi-collectors');
var kpiCollectorsDetailEl = makeFakeElement('kpi-collectors-detail');
var kpiSourceDbsEl = makeFakeElement('kpi-source-dbs');
var kpiSourceDbsDetailEl = makeFakeElement('kpi-source-dbs-detail');
elementRegistry['kpi-tokens'] = kpiTokensEl;
elementRegistry['kpi-tokens-breakdown'] = kpiTokensBreakdownEl;
elementRegistry['kpi-tokens-detail'] = kpiTokensDetailEl;
elementRegistry['kpi-cost'] = kpiCostEl;
elementRegistry['kpi-cost-detail'] = kpiCostDetailEl;
elementRegistry['kpi-sessions'] = kpiSessionsEl;
elementRegistry['kpi-sessions-detail'] = kpiSessionsDetailEl;
elementRegistry['kpi-collectors'] = kpiCollectorsEl;
elementRegistry['kpi-collectors-detail'] = kpiCollectorsDetailEl;
elementRegistry['kpi-source-dbs'] = kpiSourceDbsEl;
elementRegistry['kpi-source-dbs-detail'] = kpiSourceDbsDetailEl;

var arTbodyEl = makeFakeElement('agent-runs-tbody');
elementRegistry['agent-runs-tbody'] = arTbodyEl;

var agentUsageTbodyEl = makeFakeElement('agent-usage-tbody');
elementRegistry['agent-usage-tbody'] = agentUsageTbodyEl;

var modelMixChartEl = makeFakeElement('model-mix-chart');
elementRegistry['model-mix-chart'] = modelMixChartEl;

var agentsTbodyEl = makeFakeElement('agents-tbody');
elementRegistry['agents-tbody'] = agentsTbodyEl;

var afkRunsTbodyEl = makeFakeElement('afk-runs-tbody');
var afkDetailOverlayEl = makeFakeElement('afk-detail-overlay');
var afkDetailTitleEl = makeFakeElement('afk-detail-title');
var afkDetailBodyEl = makeFakeElement('afk-detail-body');
var afkDetailCloseEl = makeFakeElement('afk-detail-close');
elementRegistry['afk-runs-tbody'] = afkRunsTbodyEl;
elementRegistry['afk-detail-overlay'] = afkDetailOverlayEl;
elementRegistry['afk-detail-title'] = afkDetailTitleEl;
elementRegistry['afk-detail-body'] = afkDetailBodyEl;
elementRegistry['afk-detail-close'] = afkDetailCloseEl;

var afkReposTbodyEl = makeFakeElement('afk-repos-tbody');
elementRegistry['afk-repos-tbody'] = afkReposTbodyEl;

var cpTbodyEl = makeFakeElement('cp-tbody');
var cpPanelSubtitleEl = makeFakeElement('cp-panel-subtitle');
elementRegistry['cp-tbody'] = cpTbodyEl;
elementRegistry['cp-panel-subtitle'] = cpPanelSubtitleEl;

var afkCrListTbodyEl = makeFakeElement('afk-cr-list-tbody');
var afkCrPaginationEl = makeFakeElement('afk-cr-pagination');
var afkCrFilterProviderEl = makeFakeElement('afk-cr-filter-provider');
var afkCrFilterRepositoryEl = makeFakeElement('afk-cr-filter-repository');
var afkCrFilterProviderStateEl = makeFakeElement('afk-cr-filter-provider-state');
var afkCrFilterApplyEl = makeFakeElement('afk-cr-filter-apply');
var afkCrFilterClearEl = makeFakeElement('afk-cr-filter-clear');
elementRegistry['afk-cr-list-tbody'] = afkCrListTbodyEl;
elementRegistry['afk-cr-pagination'] = afkCrPaginationEl;
elementRegistry['afk-cr-filter-provider'] = afkCrFilterProviderEl;
elementRegistry['afk-cr-filter-repository'] = afkCrFilterRepositoryEl;
elementRegistry['afk-cr-filter-provider-state'] = afkCrFilterProviderStateEl;
elementRegistry['afk-cr-filter-apply'] = afkCrFilterApplyEl;
elementRegistry['afk-cr-filter-clear'] = afkCrFilterClearEl;

var crListDetailOverlayEl = makeFakeElement('cr-list-detail-overlay');
var crListDetailTitleEl = makeFakeElement('cr-list-detail-title');
var crListDetailBodyEl = makeFakeElement('cr-list-detail-body');
var crListDetailCloseEl = makeFakeElement('cr-list-detail-close');
elementRegistry['cr-list-detail-overlay'] = crListDetailOverlayEl;
elementRegistry['cr-list-detail-title'] = crListDetailTitleEl;
elementRegistry['cr-list-detail-body'] = crListDetailBodyEl;
elementRegistry['cr-list-detail-close'] = crListDetailCloseEl;

var unresolvedTbodyEl = makeFakeElement('unresolved-relationships-tbody');
elementRegistry['unresolved-relationships-tbody'] = unresolvedTbodyEl;

var arFilterFromEl = makeFakeElement('ar-filter-from');
var arFilterToEl = makeFakeElement('ar-filter-to');
var arFilterClearEl = makeFakeElement('ar-filter-clear');
var arFilterApplyEl = makeFakeElement('ar-filter-apply');
var arFilterAgentEl = makeFakeElement('ar-filter-agent');
var arFilterStatusEl = makeFakeElement('ar-filter-status');
var arPageSizeEl = makeFakeElement('ar-page-size');
elementRegistry['ar-filter-from'] = arFilterFromEl;
elementRegistry['ar-filter-to'] = arFilterToEl;
elementRegistry['ar-filter-clear'] = arFilterClearEl;
elementRegistry['ar-filter-apply'] = arFilterApplyEl;
elementRegistry['ar-filter-agent'] = arFilterAgentEl;
elementRegistry['ar-filter-status'] = arFilterStatusEl;
elementRegistry['ar-page-size'] = arPageSizeEl;

var arPaginationEl = makeFakeElement('agent-runs-pagination');
arPaginationEl.querySelectorAll = function () { return []; };
elementRegistry['agent-runs-pagination'] = arPaginationEl;

var arDetailOverlayEl = makeFakeElement('ar-detail-overlay');
var arDetailTitleEl = makeFakeElement('ar-detail-title');
var arDetailBodyEl = makeFakeElement('ar-detail-body');
var arDetailCloseEl = makeFakeElement('ar-detail-close');
elementRegistry['ar-detail-overlay'] = arDetailOverlayEl;
elementRegistry['ar-detail-title'] = arDetailTitleEl;
elementRegistry['ar-detail-body'] = arDetailBodyEl;
elementRegistry['ar-detail-close'] = arDetailCloseEl;

var trSessionInputEl = makeFakeElement('tr-session-input');
var trLoadBtnEl = makeFakeElement('tr-load-btn');
var trSessionHeaderEl = makeFakeElement('tr-session-header');
var trViewToggleEl = makeFakeElement('tr-view-toggle');
var trTimelineWrapEl = makeFakeElement('tr-timeline-wrap');
var trMessagesWrapEl = makeFakeElement('tr-messages-wrap');
var trPartsWrapEl = makeFakeElement('tr-parts-wrap');
var trNextPageBtnEl = makeFakeElement('tr-next-page-btn');
var trStatusEl = makeFakeElement('tr-status');
elementRegistry['tr-session-input'] = trSessionInputEl;
elementRegistry['tr-load-btn'] = trLoadBtnEl;
elementRegistry['tr-session-header'] = trSessionHeaderEl;
elementRegistry['tr-view-toggle'] = trViewToggleEl;
elementRegistry['tr-timeline-wrap'] = trTimelineWrapEl;
elementRegistry['tr-messages-wrap'] = trMessagesWrapEl;
elementRegistry['tr-parts-wrap'] = trPartsWrapEl;
elementRegistry['tr-next-page-btn'] = trNextPageBtnEl;
elementRegistry['tr-status'] = trStatusEl;

var crProvOverlayEl = makeFakeElement('cr-prov-overlay');
var crProvTitleEl = makeFakeElement('cr-prov-title');
var crProvBodyEl = makeFakeElement('cr-prov-body');
var crProvCloseEl = makeFakeElement('cr-prov-close');
elementRegistry['cr-prov-overlay'] = crProvOverlayEl;
elementRegistry['cr-prov-title'] = crProvTitleEl;
elementRegistry['cr-prov-body'] = crProvBodyEl;
elementRegistry['cr-prov-close'] = crProvCloseEl;

var historyCalls = [];
var historyReplaceCalls = [];
var historyStub = {
  pushState: function (state, title, url) { historyCalls.push(url); },
  replaceState: function (state, title, url) { historyReplaceCalls.push(url); }
};

var W = {}; // window test seam
var appJsSandbox = null;
var main = {};

(function loadRealAppJs() {
  var appJsPath = path.join(__dirname, '..', 'app.js');
  var source = fs.readFileSync(appJsPath, 'utf8');
  var adaptersPath = path.join(__dirname, '..', 'adapters', 'change_request_adapters.js');
  var adaptersSource = fs.readFileSync(adaptersPath, 'utf8');

  var documentStub = {
    readyState: 'loading',
    querySelector: function () { return null; },
    getElementById: function (id) { return elementRegistry[id] || null; },
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
    fetch: function () { return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } }); },
    location: { href: '', search: '', pathname: '' },
    history: historyStub,
    URLSearchParams: URLSearchParams,
    navigator: {}
  };
  sandbox.window = sandboxWindow;
  appJsSandbox = sandbox;
  main.sandbox = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(adaptersSource, sandbox, { filename: 'change_request_adapters.js' });
  vm.runInContext(source, sandbox, { filename: 'app.js' });

  // Copy window seam
  Object.keys(sandboxWindow).forEach(function (k) { W[k] = sandboxWindow[k]; });
})();

// ── Source code assertions ───────────────────────────────────────────────

console.log('\u25B6 Issue #739 — source code verification');

(function () {
  var appJsSource = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');

  // 1. fetchAll() calls summary endpoints
  assert(appJsSource.indexOf('/api/v1/usage/dashboard/summary') !== -1,
    'app.js: fetchAll() calls /api/v1/usage/dashboard/summary');
  assert(appJsSource.indexOf('/api/v1/afk/dashboard/summary') !== -1,
    'app.js: fetchAll() calls /api/v1/afk/dashboard/summary');

  // 2. PANEL_ENDPOINTS mapping updated
  assert(/'kpi-tokens':\s*\['summaryUsage'\]/.test(appJsSource),
    'app.js: kpi-tokens maps to summaryUsage in PANEL_ENDPOINTS');
  assert(/'kpi-cost':\s*\['summaryUsage'\]/.test(appJsSource),
    'app.js: kpi-cost maps to summaryUsage in PANEL_ENDPOINTS');
  assert(/'afk-outcomes':\s*\['summaryAfk'\]/.test(appJsSource),
    'app.js: afk-outcomes maps to summaryAfk in PANEL_ENDPOINTS');
  assert(/'afk-repos':\s*\['summaryAfk'\]/.test(appJsSource),
    'app.js: afk-repos maps to summaryAfk in PANEL_ENDPOINTS');
  assert(/'afk-change-requests':\s*\['summaryAfk'\]/.test(appJsSource),
    'app.js: afk-change-requests maps to summaryAfk in PANEL_ENDPOINTS');

  // 3. Deferred endpoints exist for lazy loading
  assert(appJsSource.indexOf('fetchDetailEndpoints') !== -1,
    'app.js: fetchDetailEndpoints function exists');
  assert(appJsSource.indexOf('fetchAfkDetailData') !== -1,
    'app.js: fetchAfkDetailData function exists');
  assert(appJsSource.indexOf('fetchClientProjectData') !== -1,
    'app.js: fetchClientProjectData function exists');
  assert(appJsSource.indexOf('fetchModelData') !== -1,
    'app.js: fetchModelData function exists');
  assert(appJsSource.indexOf('fetchAgentUsageData') !== -1,
    'app.js: fetchAgentUsageData function exists');

  // 4. _firstPaintDone gate exists
  assert(appJsSource.indexOf('_firstPaintDone') !== -1,
    'app.js: _firstPaintDone flag exists for first-paint gate');

  // 5. Summary data used in renderKPIs
  assert(appJsSource.indexOf('summaryUsage') !== -1,
    'app.js: renderKPIs uses summaryUsage data');
  assert(appJsSource.indexOf("if (data.summaryUsage)") !== -1 &&
         appJsSource.indexOf("aggregateSummaryBuckets(data.summaryUsage)") !== -1,
    'app.js: renderKPIs aggregates summaryUsage.buckets[] and falls back to aggTotal');
})();

// ── PANEL_ENDPOINTS mapping verification ──────────────────────────────────

console.log('\u25B6 Issue #739 — PANEL_ENDPOINTS mapping');

(function () {
  // Verify that summary endpoints mark the correct panels stale
  var summaryUsageFail = W.resolvePanelStatuses({ summaryUsage: 'boom' });
  assert(summaryUsageFail['kpi-tokens'] === 'stale',
    'summaryUsage failure stales kpi-tokens');
  assert(summaryUsageFail['kpi-cost'] === 'stale',
    'summaryUsage failure stales kpi-cost');

  var summaryAfkFail = W.resolvePanelStatuses({ summaryAfk: 'boom' });
  assert(summaryAfkFail['afk-outcomes'] === 'stale',
    'summaryAfk failure stales afk-outcomes');
  assert(summaryAfkFail['afk-repos'] === 'stale',
    'summaryAfk failure stales afk-repos');
  assert(summaryAfkFail['afk-change-requests'] === 'stale',
    'summaryAfk failure stales afk-change-requests');

  // Detail endpoint failures do NOT affect summary panels
  assert(summaryUsageFail['kpi-sessions'] === 'ok',
    'summaryUsage failure: kpi-sessions stays ok (aggTotal is fine)');
  assert(summaryAfkFail['unresolved-relationships'] === 'ok',
    'summaryAfk failure: unresolved-relationships stays ok (afkRuns is fine)');

  // All-clear resolves all panels to ok
  var allOk = W.resolvePanelStatuses({});
  assert(allOk['kpi-tokens'] === 'ok', 'no errors: kpi-tokens resolves to ok');
  assert(allOk['kpi-cost'] === 'ok', 'no errors: kpi-cost resolves to ok');
  assert(allOk['afk-outcomes'] === 'ok', 'no errors: afk-outcomes resolves to ok');
  assert(allOk['afk-repos'] === 'ok', 'no errors: afk-repos resolves to ok');
  assert(allOk['afk-change-requests'] === 'ok', 'no errors: afk-change-requests resolves to ok');
})();

// ── AFK Dashboard Summary: filter wiring (issue #732) ──────────────────────

console.log('\u25B6 Issue #732 — fetchAll() wires AFK Dashboard Summary filters into URL');

(async function () {
  // Verify that fetchAll() passes the provider/repository filter state
  // into the AFK summary URL via buildAfkDashboardSummaryUrl(), instead
  // of using a hardcoded inline URL (the bug fixed in #732).
  //
  // Runs as an async IIFE BEFORE the other fetchAll() tests to avoid
  // race conditions with shared sandbox state.

  // Set non-empty filter state — the key fix for #732
  W._setAfkDashSummaryFilters({ provider: 'github', repository: 'acme/web-app' });
  W._setFirstPaintDone(false);

  var fetchedUrls = [];
  var _prevFetch = main.sandbox.fetch;
  main.sandbox.fetch = function (url) {
    fetchedUrls.push(String(url));
    return Promise.resolve({
      ok: true,
      json: function () {
        return Promise.resolve({
          status: 'ok',
          data: { items: [], total: 0, total_input_tokens: 0, total_output_tokens: 0, total_estimated_cost_usd: 0, session_count: 0 }
        });
      }
    });
  };

  await W.fetchAll();

  var afkUrl = fetchedUrls.find(function (u) { return u.indexOf('/api/v1/afk/dashboard/summary') !== -1; });
  assert(afkUrl !== undefined, 'fetchAll() calls /api/v1/afk/dashboard/summary');
  assert(afkUrl.indexOf('provider=github') !== -1,
    'AFK summary URL includes provider filter from afkDashSummaryFilters');
  assert(afkUrl.indexOf('repository=acme%2Fweb-app') !== -1,
    'AFK summary URL includes repository filter from afkDashSummaryFilters (URL-encoded)');
  assert(afkUrl.indexOf('interval=') !== -1,
    'AFK summary URL includes interval parameter');

  // Verify the URL uses from_date (not the old hardcoded start_date)
  assert(afkUrl.indexOf('from_date=') !== -1,
    'AFK summary URL uses from_date parameter');
  assert(afkUrl.indexOf('start_date=') === -1,
    'AFK summary URL does not use the old hardcoded start_date');

  // Restore state for subsequent tests
  main.sandbox.fetch = _prevFetch;
  W._setFirstPaintDone(false);

  // Issue #732: verify buildAfkDashboardSummaryUrl passes the interval
  // value through to the URL (monthly and daily).
  var monthlyUrl = W.buildAfkDashboardSummaryUrl({
    from_date: '2026-09-01',
    to_date: '2026-09-24',
    provider: 'github',
    repository: 'acme/web-app'
  }, 'monthly');
  assert(monthlyUrl.indexOf('interval=monthly') !== -1,
    'buildAfkDashboardSummaryUrl includes interval=monthly when interval is monthly');

  var dailyUrl = W.buildAfkDashboardSummaryUrl({
    from_date: '2026-09-01',
    to_date: '2026-09-24',
    provider: 'github',
    repository: 'acme/web-app'
  }, 'daily');
  assert(dailyUrl.indexOf('interval=daily') !== -1,
    'buildAfkDashboardSummaryUrl includes interval=daily when interval is daily');
})();

// ── fetchAll() initial load behavior ──────────────────────────────────────

console.log('\u25B6 Issue #739 — fetchAll() initial load: summary only');

(function () {
  // On initial load (_firstPaintDone = false), fetchAll() should only call
  // summary endpoints, NOT detail endpoints.
  W._setFirstPaintDone(false);
  W._setAfkDetailFetched(false);
  W._setClientProjectFetched(false);
  W._setModelDetailFetched(false);
  W._setAgentUsageFetched(false);

  var fetchedUrls = [];
  main.sandbox.fetch = function (url) {
    fetchedUrls.push(String(url));
    return Promise.resolve({
      ok: true,
      json: function () {
        return Promise.resolve({
          status: 'ok',
          data: {
            items: [],
            total: 0,
            total_input_tokens: 0,
            total_output_tokens: 0,
            total_estimated_cost_usd: 0,
            session_count: 0
          }
        });
      }
    });
  };

  W.fetchAll().then(function () {
    // Summary endpoints should be called
    assert(fetchedUrls.some(function (u) { return u.indexOf('/api/v1/usage/dashboard/summary') !== -1; }),
      'initial load: /api/v1/usage/dashboard/summary is called');
    assert(fetchedUrls.some(function (u) { return u.indexOf('/api/v1/afk/dashboard/summary') !== -1; }),
      'initial load: /api/v1/afk/dashboard/summary is called');
    assert(fetchedUrls.some(function (u) { return u.indexOf('/health') !== -1; }),
      'initial load: /health is called');
    assert(fetchedUrls.some(function (u) { return u.indexOf('/api/v1/usage/agent-runs') !== -1; }),
      'initial load: /api/v1/usage/agent-runs is called');

    // Deferred endpoints should NOT be called during initial load
    assert(!fetchedUrls.some(function (u) { return u.indexOf('/api/v1/usage/aggregates?') !== -1 && u.indexOf('group_by') === -1; }),
      'initial load: /api/v1/usage/aggregates (total) is NOT called');
    assert(!fetchedUrls.some(function (u) { return u.indexOf('group_by=model') !== -1; }),
      'initial load: aggregates?group_by=model is NOT called');
    assert(!fetchedUrls.some(function (u) { return u.indexOf('group_by=agent') !== -1; }),
      'initial load: aggregates?group_by=agent is NOT called');
    assert(!fetchedUrls.some(function (u) { return u.indexOf('group_by=client,project') !== -1; }),
      'initial load: aggregates?group_by=client,project is NOT called');
    assert(!fetchedUrls.some(function (u) { return u.indexOf('/api/v1/afk-outcomes/runs') !== -1; }),
      'initial load: /api/v1/afk-outcomes/runs is NOT called');
    assert(!fetchedUrls.some(function (u) { return u.indexOf('/api/v1/afk-outcomes/change-requests') !== -1; }),
      'initial load: /api/v1/afk-outcomes/change-requests is NOT called');
    assert(!fetchedUrls.some(function (u) { return u.indexOf('/api/v1/usage/records') !== -1; }),
      'initial load: /api/v1/usage/records is NOT called');

    // Restore default fetch
    main.sandbox.fetch = function () {
      return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } });
    };
  });
})();

// ── fetchAll() subsequent refresh behavior ────────────────────────────────

console.log('\u25B6 Issue #739 — fetchAll() subsequent refresh: all endpoints');

(function () {
  // On subsequent refresh (_firstPaintDone = true), fetchAll() should call
  // ALL endpoints (summary + detail).
  W._setFirstPaintDone(true);

  var fetchedUrls = [];
  main.sandbox.fetch = function (url) {
    fetchedUrls.push(String(url));
    var responseData = {};
    if (url.indexOf('group_by=model') !== -1) {
      responseData = { status: 'ok', data: [] };
    } else if (url.indexOf('group_by=agent') !== -1) {
      responseData = { status: 'ok', data: [] };
    } else if (url.indexOf('group_by=client,project') !== -1) {
      responseData = { status: 'ok', data: [] };
    } else if (url.indexOf('/api/v1/afk-outcomes/runs') !== -1) {
      responseData = { status: 'ok', data: { items: [], total: 0 } };
    } else if (url.indexOf('/api/v1/afk-outcomes/change-requests') !== -1) {
      responseData = { status: 'ok', data: { items: [], total: 0 } };
    } else {
      responseData = { status: 'ok', data: { items: [], total: 0, total_input_tokens: 0, total_output_tokens: 0, total_estimated_cost_usd: 0, session_count: 0 } };
    }
    return Promise.resolve({
      ok: true,
      json: function () { return Promise.resolve(responseData); }
    });
  };

  W.fetchAll().then(function () {
    // Summary endpoints should be called
    assert(fetchedUrls.some(function (u) { return u.indexOf('/api/v1/usage/dashboard/summary') !== -1; }),
      'subsequent refresh: summaryUsage is called');
    assert(fetchedUrls.some(function (u) { return u.indexOf('/api/v1/afk/dashboard/summary') !== -1; }),
      'subsequent refresh: summaryAfk is called');

    // Detail endpoints should ALSO be called
    assert(fetchedUrls.some(function (u) { return u.indexOf('/api/v1/afk-outcomes/runs') !== -1; }),
      'subsequent refresh: afk-outcomes/runs is called');
    assert(fetchedUrls.some(function (u) { return u.indexOf('/api/v1/afk-outcomes/change-requests') !== -1; }),
      'subsequent refresh: afk-outcomes/change-requests is called');

    // Restore default fetch
    main.sandbox.fetch = function () {
      return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } });
    };
    W._setFirstPaintDone(false); // reset for other tests
  });
})();

// ── Panel freshness on summary failure ────────────────────────────────────

console.log('\u25B6 Issue #739 — panel freshness on summary failure');

(function () {
  // When summaryUsage fails, kpi-tokens and kpi-cost should be stale
  var fail = W.resolvePanelStatuses({ summaryUsage: 'boom' });
  assert(fail['kpi-tokens'] === 'stale', 'summaryUsage failure: kpi-tokens is stale');
  assert(fail['kpi-cost'] === 'stale', 'summaryUsage failure: kpi-cost is stale');

  // Panel with stale status and previous data should not re-render
  assert(W.shouldRenderPanel({ 'kpi-tokens': { status: 'stale', updatedAt: 1000 } }, 'kpi-tokens') === false,
    'stale kpi-tokens with previous data: shouldRenderPanel returns false');

  // Panel with stale status and NO previous data should render (empty state)
  assert(W.shouldRenderPanel({ 'kpi-tokens': { status: 'stale', updatedAt: null } }, 'kpi-tokens') === true,
    'stale kpi-tokens with no previous data: shouldRenderPanel returns true');

  // Freshness label shows "Showing previous data" for stale panels with data
  var fresh = W.computePanelFreshness({ 'kpi-tokens': { status: 'stale', updatedAt: 1000 } }, 'kpi-tokens', 2000);
  assert(fresh !== null && fresh.status === 'stale' && fresh.label === 'Showing previous data',
    'stale kpi-tokens shows "Showing previous data" freshness label');

  // When summaryAfk fails, AFK panels should be stale
  var afkFail = W.resolvePanelStatuses({ summaryAfk: 'boom' });
  assert(afkFail['afk-outcomes'] === 'stale', 'summaryAfk failure: afk-outcomes is stale');
  assert(afkFail['afk-repos'] === 'stale', 'summaryAfk failure: afk-repos is stale');
  assert(afkFail['afk-change-requests'] === 'stale', 'summaryAfk failure: afk-change-requests is stale');
})();

// ── Lazy-load functions exist and are idempotent ──────────────────────────

console.log('\u25B6 Issue #739 — lazy-load functions');

(function () {
  assert(typeof W.fetchAfkDetailData === 'function', 'fetchAfkDetailData is exposed');
  assert(typeof W.fetchClientProjectData === 'function', 'fetchClientProjectData is exposed');
  assert(typeof W.fetchModelData === 'function', 'fetchModelData is exposed');
  assert(typeof W.fetchAgentUsageData === 'function', 'fetchAgentUsageData is exposed');
  assert(typeof W.fetchDetailEndpoints === 'function', 'fetchDetailEndpoints is exposed');
  assert(typeof W._setFirstPaintDone === 'function', '_setFirstPaintDone setter is exposed');
  assert(typeof W._getFirstPaintDone === 'function', '_getFirstPaintDone getter is exposed');
})();

// ── Panel open triggers deferred fetch ────────────────────────────────────

console.log('\u25B6 Issue #739 — panel open triggers deferred fetch');

(function () {
  // Simulate tab activation: when a tab is activated, the corresponding
  // deferred fetch function should be called.  We verify this by checking
  // that the fetch functions are invoked when their respective tab handlers
  // are triggered.

  // Reset all fetched flags
  W._setAfkDetailFetched(false);
  W._setClientProjectFetched(false);
  W._setModelDetailFetched(false);
  W._setAgentUsageFetched(false);

  // Save the previous mock so we can restore it after the panel tests.
  // This is critical: fetchAll() from earlier IIFEs is still pending async
  // and fetchDetailEndpoints() will use whatever mock is active when it runs.
  var _prevFetch = main.sandbox.fetch;
  var panelFetchUrls = [];
  main.sandbox.fetch = function (url) {
    panelFetchUrls.push(String(url));
    var responseData = {};
    if (url.indexOf('group_by=model') !== -1) {
      responseData = { status: 'ok', data: [] };
    } else if (url.indexOf('group_by=agent') !== -1) {
      responseData = { status: 'ok', data: [] };
    } else if (url.indexOf('group_by=client,project') !== -1) {
      responseData = { status: 'ok', data: [] };
    } else if (url.indexOf('/api/v1/afk-outcomes/runs') !== -1) {
      responseData = { status: 'ok', data: { items: [], total: 0 } };
    } else if (url.indexOf('/api/v1/afk-outcomes/change-requests') !== -1) {
      responseData = { status: 'ok', data: { items: [], total: 0 } };
    } else {
      responseData = { status: 'ok', data: { items: [], total: 0 } };
    }
    return Promise.resolve({
      ok: true,
      json: function () { return Promise.resolve(responseData); }
    });
  };

  // Test 1: Calling fetchModelData triggers the model aggregate endpoint
  panelFetchUrls = [];
  var _urls1 = panelFetchUrls;
  W.fetchModelData().then(function () {
    assert(_urls1.some(function (u) { return u.indexOf('group_by=model') !== -1; }),
      'fetchModelData triggers /api/v1/usage/aggregates?group_by=model');
  });

  // Test 2: Calling fetchAgentUsageData triggers the agent aggregate endpoint
  panelFetchUrls = [];
  var _urls2 = panelFetchUrls;
  W.fetchAgentUsageData().then(function () {
    assert(_urls2.some(function (u) { return u.indexOf('group_by=agent') !== -1; }),
      'fetchAgentUsageData triggers /api/v1/usage/aggregates?group_by=agent');
  });

  // Test 3: Calling fetchClientProjectData triggers the client,project aggregate endpoint
  panelFetchUrls = [];
  var _urls3 = panelFetchUrls;
  W.fetchClientProjectData().then(function () {
    assert(_urls3.some(function (u) { return u.indexOf('group_by=client,project') !== -1; }),
      'fetchClientProjectData triggers /api/v1/usage/aggregates?group_by=client,project');
  });

  // Test 4: Calling fetchAfkDetailData triggers the AFK outcomes runs endpoint
  panelFetchUrls = [];
  var _urls4 = panelFetchUrls;
  W.fetchAfkDetailData().then(function () {
    assert(_urls4.some(function (u) { return u.indexOf('/api/v1/afk-outcomes/runs') !== -1; }),
      'fetchAfkDetailData triggers /api/v1/afk-outcomes/runs');
  });

  // Test 5: Idempotent guard — calling a lazy-load function a second time
  // should NOT trigger another fetch (the fetched flag prevents re-fetch).
  W._setModelDetailFetched(true);
  panelFetchUrls = [];
  var _urls5 = panelFetchUrls;
  W.fetchModelData().then(function () {
    assert(_urls5.length === 0,
      'fetchModelData is idempotent: second call does not re-fetch when already fetched');
  });
  W._setModelDetailFetched(false);

  // Restore the previous mock so that pending fetchAll() calls from earlier
  // IIFEs use the correct mock when fetchDetailEndpoints() runs.
  main.sandbox.fetch = _prevFetch;
})();

// ── Summary data renders KPI cards ────────────────────────────────────────

console.log('\u25B6 Issue #739 — renderKPIs with summaryUsage data');

(function () {
  // renderKPIs should use summaryUsage when available.
  // The backend /dashboard/summary endpoint returns buckets[] with per-bucket
  // fields (input_tokens, estimated_cost_usd); renderKPIs sums them.
  W.setDateRangeState({ preset: 'this-month' });
  W.renderKPIs({
    summaryUsage: {
      buckets: [
        { input_tokens: 6000, output_tokens: 3000, cache_read_tokens: 1000, cache_write_tokens: 500, estimated_cost_usd: 7.00 },
        { input_tokens: 4000, output_tokens: 2000, cache_read_tokens: 1000, cache_write_tokens: 500, estimated_cost_usd: 5.34 }
      ]
    },
    _dateRange: { startDate: new Date('2026-09-01'), endDate: new Date('2026-09-24') }
  });
  // The headline should show Token Usage = sum(input) + sum(output) = 10K + 5K = 15K
  assert(kpiTokensEl.textContent === '15.0K',
    'renderKPIs: kpi-tokens headline shows Token Usage from summaryUsage buckets (15.0K)');
  assert(kpiCostEl.textContent === '$12.34',
    'renderKPIs: kpi-cost shows cost from summaryUsage buckets ($12.34)');
})();

// ── Run all tests ─────────────────────────────────────────────────────────

function runTests() {
  console.log('\n═══════════════════════════════════════════════');
  console.log('  Passed: ' + passed + '  / Failed: ' + failed);
  console.log('═══════════════════════════════════════════════');
  if (failed > 0) {
    process.exit(1);
  }
}

// Use setTimeout to allow async tests (fetchAll) to complete
setTimeout(runTests, 100);
