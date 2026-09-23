/**
 * Unit tests for AFK Dashboard Summary frontend (issue #732).
 *
 * Run with: node frontend/tests/test_afk_dashboard_summary.js
 *
 * Tests verify:
 *  - Request URL construction (filters, interval, date range)
 *  - Response rendering (buckets → rows, no double-counting)
 *  - Empty state rendering
 *  - API failure → stale panel behavior
 *  - Filter state management
 *  - Trend label derivation
 */

'use strict';

// Minimal window polyfill for Node.js test environment
if (typeof window === 'undefined') {
  var window = {};
}

var fs = require('fs');
var vm = require('vm');
var path = require('path');
var fixture = require('../fixtures/afk_dashboard_summary.js');

// ── Element registry ─────────────────────────────────────────────────────
var elementRegistry = {};

function makeFakeElement(id) {
  var listeners = {};
  return {
    id: id,
    value: '',
    disabled: false,
    innerHTML: '',
    style: {},
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

// AFK Dashboard Summary panel fakes (issue #732)
var afkDashSummaryTbody = makeFakeElement('afk-dashboard-summary-tbody');
var afkDashIntervalSelect = makeFakeElement('afk-dash-interval');
var afkDashProviderInput = makeFakeElement('afk-dash-filter-provider');
var afkDashRepositoryInput = makeFakeElement('afk-dash-filter-repository');
var afkDashFilterApply = makeFakeElement('afk-dash-filter-apply');
var afkDashFilterClear = makeFakeElement('afk-dash-filter-clear');
elementRegistry['afk-dashboard-summary-tbody'] = afkDashSummaryTbody;
elementRegistry['afk-dash-interval'] = afkDashIntervalSelect;
elementRegistry['afk-dash-filter-provider'] = afkDashProviderInput;
elementRegistry['afk-dash-filter-repository'] = afkDashRepositoryInput;
elementRegistry['afk-dash-filter-apply'] = afkDashFilterApply;
elementRegistry['afk-dash-filter-clear'] = afkDashFilterClear;

// Reuse existing element fakes from test_pure_functions.js pattern
var agentUsageTbodyEl = makeFakeElement('agent-usage-tbody');
elementRegistry['agent-usage-tbody'] = agentUsageTbodyEl;

// Browser-history stub
var historyCalls = [];
var historyReplaceCalls = [];
var historyStub = {
  pushState: function (state, title, url) { historyCalls.push(url); },
  replaceState: function (state, title, url) { historyReplaceCalls.push(url); }
};

// ── Load real app.js ─────────────────────────────────────────────────────
var appJsSandbox = null;

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
  vm.createContext(sandbox);
  vm.runInContext(adaptersSource, sandbox, { filename: 'change_request_adapters.js' });
  vm.runInContext(source, sandbox, { filename: 'app.js' });

  // Expose AFK Dashboard Summary functions on window test seam
  window.buildAfkDashboardSummaryUrl = sandboxWindow.buildAfkDashboardSummaryUrl;
  window.aggregateAfkDashboardSummaryBuckets = sandboxWindow.aggregateAfkDashboardSummaryBuckets;
  window.buildAfkDashboardSummaryRows = sandboxWindow.buildAfkDashboardSummaryRows;
  window.renderAfkDashboardSummaryTable = sandboxWindow.renderAfkDashboardSummaryTable;
  window.readAfkDashboardFiltersFromUI = sandboxWindow.readAfkDashboardFiltersFromUI;
  window.renderAfkDashboardTrendLabel = sandboxWindow.renderAfkDashboardTrendLabel;
  // Panel freshness helpers: shouldRenderPanel (already on the app.js seam)
  // and setPanelState (drives the closure's panelStates map) let the render
  // tests exercise the stale-panel early-return path.
  window.shouldRenderPanel = sandboxWindow.shouldRenderPanel;
  window.setPanelState = sandboxWindow.setPanelState;
})();

// ── Simple test runner ──────────────────────────────────────────────────

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

// ── Tests: buildAfkDashboardSummaryUrl ───────────────────────────────────

console.log('\u25B6 buildAfkDashboardSummaryUrl');

(function () {
  var url = window.buildAfkDashboardSummaryUrl({
    from_date: '2026-08-01',
    to_date: '2026-08-07'
  }, 'daily');
  assert(url.indexOf('/api/v1/afk/dashboard/summary') === 0, 'starts with correct path');
  assert(url.indexOf('from_date=2026-08-01') !== -1, 'includes from_date');
  assert(url.indexOf('to_date=2026-08-07') !== -1, 'includes to_date');
  assert(url.indexOf('interval=daily') !== -1, 'includes interval=daily');
})();

(function () {
  var url = window.buildAfkDashboardSummaryUrl({
    from_date: '2026-08-01',
    to_date: '2026-08-07',
    provider: 'github',
    repository: 'acme/web-app'
  }, 'monthly');
  assert(url.indexOf('provider=github') !== -1, 'includes provider filter');
  assert(url.indexOf('repository=acme%2Fweb-app') !== -1 || url.indexOf('repository=acme/web-app') !== -1, 'includes repository filter');
  assert(url.indexOf('interval=monthly') !== -1, 'includes interval=monthly');
})();

(function () {
  var url = window.buildAfkDashboardSummaryUrl({
    from_date: '2026-08-01',
    to_date: '2026-08-07'
  }, 'daily');
  // Omitted filters should not appear in URL
  assert(url.indexOf('provider=') === -1, 'no provider when empty');
  assert(url.indexOf('repository=') === -1, 'no repository when empty');
})();

// ── Tests: aggregateAfkDashboardSummaryBuckets ──────────────────────────

console.log('\u25B6 aggregateAfkDashboardSummaryBuckets');

(function () {
  var data = fixture.dailyResponse();
  var agg = window.aggregateAfkDashboardSummaryBuckets(data.buckets);
  assert(agg.runs_started === 5, 'sums runs_started: 3+2=5');
  assert(agg.change_requests_opened === 3, 'sums cr_opened: 2+1=3');
  assert(agg.change_requests_merged === 2, 'sums cr_merged: 1+1=2');
  assert(agg.change_requests_closed === 0, 'sums cr_closed: 0+0=0');
  assert(agg.execution_count === 9, 'sums execution_count: 5+4=9');
  assert(agg.session_count === 7, 'sums session_count: 4+3=7');
  assert(agg.input_tokens === 90000, 'sums input_tokens: 50000+40000=90000');
  assert(agg.output_tokens === 27000, 'sums output_tokens: 15000+12000=27000');
  assert(Math.abs(agg.estimated_cost_usd - 0.77) < 0.01, 'sums estimated_cost_usd: 0.42+0.35=0.77');
})();

(function () {
  var agg = window.aggregateAfkDashboardSummaryBuckets([]);
  assert(agg.runs_started === 0, 'empty buckets → 0 runs_started');
  assert(agg.estimated_cost_usd === 0, 'empty buckets → 0 cost');
})();

// ── Tests: buildAfkDashboardSummaryRows ─────────────────────────────────

console.log('\u25B6 buildAfkDashboardSummaryRows');

(function () {
  var data = fixture.dailyResponse();
  var rows = window.buildAfkDashboardSummaryRows(data);
  assert(rows.length === 2, 'returns one row per bucket');
  assert(rows[0].period_start === '2026-08-01', 'row 0 period_start matches bucket');
  assert(rows[0].provider === 'github', 'row 0 provider matches bucket');
  assert(rows[0].repository === 'acme/web-app', 'row 0 repository matches bucket');
})();

(function () {
  var data = fixture.emptyResponse();
  var rows = window.buildAfkDashboardSummaryRows(data);
  assert(rows.length === 0, 'empty buckets → empty rows');
})();

// ── Tests: renderAfkDashboardTrendLabel ─────────────────────────────────

console.log('\u25B6 renderAfkDashboardTrendLabel');

(function () {
  var label = window.renderAfkDashboardTrendLabel('daily');
  assert(label === 'Daily', 'daily → Daily');
})();

(function () {
  var label = window.renderAfkDashboardTrendLabel('monthly');
  assert(label === 'Monthly', 'monthly → Monthly');
})();

(function () {
  var label = window.renderAfkDashboardTrendLabel(null);
  assert(label === '--', 'null interval → --');
})();

// ── Tests: readAfkDashboardFiltersFromUI ────────────────────────────────

console.log('\u25B6 readAfkDashboardFiltersFromUI');

(function () {
  afkDashProviderInput.value = 'github';
  afkDashRepositoryInput.value = 'acme/web-app';
  var filters = window.readAfkDashboardFiltersFromUI();
  assert(filters.provider === 'github', 'reads provider from UI');
  assert(filters.repository === 'acme/web-app', 'reads repository from UI');
  afkDashProviderInput.value = '';
  afkDashRepositoryInput.value = '';
})();

// ── Tests: No double-counting when aggregating across providers ──────────

console.log('\u25B6 No double-counting across providers');

(function () {
  var data = fixture.multiProviderResponse();
  var agg = window.aggregateAfkDashboardSummaryBuckets(data.buckets);
  // github: 2 runs + gitlab: 1 run = 3 total (not 2 or 1)
  assert(agg.runs_started === 3, 'cross-provider sum: 2+1=3 runs_started');
  assert(agg.session_count === 3, 'cross-provider sum: 2+1=3 sessions');
  assert(agg.input_tokens === 50000, 'cross-provider sum: 30000+20000=50000 input_tokens');
  assert(Math.abs(agg.estimated_cost_usd - 0.43) < 0.01, 'cross-provider cost: 0.25+0.18=0.43');
})();

// ── Tests: renderAfkDashboardSummaryTable ──────────────────────────────

console.log('\u25B6 renderAfkDashboardSummaryTable');

// Add freshness element to registry: renderAfkDashboardSummaryTable calls
// applyPanelFreshness, which looks up #freshness-afk-dashboard-summary.
var freshnessEl = makeFakeElement('freshness-afk-dashboard-summary');
elementRegistry['freshness-afk-dashboard-summary'] = freshnessEl;

(function () {
  // Test empty state rendering
  var data = fixture.emptyResponse();
  afkDashSummaryTbody.innerHTML = '';
  window.renderAfkDashboardSummaryTable(data);
  assert(afkDashSummaryTbody.innerHTML.indexOf('No AFK dashboard data') !== -1, 'empty state renders');
})();

(function () {
  // Test with daily fixture data
  var data = fixture.dailyResponse();
  afkDashSummaryTbody.innerHTML = '';
  window.renderAfkDashboardSummaryTable(data);
  assert(afkDashSummaryTbody.innerHTML.indexOf('acme/web-app') !== -1, 'renders repository name');
  assert(afkDashSummaryTbody.innerHTML.indexOf('2026-08-01') !== -1, 'renders period_start');
  // Check totals row exists
  assert(afkDashSummaryTbody.innerHTML.indexOf('All') !== -1, 'totals row rendered');
  // Check the totals row carries the aggregated runs_started (3+2=5) and
  // execution_count (5+4=9) sums
  assert(afkDashSummaryTbody.innerHTML.indexOf('>5<') !== -1, 'totals row sums runs_started');
  assert(afkDashSummaryTbody.innerHTML.indexOf('>9<') !== -1, 'totals row sums execution_count');
  // Check the trend label is rendered from the response interval
  assert(afkDashSummaryTbody.innerHTML.indexOf('Trend: Daily') !== -1, 'trend label rendered');
})();

(function () {
  // Test stale panel behavior: set panel state to stale with prior data,
  // call renderAfkDashboardSummaryTable, and verify shouldRenderPanel
  // returns false (so the render function returns early and keeps the
  // previous content on screen).
  var data = fixture.dailyResponse();
  afkDashSummaryTbody.innerHTML = 'prior-content';
  window.setPanelState('afk-dashboard-summary', 'stale', 500000);
  assert(window.shouldRenderPanel(
    { 'afk-dashboard-summary': { status: 'stale', updatedAt: 500000 } },
    'afk-dashboard-summary') === false,
    'stale + prior data \u2192 shouldRenderPanel false');
  window.renderAfkDashboardSummaryTable(data);
  assert(afkDashSummaryTbody.innerHTML === 'prior-content',
    'stale panel keeps previous data (render returns early)');
  // Reset panel state so later renders behave normally
  window.setPanelState('afk-dashboard-summary', 'ok', Date.now());
})();

// ── Summary ──────────────────────────────────────────────────────────────

console.log('\n' + '='.repeat(60));
if (failed === 0) {
  console.log('  AFK Dashboard Summary tests: ALL ' + passed + ' PASSED');
} else {
  console.log('  AFK Dashboard Summary tests: ' + failed + ' FAILED, ' + passed + ' passed');
}
console.log('='.repeat(60));
process.exit(failed > 0 ? 1 : 0);
