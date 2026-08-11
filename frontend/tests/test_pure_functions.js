/**
 * Unit tests for Aurora Glass pure helper functions.
 *
 * Run with: node frontend/tests/test_pure_functions.js
 *
 * These tests verify the formatting, derivation, and escaping logic
 * that is extracted from frontend/app.js into testable pure functions.
 */

// Minimal window polyfill for Node.js test environment
if (typeof window === 'undefined') {
  var window = {};
}

// Load the REAL production functions from frontend/app.js.
// app.js runs as an IIFE that exposes resolveProjectLabel and the pure
// helpers on window.  Evaluating it inside a Node vm sandbox (with a
// minimal DOM stub) means these tests exercise the production code
// itself — not a copy-pasted duplicate — so the rendered markup and
// helpers are guaranteed to match what the real dashboard renders.
var fs = require('fs');
var vm = require('vm');
var path = require('path');

// The vm sandbox that runs app.js.  Kept in scope so tests can swap the
// fetch stub (e.g. to count /admin/clients calls in the background-refresh
// tests) — the sandbox object is the vm context's global object.
var appJsSandbox = null;

// Async-block completion counter for deterministic summary polling (issue N3).
// MUST be initialized at the top of the file before any test block that
// increments it — otherwise var hoisting leaves it undefined during the
// ++ calls and the = 0 assignment later clobbers the counter.
var pendingAsyncBlocks = 0;

// Element registry for the vm document stub (issue #7 filter-wiring tests).
// app.js captures its element refs via getElementById at IIFE load time, so
// fake filter-bar elements must be registered BEFORE loadRealAppJs runs —
// after load, els.* already points at the captured (null) refs.  Ids that
// are never registered still resolve to null, preserving the existing
// no-DOM behavior of every other test.
var elementRegistry = {};

/** Minimal DOM-element fake backed by the registry: value/disabled state,
 *  a classList (add/remove/toggle/contains), innerHTML storage, stored
 *  event listeners, and a no-op querySelectorAll (table render path). */
function makeFakeElement(id) {
  var listeners = {};
  return {
    id: id,
    value: '',
    disabled: false,
    innerHTML: '',
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
    querySelectorAll: function () { return []; }
  };
}

// Agent Runs filter-bar fakes (issue #7): the two date inputs, the Clear
// button, and the table tbody (renderAgentRunsTable writes its empty-state
// row there after the Clear re-fetch).  Initial state matches the real page:
// both inputs empty, Clear disabled by default.
var arFilterFromEl = makeFakeElement('ar-filter-from');
var arFilterToEl = makeFakeElement('ar-filter-to');
var arFilterClearEl = makeFakeElement('ar-filter-clear');
var arTbodyEl = makeFakeElement('agent-runs-tbody');
elementRegistry['ar-filter-from'] = arFilterFromEl;
elementRegistry['ar-filter-to'] = arFilterToEl;
elementRegistry['ar-filter-clear'] = arFilterClearEl;
elementRegistry['agent-runs-tbody'] = arTbodyEl;

(function loadRealAppJs() {
  var appJsPath = path.join(__dirname, '..', 'app.js');
  var source = fs.readFileSync(appJsPath, 'utf8');

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
    console: console,
    setTimeout: setTimeout,
    setInterval: setInterval,
    clearInterval: clearInterval,
    clearTimeout: clearTimeout,
    fetch: function () { return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } }); },
    location: { href: '' },
    navigator: {}
  };
  sandbox.window = sandboxWindow;
  appJsSandbox = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: 'app.js' });

  window.resolveProjectLabel = sandboxWindow.resolveProjectLabel;
  window.createClientCache = sandboxWindow.createClientCache;
  window.ensureClientName = sandboxWindow.ensureClientName;
  window.refreshClientCache = sandboxWindow.refreshClientCache;
  window.invalidateClientCache = sandboxWindow.invalidateClientCache;
  window.formatUpdatedAgo = sandboxWindow.formatUpdatedAgo;
  window.computePanelFreshness = sandboxWindow.computePanelFreshness;
  window.shouldRenderPanel = sandboxWindow.shouldRenderPanel;
  window.resolvePanelStatuses = sandboxWindow.resolvePanelStatuses;
  window.formatClockTime = sandboxWindow.formatClockTime;
  window.getLastRefreshedAt = sandboxWindow.getLastRefreshedAt;
  window.kpiSubtitle = sandboxWindow.kpiSubtitle;
  window.formatAgentRunTimestamp = sandboxWindow.formatAgentRunTimestamp;
  window.readFiltersFromUI = sandboxWindow.readFiltersFromUI;
  window.computeArDateFilterState = sandboxWindow.computeArDateFilterState;
  window.syncArDateFilterUI = sandboxWindow.syncArDateFilterUI;
  window.clearArDateFilters = sandboxWindow.clearArDateFilters;
  window.setupAgentRunEventHandlers = sandboxWindow.setupAgentRunEventHandlers;
})();

// ── Pure functions (duplicated from app.js for testability) ──────────────

function fmtNum(n) {
  if (n == null || isNaN(n)) return '--';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return n.toLocaleString('en-US');
}

function fmtCost(n) {
  if (n == null || isNaN(n)) return '$--';
  const num = Number(n);
  return '$' + num.toFixed(num < 0.01 ? 4 : 2);
}

/** Format a session model identifier for display.
 *  Renders an em dash (\u2014) when the model is null/absent, mirroring
 *  the Cost column fallback pattern. HTML-escapes the model string. */
function fmtModel(model) {
  if (model == null || model === '') return '\u2014';
  return escHtml(String(model));
}

function fmtDuration(start, end) {
  if (!start || !end) return '--';
  const ms = new Date(end) - new Date(start);
  if (ms < 0) return '--';
  const mins = Math.floor(ms / 60000);
  const hrs = Math.floor(mins / 60);
  const days = Math.floor(hrs / 24);
  if (days > 0) return days + 'd ' + (hrs % 24) + 'h';
  if (hrs > 0)  return hrs + 'h ' + (mins % 60) + 'm';
  return mins + 'm';
}

function fmtRelative(isoStr) {
  if (!isoStr) return '--';
  const diff = Date.now() - new Date(isoStr).getTime();
  const mins = Math.floor(diff / 60000);
  const hrs  = Math.floor(mins / 60);
  const days = Math.floor(hrs / 24);
  if (mins < 1)  return 'just now';
  if (mins < 60) return mins + 'm ago';
  if (hrs < 24)  return hrs + 'h ago';
  return days + 'd ago';
}

function deriveProvider(modelName) {
  const m = (modelName || '').toLowerCase();
  if (m.includes('gpt') || m.includes('o1') || m.includes('o3') || m.includes('o4') || m.includes('davinci')) return 'OpenAI';
  if (m.includes('claude') || m.includes('haiku') || m.includes('sonnet') || m.includes('opus')) return 'Anthropic';
  if (m.includes('gemini') || m.includes('gemma')) return 'Google';
  if (m.includes('llama') || m.includes('mistral') || m.includes('mixtral')) return 'Meta / Mistral';
  if (m.includes('deepseek')) return 'DeepSeek';
  if (m.includes('command') || m.includes('cohere')) return 'Cohere';
  if (m.includes('grok')) return 'xAI';
  return 'Other';
}

function escHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function fmtTodoProgress(completed, total) {
  if (total == null || total <= 0) return '--';
  var c = completed || 0;
  return c + '/' + total;
}

function statusBadgeClass(status) {
  if (status === 'running') return 'badge-running';
  if (status === 'stale') return 'badge-stale';
  if (status === 'completed') return 'badge-completed';
  if (status === 'blocked') return 'badge-blocked';
  return 'badge-unknown';
}

function fmtCodeChanges(n) {
  if (n == null || n <= 0) return '--';
  return fmtNum(n);
}

/** Format a project label for display.
 *  Uses the resolved project_label field from the API when available;
 *  falls back to project_id / workspace_id for backward compatibility. */
function fmtProjectLabel(obj) {
  if (obj && obj.project_label) return obj.project_label;
  var pid = obj && obj.project_id;
  var wid = obj && obj.workspace_id;
  if (pid && wid && wid !== pid) {
    return pid + ' / ' + wid;
  }
  if (pid) return pid;
  if (wid) return wid;
  return '--';
}

function truncate(str, maxLen) {
  if (!str) return '--';
  if (str.length <= maxLen) return escHtml(str);
  return escHtml(str.substring(0, maxLen)) + '&hellip;';
}

function shortUUID(id) {
  if (!id) return '--';
  return String(id).substring(0, 8);
}

/** Format a compact multi-line Token Breakdown HTML string.
 *  Flat two-line breakdown with optional cache line:
 *    {total} total
 *    {input} in | {output} out
 *    {cr} cache read [+ {cw} cache write]   (optional)
 *
 *  Where:
 *    total = input + output + cacheRead + cacheWrite
 *
 *  Cache line is omitted when both cache_read and cache_write are zero.
 *  When only cache_read > 0:  "{cr} cache read"
 *  When only cache_write > 0: "{cw} cache write"
 *  When both > 0:             "{cr} cache read + {cw} cache write"
 *
 *  @param {number|null} inputTokens
 *  @param {number|null} outputTokens
 *  @param {number|null} cacheReadTokens
 *  @param {number|null} cacheWriteTokens
 *  @returns {string} HTML for the cell content
 */
function fmtTokenBreakdownCompact(inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens) {
  var input = inputTokens || 0;
  var output = outputTokens || 0;
  var cr = cacheReadTokens || 0;
  var cw = cacheWriteTokens || 0;

  var total = input + output + cr + cw;

  var result = fmtNum(total) + ' total<br>'
    + fmtNum(input) + ' in | ' + fmtNum(output) + ' out';

  if (cr > 0 && cw > 0) {
    result += '<br>' + fmtNum(cr) + ' cache read + ' + fmtNum(cw) + ' cache write';
  } else if (cr > 0) {
    result += '<br>' + fmtNum(cr) + ' cache read';
  } else if (cw > 0) {
    result += '<br>' + fmtNum(cw) + ' cache write';
  }

  return result;
}

/** Format agent-run token cell using the shared compact Token Breakdown formatter.
 *  Delegates to fmtTokenBreakdownCompact for the shared two-line + optional cache line format.
 *  @param {number|null} inputTokens
 *  @param {number|null} outputTokens
 *  @param {number|null} cacheReadTokens
 *  @param {number|null} cacheWriteTokens
 *  @returns {string} HTML for the cell content
 */
function fmtAgentRunTokens(inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens) {
  return fmtTokenBreakdownCompact(inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens);
}

// ── Date-range pure functions (duplicated from app.js for testability) ──

/**
 * Compute a start/end Date range from a named preset.
 * @param {string} preset - 'this-month', 'last-month', 'last-30-days', 'last-7-days'
 * @param {Date} [now] - reference date (for testing); defaults to new Date()
 * @returns {{ startDate: Date, endDate: Date }}
 */
function computeDateRange(preset, now) {
  now = now || new Date();
  var startDate, endDate;

  switch (preset) {
    case 'this-month':
      startDate = new Date(now.getFullYear(), now.getMonth(), 1);
      startDate.setHours(0, 0, 0, 0);
      endDate = now;
      break;
    case 'last-month':
      startDate = new Date(now.getFullYear(), now.getMonth() - 1, 1);
      startDate.setHours(0, 0, 0, 0);
      endDate = new Date(now.getFullYear(), now.getMonth(), 1);
      endDate.setHours(0, 0, 0, 0);
      break;
    case 'last-30-days':
      startDate = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);
      startDate.setHours(0, 0, 0, 0);
      endDate = now;
      break;
    case 'last-7-days':
      startDate = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);
      startDate.setHours(0, 0, 0, 0);
      endDate = now;
      break;
    default:
      startDate = new Date(now.getFullYear(), now.getMonth(), 1);
      startDate.setHours(0, 0, 0, 0);
      endDate = now;
  }

  return { startDate: startDate, endDate: endDate };
}

/**
 * Format a date range as a human-readable label.
 * @param {Date} startDate
 * @param {Date} endDate
 * @returns {string} e.g. "Jul 1–27, 2026"
 */
function formatRangeLabel(startDate, endDate) {
  if (!startDate || !endDate) return '--';
  var monthDayOpts = { month: 'short', day: 'numeric' };
  var fullOpts = { month: 'short', day: 'numeric', year: 'numeric' };
  var startYear = startDate.getFullYear();
  var endYear = endDate.getFullYear();
  var startMonth = startDate.getMonth();
  var endMonth = endDate.getMonth();

  if (startYear === endYear && startMonth === endMonth) {
    // Same month and year: "Jul 1–27, 2026"
    var m = startDate.toLocaleDateString('en-US', { month: 'short' });
    return m + ' ' + startDate.getDate() + '\u2013' + endDate.getDate() + ', ' + startYear;
  } else if (startYear === endYear) {
    // Different months, same year: "Jun 28–Jul 27, 2026"
    return startDate.toLocaleDateString('en-US', monthDayOpts) + '\u2013' + endDate.toLocaleDateString('en-US', monthDayOpts) + ', ' + startYear;
  } else {
    // Different years: "Dec 28, 2025–Jan 27, 2026"
    return startDate.toLocaleDateString('en-US', fullOpts) + '\u2013' + endDate.toLocaleDateString('en-US', fullOpts);
  }
}

// ── Simple test runner ──────────────────────────────────────────────────

let passed = 0;
let failed = 0;

function assert(condition, label) {
  if (condition) {
    passed++;
  } else {
    failed++;
    console.error('  \u2717 FAIL:', label);
  }
}

// ── Tests for fmtNum ────────────────────────────────────────────────────

console.log('\u25B6 fmtNum');

assert(fmtNum(null) === '--', 'null \u2192 --');
assert(fmtNum(undefined) === '--', 'undefined \u2192 --');
assert(fmtNum(NaN) === '--', 'NaN \u2192 --');
assert(fmtNum(0) === '0', '0 \u2192 0');
assert(fmtNum(42) === '42', '42 \u2192 42');
assert(fmtNum(1000) === '1.0K', '1000 \u2192 1.0K');
assert(fmtNum(1500) === '1.5K', '1500 \u2192 1.5K');
assert(fmtNum(1000000) === '1.0M', '1000000 \u2192 1.0M');
assert(fmtNum(2500000) === '2.5M', '2500000 \u2192 2.5M');
assert(fmtNum(999) === '999', '999 \u2192 999 (below K threshold)');

// ── Tests for fmtCost ───────────────────────────────────────────────────

console.log('\u25B6 fmtCost');

assert(fmtCost(null) === '$--', 'null \u2192 $--');
assert(fmtCost(undefined) === '$--', 'undefined \u2192 $--');
assert(fmtCost(NaN) === '$--', 'NaN \u2192 $--');
assert(fmtCost(0) === '$0.0000', '0 \u2192 $0.0000');
assert(fmtCost(1.5) === '$1.50', '1.5 \u2192 $1.50');
assert(fmtCost(0.005) === '$0.0050', '0.005 \u2192 $0.0050 (4 decimal places)');
assert(fmtCost(0.01) === '$0.01', '0.01 \u2192 $0.01 (2 decimal places)');
assert(fmtCost(123.456) === '$123.46', '123.456 \u2192 $123.46');
assert(fmtCost(0.00123) === '$0.0012', '0.00123 \u2192 $0.0012');

// ── Tests for fmtModel ──────────────────────────────────────────────────

console.log('\u25B6 fmtModel');

assert(fmtModel(null) === '\u2014', 'null \u2192 em dash');
assert(fmtModel(undefined) === '\u2014', 'undefined \u2192 em dash');
assert(fmtModel('') === '\u2014', 'empty string \u2192 em dash');
assert(fmtModel('claude-sonnet-4-20250514') === 'claude-sonnet-4-20250514', 'model string unchanged');
assert(fmtModel('<script>') === '&lt;script&gt;', 'HTML escaped');

// ── Tests for fmtDuration ───────────────────────────────────────────────

console.log('\u25B6 fmtDuration');

assert(fmtDuration(null, null) === '--', 'null inputs \u2192 --');
assert(fmtDuration('', '') === '--', 'empty inputs \u2192 --');
assert(fmtDuration('2026-01-01T00:00:00Z', '2026-01-01T00:05:00Z') === '5m', '5 minutes \u2192 5m');
assert(fmtDuration('2026-01-01T00:00:00Z', '2026-01-01T01:30:00Z') === '1h 30m', '1h30m \u2192 1h 30m');
assert(fmtDuration('2026-01-01T00:00:00Z', '2026-01-03T05:00:00Z') === '2d 5h', '2d5h \u2192 2d 5h');
assert(fmtDuration('2026-01-03T05:00:00Z', '2026-01-01T00:00:00Z') === '--', 'negative duration \u2192 --');

// ── Tests for fmtRelative ───────────────────────────────────────────────

console.log('\u25B6 fmtRelative');

assert(fmtRelative(null) === '--', 'null \u2192 --');
assert(fmtRelative('') === '--', 'empty string \u2192 --');

// (Note: fmtRelative depends on Date.now(), so only structural tests)

// ── Tests for deriveProvider ────────────────────────────────────────────

console.log('\u25B6 deriveProvider');

assert(deriveProvider('gpt-4') === 'OpenAI', 'gpt-4 \u2192 OpenAI');
assert(deriveProvider('o1-preview') === 'OpenAI', 'o1-preview \u2192 OpenAI');
assert(deriveProvider('o3-mini') === 'OpenAI', 'o3-mini \u2192 OpenAI');
assert(deriveProvider('claude-3-opus') === 'Anthropic', 'claude-3-opus \u2192 Anthropic');
assert(deriveProvider('claude-sonnet-4') === 'Anthropic', 'claude-sonnet-4 \u2192 Anthropic');
assert(deriveProvider('gemini-pro') === 'Google', 'gemini-pro \u2192 Google');
assert(deriveProvider('llama-3.1-70b') === 'Meta / Mistral', 'llama-3.1-70b \u2192 Meta / Mistral');
assert(deriveProvider('mistral-large') === 'Meta / Mistral', 'mistral-large \u2192 Meta / Mistral');
assert(deriveProvider('deepseek-chat') === 'DeepSeek', 'deepseek-chat \u2192 DeepSeek');
assert(deriveProvider('command-r') === 'Cohere', 'command-r \u2192 Cohere');
assert(deriveProvider('grok-2') === 'xAI', 'grok-2 \u2192 xAI');
assert(deriveProvider('unknown-model-xyz') === 'Other', 'unknown \u2192 Other');
assert(deriveProvider(null) === 'Other', 'null \u2192 Other');
assert(deriveProvider('') === 'Other', 'empty \u2192 Other');

// ── Tests for escHtml ───────────────────────────────────────────────────

console.log('\u25B6 escHtml');

assert(escHtml(null) === '', 'null \u2192 empty');
assert(escHtml('') === '', 'empty \u2192 empty');
assert(escHtml('hello') === 'hello', 'plain text unchanged');
assert(escHtml('<script>') === '&lt;script&gt;', '<script> escaped');
assert(escHtml('a&b') === 'a&amp;b', '& escaped');
assert(escHtml('"quote"') === '&quot;quote&quot;', 'double quotes escaped');
assert(escHtml("it's") === 'it&#39;s', 'single quotes escaped');
assert(escHtml('<a href="x">') === '&lt;a href=&quot;x&quot;&gt;', 'combined escaping');

// ── Tests for fmtTodoProgress ───────────────────────────────────────────

console.log('\u25B6 fmtTodoProgress');

assert(fmtTodoProgress(null, null) === '--', 'null inputs → --');
assert(fmtTodoProgress(undefined, undefined) === '--', 'undefined inputs → --');
assert(fmtTodoProgress(0, 0) === '--', 'zero total → --');
assert(fmtTodoProgress(3, 5) === '3/5', '3/5 → 3/5');
assert(fmtTodoProgress(0, 10) === '0/10', '0/10 → 0/10');
assert(fmtTodoProgress(5, 5) === '5/5', '5/5 → 5/5');
assert(fmtTodoProgress(2, null) === '--', 'null total → --');

// ── Tests for statusBadgeClass ──────────────────────────────────────────

console.log('\u25B6 statusBadgeClass');

assert(statusBadgeClass('running') === 'badge-running', 'running → badge-running');
assert(statusBadgeClass('stale') === 'badge-stale', 'stale → badge-stale');
assert(statusBadgeClass('completed') === 'badge-completed', 'completed → badge-completed');
assert(statusBadgeClass('blocked') === 'badge-blocked', 'blocked → badge-blocked');
assert(statusBadgeClass('unknown') === 'badge-unknown', 'unknown → badge-unknown');
assert(statusBadgeClass('anything-else') === 'badge-unknown', 'anything else → badge-unknown');
assert(statusBadgeClass(null) === 'badge-unknown', 'null → badge-unknown');
assert(statusBadgeClass('') === 'badge-unknown', 'empty → badge-unknown');

// ── Tests for currentStatus badge fallback ────────────────────────────────

console.log('\u25B6 currentStatus badge fallback');

/**
 * The two-field contract: render the status badge using currentStatus when
 * available, falling back to status for backward compatibility.  This is
 * used in renderAgentRunsTable, renderAgentRunDetail (parent), and child
 * summaries within renderAgentRunDetail.
 *
 * @param {object} run - { currentStatus?: string, status?: string }
 * @returns {string} CSS badge class from statusBadgeClass
 */
function resolveBadgeStatus(run) {
  return statusBadgeClass(run && (run.currentStatus || run.status));
}

// When currentStatus is present, it takes precedence
assert(resolveBadgeStatus({ currentStatus: 'running', status: 'completed' }) === 'badge-running',
  'currentStatus running wins over status completed');
assert(resolveBadgeStatus({ currentStatus: 'stale', status: 'running' }) === 'badge-stale',
  'currentStatus stale wins over status running');
assert(resolveBadgeStatus({ currentStatus: 'blocked', status: 'running' }) === 'badge-blocked',
  'currentStatus blocked wins over status running');
assert(resolveBadgeStatus({ currentStatus: 'completed', status: 'unknown' }) === 'badge-completed',
  'currentStatus completed wins over status unknown');
assert(resolveBadgeStatus({ currentStatus: 'unknown', status: 'running' }) === 'badge-unknown',
  'currentStatus unknown wins over status running');

// When currentStatus is absent, falls back to status
assert(resolveBadgeStatus({ status: 'running' }) === 'badge-running',
  'missing currentStatus falls back to status running');
assert(resolveBadgeStatus({ status: 'stale' }) === 'badge-stale',
  'missing currentStatus falls back to status stale');
assert(resolveBadgeStatus({ status: 'blocked' }) === 'badge-blocked',
  'missing currentStatus falls back to status blocked');

// When both are absent, returns badge-unknown
assert(resolveBadgeStatus({}) === 'badge-unknown',
  'both missing → badge-unknown');
assert(resolveBadgeStatus(null) === 'badge-unknown',
  'null run → badge-unknown');
assert(resolveBadgeStatus(undefined) === 'badge-unknown',
  'undefined run → badge-unknown');

// Same value for both fields
assert(resolveBadgeStatus({ currentStatus: 'running', status: 'running' }) === 'badge-running',
  'currentStatus == status → badge-running');

// ── Tests for fmtCodeChanges ────────────────────────────────────────────

console.log('\u25B6 fmtCodeChanges');

assert(fmtCodeChanges(null) === '--', 'null → --');
assert(fmtCodeChanges(undefined) === '--', 'undefined → --');
assert(fmtCodeChanges(0) === '--', '0 → --');
assert(fmtCodeChanges(1) === '1', '1 → 1');
assert(fmtCodeChanges(42) === '42', '42 → 42');
assert(fmtCodeChanges(1000) === '1.0K', '1000 → 1.0K');
assert(fmtCodeChanges(-1) === '--', '-1 → --');

// ── Tests for fmtProjectLabel ───────────────────────────────────────

console.log('\u25B6 fmtProjectLabel');

assert(fmtProjectLabel(null) === '--', 'null → --');
assert(fmtProjectLabel(undefined) === '--', 'undefined → --');
assert(fmtProjectLabel({}) === '--', 'empty object → --');

assert(fmtProjectLabel({ project_label: 'My Project' }) === 'My Project', 'project_label wins');
assert(fmtProjectLabel({ project_label: 'My Project', project_id: 'abc123' }) === 'My Project', 'project_label wins over project_id');
assert(fmtProjectLabel({ project_label: 'My Project', project_id: 'abc123', workspace_id: 'ws456' }) === 'My Project', 'project_label wins over both');

assert(fmtProjectLabel({ project_id: 'abc123' }) === 'abc123', 'project_id alone → project_id');
assert(fmtProjectLabel({ workspace_id: 'ws456' }) === 'ws456', 'workspace_id alone → workspace_id');
assert(fmtProjectLabel({ project_id: 'abc123', workspace_id: 'abc123' }) === 'abc123', 'project_id equals workspace_id → just project_id');
assert(fmtProjectLabel({ project_id: 'abc123', workspace_id: 'ws456' }) === 'abc123 / ws456', 'both present and different → joined');

assert(fmtProjectLabel({ project_label: '' }) === '--', 'empty project_label falls through to default');
assert(fmtProjectLabel({ project_label: '', project_id: 'abc123' }) === 'abc123', 'empty project_label falls through to project_id');

// ── Tests for Client/Project Breakdown project label display ─────────────

/**
 * Delegates to the production resolveProjectLabel() from app.js.
 * Using window.resolveProjectLabel ensures tests always exercise
 * the single source of truth, preventing silent drift between
 * test expectations and production display logic.
 *
 * After the backend fix for issue #313, the group_value's pipe-delimited
 * project part is the resolved Project Label (not raw external_project_id).
 * This means both projectLabel (from API response) and projectId (from
 * group_value split) contain friendly labels.  resolveProjectLabel() must
 * still prefer the API's project_label field and fall back to projectId.
 */
function resolveCPProjectLabel(row) {
  return window.resolveProjectLabel(row);
}

console.log('\u25B6 resolveCPProjectLabel');

// When project_label is available from the API, it takes precedence
assert(resolveCPProjectLabel({ projectLabel: 'My Web App', projectId: 'proj-abc' }) === 'My Web App',
  'projectLabel wins over projectId');

// When project_label is null/empty, falls back to projectId
assert(resolveCPProjectLabel({ projectLabel: null, projectId: 'proj-abc' }) === 'proj-abc',
  'null projectLabel falls back to projectId');
assert(resolveCPProjectLabel({ projectLabel: '', projectId: 'proj-xyz' }) === 'proj-xyz',
  'empty string projectLabel falls back to projectId');

// When project_label is undefined (missing), falls back to projectId
assert(resolveCPProjectLabel({ projectId: 'proj-abc' }) === 'proj-abc',
  'missing projectLabel falls back to projectId');

// When both are missing, returns 'unknown'
assert(resolveCPProjectLabel({}) === 'unknown', 'empty object \u2192 unknown');
assert(resolveCPProjectLabel({ projectLabel: null, projectId: null }) === 'unknown',
  'null projectLabel and null projectId \u2192 unknown');

// Typical API responses
assert(resolveCPProjectLabel({ projectLabel: 'Friendly Name', projectId: 'proj-abc' }) === 'Friendly Name',
  'API with resolved label uses it');
assert(resolveCPProjectLabel({ projectLabel: 'unknown', projectId: 'unknown' }) === 'unknown',
  'API with unknown label uses unknown');

// ── Post-issue-#313: group_value project part IS the project label ─────

// When project_label from API is present and matches the group_value
assert(resolveCPProjectLabel({ projectLabel: 'My App', projectId: 'My App' }) === 'My App',
  'both fields contain same label \u2192 returns label');

// When only projectId contains the resolved label (API project_label is null)
assert(resolveCPProjectLabel({ projectLabel: null, projectId: 'My App' }) === 'My App',
  'null projectLabel falls back to projectId which now contains the label');

// When only projectId contains the resolved label (API project_label is empty)
assert(resolveCPProjectLabel({ projectLabel: '', projectId: 'Database Backup Tool' }) === 'Database Backup Tool',
  'empty projectLabel falls back to friendly label in projectId');

// Verify no raw external project IDs appear in cells for this view
// (projectLabel wins when available, projectId now holds the label)
assert(resolveCPProjectLabel({ projectLabel: 'Friendly Name', projectId: 'raw-id-abc' }) === 'Friendly Name',
  'raw ID in projectId is hidden when projectLabel is present');
assert(resolveCPProjectLabel({ projectLabel: 'Friendly Name', projectId: 'Friendly Name' }) === 'Friendly Name',
  'projectLabel used when both are the friendly label');

// Edge case: project_label from API is 'unknown' (COALESCE fallback),
// group_value project part is also 'unknown' — should display 'unknown'
assert(resolveCPProjectLabel({ projectLabel: 'unknown', projectId: 'unknown' }) === 'unknown',
  'unknown fallback displays as unknown');

// ── Tests for truncate ──────────────────────────────────────────────────

console.log('\u25B6 truncate');

assert(truncate(null, 10) === '--', 'null → --');
assert(truncate('', 10) === '--', 'empty → --');
assert(truncate('hello', 10) === 'hello', 'short string unchanged');
assert(truncate('hello world', 5) === 'hello&hellip;', 'long string truncated with ellipsis');
assert(truncate('hello', 5) === 'hello', 'exact length unchanged');
assert(truncate('<script>', 10) === '&lt;script&gt;', 'html escaped');

// ── Tests for shortUUID ─────────────────────────────────────────────────

console.log('\u25B6 shortUUID');

assert(shortUUID(null) === '--', 'null → --');
assert(shortUUID(undefined) === '--', 'undefined → --');
assert(shortUUID('') === '--', 'empty → --');
assert(shortUUID('550e8400-e29b-41d4-a716-446655440000') === '550e8400', 'UUID truncated to 8 chars');
assert(shortUUID('abc') === 'abc', 'short string returned as-is');

// ── Tests for computeDateRange ──────────────────────────────────────────

console.log('\u25B6 computeDateRange');

// Use fixed reference date: 2026-07-27 14:30:00 local
var refDate = new Date(2026, 6, 27, 14, 30, 0, 0);

(function () {
  var r = computeDateRange('this-month', refDate);
  assert(r.startDate instanceof Date, 'this-month: startDate is a Date');
  assert(r.endDate instanceof Date, 'this-month: endDate is a Date');
  assert(r.startDate <= r.endDate, 'this-month: startDate <= endDate');
  assert(r.startDate.getFullYear() === 2026, 'this-month: startDate year is 2026');
  assert(r.startDate.getMonth() === 6, 'this-month: startDate month is July (6)');
  assert(r.startDate.getDate() === 1, 'this-month: startDate day is 1');
  assert(r.startDate.getHours() === 0, 'this-month: startDate hours is 0');
  assert(r.startDate.getMinutes() === 0, 'this-month: startDate minutes is 0');
  assert(r.endDate.getTime() === refDate.getTime(), 'this-month: endDate === refDate');
})();

(function () {
  var r = computeDateRange('last-month', refDate);
  assert(r.startDate instanceof Date, 'last-month: startDate is a Date');
  assert(r.endDate instanceof Date, 'last-month: endDate is a Date');
  assert(r.startDate <= r.endDate, 'last-month: startDate <= endDate');
  assert(r.startDate.getFullYear() === 2026, 'last-month: startDate year is 2026');
  assert(r.startDate.getMonth() === 5, 'last-month: startDate month is June (5)');
  assert(r.startDate.getDate() === 1, 'last-month: startDate day is 1');
  assert(r.endDate.getFullYear() === 2026, 'last-month: endDate year is 2026');
  assert(r.endDate.getMonth() === 6, 'last-month: endDate month is July (6)');
  assert(r.endDate.getDate() === 1, 'last-month: endDate day is 1');
  assert(r.endDate.getHours() === 0, 'last-month: endDate hours is 0');
})();

(function () {
  var r = computeDateRange('last-30-days', refDate);
  // 30 days before July 27 is June 27
  assert(r.startDate instanceof Date, 'last-30-days: startDate is a Date');
  assert(r.endDate instanceof Date, 'last-30-days: endDate is a Date');
  assert(r.startDate <= r.endDate, 'last-30-days: startDate <= endDate');
  assert(r.startDate.getFullYear() === 2026, 'last-30-days: startDate year is 2026');
  assert(r.startDate.getMonth() === 5, 'last-30-days: startDate month is June (5)');
  assert(r.startDate.getDate() === 27, 'last-30-days: startDate day is 27');
  assert(r.startDate.getHours() === 0, 'last-30-days: startDate hours is 0');
  assert(r.startDate.getMinutes() === 0, 'last-30-days: startDate minutes is 0');
  assert(r.endDate.getTime() === refDate.getTime(), 'last-30-days: endDate === refDate');
})();

(function () {
  var r = computeDateRange('last-7-days', refDate);
  // 7 days before July 27 is July 20
  assert(r.startDate instanceof Date, 'last-7-days: startDate is a Date');
  assert(r.endDate instanceof Date, 'last-7-days: endDate is a Date');
  assert(r.startDate <= r.endDate, 'last-7-days: startDate <= endDate');
  assert(r.startDate.getFullYear() === 2026, 'last-7-days: startDate year is 2026');
  assert(r.startDate.getMonth() === 6, 'last-7-days: startDate month is July (6)');
  assert(r.startDate.getDate() === 20, 'last-7-days: startDate day is 20');
  assert(r.startDate.getHours() === 0, 'last-7-days: startDate hours is 0');
  assert(r.startDate.getMinutes() === 0, 'last-7-days: startDate minutes is 0');
  assert(r.endDate.getTime() === refDate.getTime(), 'last-7-days: endDate === refDate');
})();

(function () {
  var r = computeDateRange('unknown-preset', refDate);
  assert(r.startDate instanceof Date, 'unknown preset: startDate is a Date');
  assert(r.endDate instanceof Date, 'unknown preset: endDate is a Date');
  // Default should fall back to this-month behavior
  assert(r.startDate.getDate() === 1, 'unknown preset: defaults to 1st of month');
  assert(r.startDate.getMonth() === 6, 'unknown preset: defaults to July');
})();

(function () {
  // January test for last-month crossing year boundary
  var janRef = new Date(2026, 0, 15, 12, 0, 0, 0); // Jan 15, 2026
  var r = computeDateRange('last-month', janRef);
  assert(r.startDate.getFullYear() === 2025, 'last-month (Jan): startDate year is 2025');
  assert(r.startDate.getMonth() === 11, 'last-month (Jan): startDate month is December (11)');
  assert(r.startDate.getDate() === 1, 'last-month (Jan): startDate day is 1');
  assert(r.endDate.getFullYear() === 2026, 'last-month (Jan): endDate year is 2026');
  assert(r.endDate.getMonth() === 0, 'last-month (Jan): endDate month is January (0)');
  assert(r.endDate.getDate() === 1, 'last-month (Jan): endDate day is 1');
})();

// ── Tests for formatRangeLabel ──────────────────────────────────────────

console.log('\u25B6 formatRangeLabel');

(function () {
  // Same month and year: "Jul 1–27, 2026"
  var start = new Date(2026, 6, 1);
  var end = new Date(2026, 6, 27);
  var label = formatRangeLabel(start, end);
  assert(label.indexOf('Jul') !== -1, 'same month: contains month abbreviation');
  assert(label.indexOf('1') !== -1, 'same month: contains start day');
  assert(label.indexOf('27') !== -1, 'same month: contains end day');
  assert(label.indexOf('2026') !== -1, 'same month: contains year');
  assert(label.indexOf('\u2013') !== -1, 'same month: uses en-dash separator');
})();

(function () {
  // Different months, same year: "Jun 28–Jul 27, 2026"
  var start = new Date(2026, 5, 28);
  var end = new Date(2026, 6, 27);
  var label = formatRangeLabel(start, end);
  assert(label.indexOf('Jun') !== -1, 'diff month same year: contains Jun');
  assert(label.indexOf('Jul') !== -1, 'diff month same year: contains Jul');
  assert(label.indexOf('2026') !== -1, 'diff month same year: contains year');
  assert(label.indexOf('\u2013') !== -1, 'diff month same year: uses en-dash');
})();

(function () {
  // Different years: "Dec 28, 2025–Jan 27, 2026"
  var start = new Date(2025, 11, 28);
  var end = new Date(2026, 0, 27);
  var label = formatRangeLabel(start, end);
  assert(label.indexOf('Dec') !== -1, 'diff year: contains Dec');
  assert(label.indexOf('Jan') !== -1, 'diff year: contains Jan');
  assert(label.indexOf('2025') !== -1, 'diff year: contains start year');
  assert(label.indexOf('2026') !== -1, 'diff year: contains end year');
  assert(label.indexOf('\u2013') !== -1, 'diff year: uses en-dash');
})();

(function () {
  // Null/undefined protection
  assert(formatRangeLabel(null, null) === '--', 'null inputs → --');
  assert(formatRangeLabel(undefined, undefined) === '--', 'undefined inputs → --');
})();

// ── Date-range helper: resolveDateRange ──────────────────────────────

/**
 * Resolve a date range from state, handling both preset and custom.
 * This is a wrapper that either delegates to computeDateRange for named
 * presets or constructs Date objects from custom date strings.
 * @param {Object} state - { preset, customStartDate?, customEndDate? }
 * @returns {{ startDate: Date, endDate: Date }}
 */
function resolveDateRange(state) {
  if (state.preset === 'custom' && state.customStartDate && state.customEndDate) {
    var startDate = new Date(state.customStartDate + 'T00:00:00Z');
    var endDate = new Date(state.customEndDate + 'T23:59:59Z');
    if (isNaN(startDate.getTime()) || isNaN(endDate.getTime())) {
      return computeDateRange('this-month');
    }
    return { startDate: startDate, endDate: endDate };
  }
  return computeDateRange(state.preset);
}

console.log('\u25B6 resolveDateRange');

(function () {
  // Preset delegation
  var refDate = new Date(2026, 6, 27, 14, 30, 0, 0);
  var origCompute = computeDateRange;

  // Temporarily override Date.now for preset test
  var state = { preset: 'last-7-days' };
  var r = resolveDateRange(state);
  assert(r.startDate instanceof Date, 'preset: startDate is a Date');
  assert(r.endDate instanceof Date, 'preset: endDate is a Date');
  assert(r.startDate <= r.endDate, 'preset: startDate <= endDate');
})();

(function () {
  // Custom date range
  var state = {
    preset: 'custom',
    customStartDate: '2026-06-01',
    customEndDate: '2026-06-30'
  };
  var r = resolveDateRange(state);
  assert(r.startDate instanceof Date, 'custom: startDate is a Date');
  assert(r.endDate instanceof Date, 'custom: endDate is a Date');
  assert(r.startDate <= r.endDate, 'custom: startDate <= endDate');
  assert(r.startDate.getUTCFullYear() === 2026, 'custom: startDate year is 2026');
  assert(r.startDate.getUTCMonth() === 5, 'custom: startDate month is June (5)');
  assert(r.startDate.getUTCDate() === 1, 'custom: startDate day is 1');
  assert(r.endDate.getUTCFullYear() === 2026, 'custom: endDate year is 2026');
  assert(r.endDate.getUTCMonth() === 5, 'custom: endDate month is June (5)');
  assert(r.endDate.getUTCDate() === 30, 'custom: endDate day is 30');
  // Check UTC hours — T00:00:00Z for start, T23:59:59Z for end
  assert(r.startDate.getUTCHours() === 0, 'custom: startDate UTC hours is 0');
  assert(r.endDate.getUTCHours() === 23, 'custom: endDate UTC hours is 23');
  assert(r.endDate.getUTCMinutes() === 59, 'custom: endDate UTC minutes is 59');
  assert(r.endDate.getUTCSeconds() === 59, 'custom: endDate UTC seconds is 59');
})();

(function () {
  // Custom with missing dates falls back to preset
  var state = { preset: 'custom' };
  var r = resolveDateRange(state);
  // Should fall back to default (this-month) behavior
  assert(r.startDate instanceof Date, 'custom no dates: startDate is a Date');
  assert(r.endDate instanceof Date, 'custom no dates: endDate is a Date');
})();

(function () {
  // Custom with only start date falls back to preset
  var state = { preset: 'custom', customStartDate: '2026-06-01' };
  var r = resolveDateRange(state);
  assert(r.startDate instanceof Date, 'custom partial: startDate is a Date');
  assert(r.endDate instanceof Date, 'custom partial: endDate is a Date');
})();

// ── Drilldown State: helpers for client expansion preservation ──────────

/**
 * Strip expand/collapse icon characters from a client name cell text.
 * The expand icon (▶ or ▼) is rendered as the first child of the <td>,
 * so textContent includes it. This helper extracts just the client name.
 * @param {string} text - textContent from the first <td> of a client row
 * @returns {string} cleaned client name
 */
function stripExpandIcon(text) {
  if (!text) return '';
  return text.replace('\u25B6', '').replace('\u25BC', '').trim();
}

/**
 * Compute the set of client names that should remain expanded,
 * given the previously expanded names and the current list of client names.
 * Names that no longer exist in the current data are dropped.
 * This simulates the restoration logic used by renderClientProjectBreakdown.
 * @param {Object} prevExpanded - { clientName: true, ... }
 * @param {string[]} currentNames - list of client names in the current render
 * @returns {Object} { clientName: true, ... } — subset that still exists
 */
function filterExpandedClients(prevExpanded, currentNames) {
  var result = {};
  currentNames.forEach(function (name) {
    if (prevExpanded[name]) result[name] = true;
  });
  return result;
}

console.log('\u25B6 drilldown state helpers');

// ── Tests for stripExpandIcon ───────────────────────────────────────────

assert(stripExpandIcon(null) === '', 'null → empty');
assert(stripExpandIcon('') === '', 'empty → empty');
assert(stripExpandIcon('Client A') === 'Client A', 'plain name unchanged');
assert(stripExpandIcon('▶Client A') === 'Client A', 'leading play icon stripped');
assert(stripExpandIcon('▼Client A') === 'Client A', 'leading down-arrow stripped');
assert(stripExpandIcon('  ▶Client A  ') === 'Client A', 'whitespace trimmed');
assert(stripExpandIcon('▶  Client A') === 'Client A', 'extra whitespace after icon');
assert(stripExpandIcon('A Client Named ▶') === 'A Client Named', 'icon stripped anywhere in text');
assert(stripExpandIcon('Client A▶') === 'Client A', 'trailing icon stripped');
assert(stripExpandIcon('  ') === '', 'whitespace only → empty');

// ── Tests for filterExpandedClients ──────────────────────────────────────

(function () {
  // Basic: one expanded client, still exists
  var prev = { 'Client A': true };
  var current = ['Client A', 'Client B'];
  var result = filterExpandedClients(prev, current);
  assert(result['Client A'] === true, 'Client A remains expanded');
  assert(result['Client B'] === undefined, 'Client B was not previously expanded');
})();

(function () {
  // Expanded client no longer in current data
  var prev = { 'Client A': true, 'Client B': true };
  var current = ['Client B'];
  var result = filterExpandedClients(prev, current);
  assert(result['Client A'] === undefined, 'Client A dropped (no longer in data)');
  assert(result['Client B'] === true, 'Client B remains expanded');
})();

(function () {
  // Empty previous state
  var prev = {};
  var current = ['Client A', 'Client B'];
  var result = filterExpandedClients(prev, current);
  assert(Object.keys(result).length === 0, 'empty previous → no expanded clients');
})();

(function () {
  // Unknown client in prevExpanded — does not appear in current
  var prev = { 'Ghost Client': true };
  var current = ['Client A'];
  var result = filterExpandedClients(prev, current);
  assert(result['Ghost Client'] === undefined, 'unknown client not in current data');
  assert(Object.keys(result).length === 0, 'no match → empty result');
})();

(function () {
  // Stability: keyed by name, not position. If names shift order, expansion follows name.
  var prev = { 'Client B': true, 'Client A': true };
  var current = ['Client A', 'Client B'];
  var result = filterExpandedClients(prev, current);
  assert(result['Client A'] === true, 'Client A expanded regardless of position');
  assert(result['Client B'] === true, 'Client B expanded regardless of position');
})();

(function () {
  // Duplicate names in current list — last occurrence wins (same behavior as the render loop)
  var prev = { 'Client A': true };
  var current = ['Client B', 'Client A', 'Client A'];
  var result = filterExpandedClients(prev, current);
  assert(result['Client A'] === true, 'Client A expanded despite duplicate name');
})();

// ── Tests for fmtTokenBreakdownCompact ────────────────────────────────────

console.log('\u25B6 fmtTokenBreakdownCompact');

// Normal case with cache hit — flat two-line + cache read line
(function () {
  var result = fmtTokenBreakdownCompact(38800, 5200, 23400, 0);
  // total=67.4K (input + output + cacheRead + cacheWrite)
  assert(result === '67.4K total<br>38.8K in | 5.2K out<br>23.4K cache read', 'normal cache hit: flat two-line + cache read line');
})();

// Case with both cache read and cache write
(function () {
  var result = fmtTokenBreakdownCompact(38800, 5200, 23400, 4200);
  // total=71.6K (input + output + cacheRead + cacheWrite)
  assert(result === '71.6K total<br>38.8K in | 5.2K out<br>23.4K cache read + 4.2K cache write', 'cache read + write: both on cache line');
})();

// Zero-cache case — no cache line
(function () {
  var result = fmtTokenBreakdownCompact(38800, 5200, 0, 0);
  // total=44.0K
  assert(result === '44.0K total<br>38.8K in | 5.2K out', 'zero cache: no cache line');
})();

// All nulls — no cache line
(function () {
  var result = fmtTokenBreakdownCompact(null, null, null, null);
  assert(result === '0 total<br>0 in | 0 out', 'all nulls: no cache line');
})();

// All undefined — no cache line
(function () {
  var result = fmtTokenBreakdownCompact(undefined, undefined, undefined, undefined);
  assert(result === '0 total<br>0 in | 0 out', 'all undefined: no cache line');
})();

// Forbidden labels must NOT appear (\"uncached/output\" as a combined phrase, \"avg cache read\", \"/call\" suffix)
(function () {
  var result = fmtTokenBreakdownCompact(1000, 500, 2000, 0);
  // total=3.5K (input + output + cacheRead + cacheWrite)
  assert(result === '3.5K total<br>1.0K in | 500 out<br>2.0K cache read', 'expected flat format for (1000,500,2000,0)');
  assert(result.indexOf('active') === -1, 'no "active" label');
  assert(result.indexOf('uncached/output') === -1, 'no "uncached/output" label');
  assert(result.indexOf('avg cache read') === -1, 'no "avg cache read" label');
  assert(result.indexOf('/call') === -1, 'no "/call" suffix');
})();

// Cache line omitted when both cache values are zero
(function () {
  var result = fmtTokenBreakdownCompact(50000, 25000, 0, 0);
  assert(result === '75.0K total<br>50.0K in | 25.0K out', 'no cache line when both cache values zero');
  assert(result.indexOf('cache hit') === -1, 'no "cache hit" phrase in new format');
})();

// Cache write only (no cache read)
(function () {
  var result = fmtTokenBreakdownCompact(10000, 5000, 0, 3000);
  // total=18.0K
  assert(result === '18.0K total<br>10.0K in | 5.0K out<br>3.0K cache write', 'cache write only: cache write line only');
})();

// Uses fmtNum for compact number formatting
(function () {
  var result = fmtTokenBreakdownCompact(1000000, 500000, 2000000, 100000);
  // total=3.6M (input + output + cacheRead + cacheWrite)
  assert(result === '3.6M total<br>1.0M in | 500.0K out<br>2.0M cache read + 100.0K cache write', 'expected flat format for millions');
  assert(result.indexOf('1.0M') !== -1, 'uses fmtNum formatting for millions');
  assert(result.indexOf('500.0K') !== -1, 'uses fmtNum formatting for thousands');
})();

// ── Tests for fmtAgentRunTokens ──────────────────────────────────────────

console.log('\u25B6 fmtAgentRunTokens');

// Delegates to shared formatter — full cache hit with cache write
(function () {
  var result = fmtAgentRunTokens(34900, 5100, 755500, 32);
  // total=795.5K (input + output + cacheRead + cacheWrite)
  assert(result === '795.5K total<br>34.9K in | 5.1K out<br>755.5K cache read + 32 cache write', 'delegates to shared formatter: flat two-line + cache line');
})();

// Cache hit without write (cacheWrite=0)
(function () {
  var result = fmtAgentRunTokens(1000, 500, 50000, 0);
  // total=51.5K (input + output + cacheRead + cacheWrite)
  assert(result === '51.5K total<br>1.0K in | 500 out<br>50.0K cache read', 'cache hit without write: cache read line only');
})();

// Null/missing values — no cache line
(function () {
  var result = fmtAgentRunTokens(null, null, null, null);
  assert(result === '0 total<br>0 in | 0 out', 'null values: no cache line');
})();

// Zero cache reads, non-zero cache write
(function () {
  var result = fmtAgentRunTokens(10000, 5000, 0, 10);
  // total=15.0K (input + output + cacheRead + cacheWrite)
  assert(result === '15.0K total<br>10.0K in | 5.0K out<br>10 cache write', 'zero cache reads with write: cache write line only');
})();

// Forbidden labels must NOT appear as combined phrases
(function () {
  var result = fmtAgentRunTokens(100, 200, 300, 5);
  // total=605 (input + output + cacheRead + cacheWrite)
  assert(result === '605 total<br>100 in | 200 out<br>300 cache read + 5 cache write', 'expected flat format for (100,200,300,5)');
  assert(result.indexOf('uncached/output') === -1, 'no "uncached/output" label');
  assert(result.indexOf('avg cache read') === -1, 'no "avg cache read" label');
  assert(result.indexOf('/call') === -1, 'no "/call" suffix');
})();

// Large cache reads formatting
(function () {
  var result = fmtAgentRunTokens(1000000, 500000, 50000000, 1000);
  // total=51.5M (input + output + cacheRead + cacheWrite)
  assert(result === '51.5M total<br>1.0M in | 500.0K out<br>50.0M cache read + 1.0K cache write', 'large cache reads: proper fmtNum formatting');
})();

// Small values with both cache read and write
(function () {
  var result = fmtAgentRunTokens(100, 50, 150, 3);
  // total=303 (input + output + cacheRead + cacheWrite)
  assert(result === '303 total<br>100 in | 50 out<br>150 cache read + 3 cache write', 'small values: flat format displayed');
})();

// Output matches fmtTokenBreakdownCompact for same inputs
(function () {
  var direct = fmtTokenBreakdownCompact(34900, 5100, 755500, 32);
  var viaAgent = fmtAgentRunTokens(34900, 5100, 755500, 32);
  assert(direct === viaAgent, 'fmtAgentRunTokens delegates to fmtTokenBreakdownCompact: same output for (34900, 5100, 755500, 32)');
})();

// ── Client metadata cache (production code loaded from app.js) ───────────
// The REAL implementation lives in frontend/app.js: createClientCache is a
// pure factory (no DOM/fetch access) exposed on window by the vm sandbox
// loader, so the first block exercises the production 10-minute expiry
// policy directly with an injected clock.  The second block drives the
// production wiring (ensureClientName → refreshClientCache → /admin/clients)
// through a fetch-counting stub: hits never fetch, misses trigger a
// non-blocking background refresh, and a failed refresh never clears the
// last-known names.

function createClientCache(opts) {
  return window.createClientCache(opts);
}

console.log('\u25B6 client metadata cache — pure factory (10-minute TTL)');

(function () {
  var t = 1000000; // fake clock, ms
  var cache = createClientCache({ ttlMs: 600000, now: function () { return t; } });

  // Empty cache is stale → first read must fetch
  assert(cache.isExpired() === true, 'never-loaded cache is expired (stale)');

  cache.refresh([
    { id: 'c1', name: 'Alpha' },
    { id: 'c2', name: null },
    { id: 'c3', name: '' }
  ]);

  // Cache hit
  assert(cache.get('c1') === 'Alpha', 'hit: cached name returned for known id');
  assert(cache.get('c2') === 'c2', 'hit: id with no name falls back to the id');
  assert(cache.get('c3') === 'c3', 'hit: empty name falls back to the id');
  assert(cache.has('c1') === true, 'has: known id → true');
  assert(cache.isExpired() === false, 'freshly refreshed cache is not expired');

  // Cache miss
  assert(cache.get('unknown') === undefined, 'miss: unknown id returns undefined');
  assert(cache.has('unknown') === false, 'has: unknown id → false');

  // TTL expiry (10 minutes = 600000 ms)
  t += 599999;
  assert(cache.isExpired() === false, 'not expired just before the 10-minute TTL elapses');
  t += 2; // 600001 ms since refresh
  assert(cache.isExpired() === true, 'expired once the 10-minute TTL elapses');
  assert(cache.get('c1') === 'Alpha', 'stale-while-revalidate: last-known names remain readable after expiry');

  // A fresh refresh resets the TTL window and replaces the map
  t += 600000;
  cache.refresh([{ id: 'c2', name: 'Beta' }]);
  assert(cache.isExpired() === false, 'refresh resets the TTL window');
  assert(cache.get('c2') === 'Beta', 'refresh: renamed id returns the new name');
  assert(cache.get('c1') === undefined, 'refresh: ids absent from the fresh list drop out');

  // Manual invalidation (post-admin-change hook): marks stale, keeps names
  cache.invalidate();
  assert(cache.isExpired() === true, 'invalidate marks the cache stale (forces refetch)');
  assert(cache.get('c2') === 'Beta', 'invalidate keeps last-known names (stale-while-failure)');
})();

console.log('\u25B6 client metadata cache — background refresh wiring');

(function () {
  var calls = [];
  appJsSandbox.fetch = function (url) {
    calls.push(url);
    return Promise.resolve({
      ok: true,
      json: function () {
        return Promise.resolve({
          items: [
            { id: 'c1', name: 'Alpha' },
            { id: 'c2', name: null },
            { id: 'zzz', name: 'Zed' }
          ]
        });
      }
    });
  };

  // Start from a stale cache (as after first load or an admin change)
  window.invalidateClientCache();
  assert(calls.length === 0, 'invalidateClientCache() does not fetch by itself');

  // Lookup miss → non-blocking background refresh of /admin/clients
  var label = window.ensureClientName('unknown-id');
  assert(label === undefined, 'unknown id: ensureClientName returns undefined (caller falls back to the raw id)');
  assert(calls.length === 1 && calls[0] === '/admin/clients?limit=100',
    'unknown id: background refresh fetches /admin/clients once');

  // Let the fire-and-forget refresh land, then verify the cache was rebuilt
  pendingAsyncBlocks++;
  setTimeout(function () {
    var hitCalls = calls.length;
    assert(window.ensureClientName('c1') === 'Alpha',
      'background refresh: refreshed cache resolves the previously unknown id');
    assert(calls.length === hitCalls,
      'cache hit: a known id does not trigger another fetch');
    assert(window.ensureClientName('zzz') === 'Zed',
      'background refresh: previously unknown id now resolves from the cache');
    assert(window.ensureClientName('c2') === 'c2',
      'background refresh: id without a name falls back to the id');

    // Stale-while-failure: a failing background refresh keeps last-known names
    appJsSandbox.fetch = function (url) {
      calls.push(url);
      return Promise.resolve({ ok: false, status: 500 });
    };
    window.ensureClientName('unknown-2'); // miss → background refresh → fetch fails
    pendingAsyncBlocks++;
    setTimeout(function () {
      assert(window.ensureClientName('c1') === 'Alpha',
        'stale-while-failure: labels survive a failed background refresh');
      assert(calls.length === 2,
        'one fetch per miss: hits never fetch, a failed refresh never clears the map');
      pendingAsyncBlocks--;
    }, 10);
    pendingAsyncBlocks--;
  }, 10);
})();

// F2 — in-flight deduplication: multiple concurrent cache misses fire at
// most one /admin/clients fetch (single-flight fan-out protection).
// Wrapped in a setTimeout so the previous test's in-flight refresh has
// fully drained (clientsRefreshInFlight is null) before we start.
console.log('\u25B6 client metadata cache — in-flight deduplication (issue F2)');

pendingAsyncBlocks++;
setTimeout(function () {
  var calls = [];
  appJsSandbox.fetch = function (url) {
    calls.push(url);
    return new Promise(function (resolve) {
      // Simulate a slow fetch so concurrent callers pile up before it resolves
      setTimeout(function () {
        resolve({
          ok: true,
          json: function () {
            return Promise.resolve({
              items: [
                { id: 'x1', name: 'X-One' },
                { id: 'x2', name: 'X-Two' },
                { id: 'x3', name: 'X-Three' }
              ]
            });
          }
        });
      }, 20);
    });
  };

  // Start from a stale cache (previous test's refresh populated it)
  window.invalidateClientCache();
  var callsBefore = calls.length;

  // Three synchronous misses — all should share one in-flight promise
  var r1 = window.ensureClientName('x1');
  var r2 = window.ensureClientName('x2');
  var r3 = window.ensureClientName('x3');
  // All three return undefined (cache miss) synchronously
  assert(r1 === undefined, 'miss 1: returns undefined (caller falls back to raw id)');
  assert(r2 === undefined, 'miss 2: returns undefined');
  assert(r3 === undefined, 'miss 3: returns undefined');

  // Assert exactly ONE fetch was triggered (not three)
  var fetchCountBeforeResolve = calls.length - callsBefore;
  assert(fetchCountBeforeResolve === 1,
    'concurrent misses: exactly one /admin/clients fetch triggered, not ' + fetchCountBeforeResolve);

  // After the fetch resolves, the cache has all three names
  pendingAsyncBlocks++;
  setTimeout(function () {
    assert(window.ensureClientName('x1') === 'X-One',
      'post-dedupe: x1 resolves from the cache after single shared fetch');
    assert(window.ensureClientName('x2') === 'X-Two',
      'post-dedupe: x2 resolves from the cache');
    assert(window.ensureClientName('x3') === 'X-Three',
      'post-dedupe: x3 resolves from the cache');
    // Subsequent hits trigger zero additional fetches
    var callsAfterResolve = calls.length;
    window.ensureClientName('x1');
    assert(calls.length === callsAfterResolve,
      'post-dedupe: known ids trigger no additional fetches');
    pendingAsyncBlocks--;
  }, 30);
  pendingAsyncBlocks--;
}, 15);

// ── Panel freshness states (issue #357) ─────────────────────────────────
// The freshness logic lives in pure helpers in app.js (formatUpdatedAgo,
// computePanelFreshness, shouldRenderPanel, resolvePanelStatuses) that are
// exercised here through the vm-sandbox exports — the same production code
// the dashboard runs.  refreshDashboard maintains the per-panel state map
// (panelId → { status: 'ok'|'refreshing'|'stale', updatedAt }) and each
// panel render consumes it via these helpers.

console.log('\u25B6 formatUpdatedAgo');

(function () {
  var now = 1000000;
  assert(window.formatUpdatedAgo(now, now) === 'just now', 'zero elapsed \u2192 just now');
  assert(window.formatUpdatedAgo(now, now + 59000) === 'just now', '59s elapsed \u2192 just now');
  assert(window.formatUpdatedAgo(now, now + 60000) === '1m ago', '60s elapsed \u2192 1m ago');
  assert(window.formatUpdatedAgo(now, now + 120000) === '2m ago', '2m elapsed \u2192 2m ago');
  assert(window.formatUpdatedAgo(now, now + 59 * 60000) === '59m ago', '59m elapsed \u2192 59m ago');
  assert(window.formatUpdatedAgo(now, now + 60 * 60000) === '1h ago', '1h elapsed \u2192 1h ago');
  assert(window.formatUpdatedAgo(now, now + 23 * 3600000) === '23h ago', '23h elapsed \u2192 23h ago');
  assert(window.formatUpdatedAgo(now, now + 24 * 3600000) === '1d ago', '24h elapsed \u2192 1d ago');
  assert(window.formatUpdatedAgo(now, now + 3 * 24 * 3600000) === '3d ago', '3d elapsed \u2192 3d ago');
  assert(window.formatUpdatedAgo(null, now) === null, 'null timestamp \u2192 null (never updated)');
  assert(window.formatUpdatedAgo(undefined, now) === null, 'undefined timestamp \u2192 null (never updated)');
  // Future timestamps clamp to "just now" rather than rendering a negative age
  assert(window.formatUpdatedAgo(now + 5000, now) === 'just now', 'future timestamp clamps to just now');
})();

console.log('\u25B6 computePanelFreshness');

(function () {
  var now = 1000000;
  assert(window.computePanelFreshness({}, 'kpi', now) === null, 'unknown panel \u2192 null (nothing rendered)');
  assert(window.computePanelFreshness(null, 'kpi', now) === null, 'missing state map \u2192 null');

  var f = window.computePanelFreshness({ 'model-mix': { status: 'refreshing', updatedAt: 500000 } }, 'model-mix', now);
  assert(f && f.status === 'refreshing' && f.label === 'Refreshing\u2026',
    'refreshing state shows "Refreshing\u2026" while the panel update is in flight');

  var g = window.computePanelFreshness({ kpi: { status: 'stale', updatedAt: 500000 } }, 'kpi', now);
  assert(g && g.status === 'stale' && g.label === 'Showing previous data',
    'stale state shows "Showing previous data" warning');

  var h = window.computePanelFreshness({ events: { status: 'ok', updatedAt: now - 120000 } }, 'events', now);
  assert(h && h.status === 'ok' && h.label === 'Updated 2m ago', 'ok state renders "Updated 2m ago"');

  var i = window.computePanelFreshness({ 'agent-runs': { status: 'ok', updatedAt: now } }, 'agent-runs', now);
  assert(i && i.label === 'Updated just now', 'ok state renders "Updated just now" for a fresh update');

  var j = window.computePanelFreshness({ kpi: { status: 'ok', updatedAt: null } }, 'kpi', now);
  assert(j && j.label === 'Updated --', 'ok state without a timestamp falls back to "Updated --"');
})();

console.log('\u25B6 resolvePanelStatuses + shouldRenderPanel (failure retention)');

(function () {
  // No endpoint errors → every panel resolves to 'ok'
  var allOk = window.resolvePanelStatuses({});
  ['kpi-tokens', 'kpi-cost', 'kpi-sessions', 'kpi-collectors', 'kpi-source-dbs',
   'model-mix', 'events', 'collector-dist', 'collectors', 'agents', 'agent-runs', 'client-project']
    .forEach(function (panelId) {
      assert(allOk[panelId] === 'ok', 'no errors: panel "' + panelId + '" resolves to ok');
    });

  // A failed endpoint stales exactly the panels that consume it
  var modelMixFailed = window.resolvePanelStatuses({ aggByModel: 'boom' });
  assert(modelMixFailed['model-mix'] === 'stale' && modelMixFailed.agents === 'stale',
    'aggByModel failure: model-mix and agents go stale');
  assert(modelMixFailed['kpi-tokens'] === 'ok' && modelMixFailed['kpi-cost'] === 'ok' &&
         modelMixFailed['kpi-sessions'] === 'ok' && modelMixFailed['kpi-collectors'] === 'ok' &&
         modelMixFailed['kpi-source-dbs'] === 'ok' && modelMixFailed.events === 'ok' && modelMixFailed['agent-runs'] === 'ok',
    'aggByModel failure: unrelated panels (KPI cards, events, agent runs) stay ok');

  var healthFailed = window.resolvePanelStatuses({ health: 'down' });
  assert(healthFailed['kpi-collectors'] === 'stale' && healthFailed['kpi-source-dbs'] === 'stale' &&
         healthFailed.events === 'stale' &&
         healthFailed['collector-dist'] === 'stale' && healthFailed.collectors === 'stale' &&
         healthFailed.agents === 'stale',
    'health failure: every health-fed panel (kpi-collectors, kpi-source-dbs, events, collector-dist, collectors, agents) goes stale');
  assert(healthFailed['kpi-tokens'] === 'ok' && healthFailed['kpi-cost'] === 'ok' &&
         healthFailed['kpi-sessions'] === 'ok' && healthFailed['agent-runs'] === 'ok',
    'health failure: agent-runs panel and non-health KPI cards stay ok');

  var agentRunsFailed = window.resolvePanelStatuses({ agentRuns: 'boom' });
  assert(agentRunsFailed['agent-runs'] === 'stale' && agentRunsFailed.events === 'stale',
    'agentRuns failure: the agent-runs panel and the events feed (high-usage run alerts) go stale');
  assert(agentRunsFailed['kpi-tokens'] === 'ok' && agentRunsFailed['kpi-sessions'] === 'ok',
    'agentRuns failure: KPI cards stay ok (kpi-sessions reads the aggregates total row, not agent runs)');

  // Failed panel retains its previous data: the state map keeps the old
  // updatedAt, shouldRenderPanel refuses the re-render, and the label flips
  // to the "Showing previous data" warning.
  var states = { 'model-mix': { status: 'ok', updatedAt: 500000 } };
  states['model-mix'] = {
    status: window.resolvePanelStatuses({ aggByModel: 'boom' })['model-mix'],
    updatedAt: states['model-mix'].updatedAt // previous successful update time survives the failure
  };
  assert(window.shouldRenderPanel(states, 'model-mix') === false,
    'failed panel skips re-render \u2192 previous successful data is retained');
  assert(window.computePanelFreshness(states, 'model-mix', 1000000).label === 'Showing previous data',
    'failed panel swaps in the "Showing previous data" freshness label');

  // Healthy panels keep rendering and show the updated timestamp
  assert(window.shouldRenderPanel({ kpi: { status: 'ok', updatedAt: 500000 } }, 'kpi') === true,
    'ok panel still renders');
  assert(window.shouldRenderPanel({ kpi: { status: 'refreshing', updatedAt: 500000 } }, 'kpi') === true,
    'refreshing panel still renders (label shows while updating)');
  assert(window.shouldRenderPanel({}, 'kpi') === true,
    'panel with no recorded state still renders (initial load)');
})();

// N1 — never-rendered stale panel: no previous data to retain → render proceeds
console.log('\u25B6 shouldRenderPanel + computePanelFreshness (stale + never-updated, issue N1)');

(function () {
  // Stale panel that has NEVER rendered (updatedAt null): render should
  // proceed so the empty/error state is shown instead of "Loading..."
  assert(window.shouldRenderPanel({ p: { status: 'stale', updatedAt: null } }, 'p') === true,
    'stale + null updatedAt: render proceeds (no previous data to retain)');
  // Stale panel WITH previous data: render is suppressed to keep last-known values
  assert(window.shouldRenderPanel({ p: { status: 'stale', updatedAt: 1000 } }, 'p') === false,
    'stale + non-null updatedAt: render skipped (retains previous data)');

  var now = 2000;
  // Stale + never-updated → no freshness label (no "Showing previous data" on placeholders)
  assert(window.computePanelFreshness({ p: { status: 'stale', updatedAt: null } }, 'p', now) === null,
    'stale + null updatedAt: computePanelFreshness returns null (no label)');
  // Stale + has previous data → "Showing previous data" label
  var fresh = window.computePanelFreshness({ p: { status: 'stale', updatedAt: 1000 } }, 'p', now);
  assert(fresh !== null && fresh.status === 'stale' && fresh.label === 'Showing previous data',
    'stale + non-null updatedAt: computePanelFreshness returns stale descriptor');
})();

console.log('\u25B6 header last-refreshed clock (issue #357)');

(function () {
  // Exposed module-level timestamp: null until the first refresh completes
  assert(window.getLastRefreshedAt() === null, 'lastRefreshedAt starts null (no refresh cycle completed yet)');

  var d = new Date(2026, 0, 2, 3, 4, 5);
  assert(window.formatClockTime(d) === '03:04:05', 'formatClockTime renders HH:MM:SS (03:04:05)');
  var e = new Date(2026, 6, 6, 23, 59, 59);
  assert(window.formatClockTime(e) === '23:59:59', 'formatClockTime renders HH:MM:SS (23:59:59)');
})();

// ── KPI subtitle split (issue #358) ─────────────────────────────────────
// Historical KPIs (Active Tokens, Est. Cost, Sessions) keep the selected
// date range as their subtitle — they are date-range aggregates.  Current-
// health KPIs (Healthy Collectors, Source Databases) are live snapshots:
// they show "As of HH:MM:SS" from the last completed refresh (issue #357's
// lastRefreshedAt) or "Current" before any refresh completes.  Exercised
// through the vm-sandbox export of the production kpiSubtitle helper.

console.log('\u25B6 kpiSubtitle (historical vs current, issue #358)');

(function () {
  var rangeLabel = 'Jul 1\u201327, 2026';

  // Historical KPIs keep the date-range subtitle — with and without a
  // completed refresh (their aggregates are range-scoped either way).
  ['kpi-tokens', 'kpi-cost', 'kpi-sessions'].forEach(function (kpiId) {
    assert(window.kpiSubtitle(kpiId, rangeLabel, null) === rangeLabel,
      kpiId + ': historical KPI keeps the date-range subtitle (no refresh yet)');
    assert(window.kpiSubtitle(kpiId, rangeLabel, new Date(2026, 6, 6, 23, 59, 59)) === rangeLabel,
      kpiId + ': historical KPI keeps the date-range subtitle after a refresh');
  });

  // Current-health KPIs show the As-of timestamp once a refresh completed
  var t = new Date(2026, 6, 6, 23, 59, 59);
  assert(window.kpiSubtitle('kpi-collectors', rangeLabel, t) === 'As of 23:59:59',
    'kpi-collectors: current-health KPI shows "As of 23:59:59"');
  assert(window.kpiSubtitle('kpi-source-dbs', rangeLabel, t) === 'As of 23:59:59',
    'kpi-source-dbs: current-health KPI shows "As of 23:59:59"');

  // The As-of timestamp reuses the header clock formatting (formatClockTime)
  var m = new Date(2026, 0, 2, 3, 4, 5);
  assert(window.kpiSubtitle('kpi-collectors', rangeLabel, m) === 'As of ' + window.formatClockTime(m),
    'kpi-collectors: As-of timestamp matches formatClockTime (shared with the header clock)');

  // Label shape is exactly "As of HH:MM:SS"
  assert(/^As of \d{2}:\d{2}:\d{2}$/.test(window.kpiSubtitle('kpi-source-dbs', rangeLabel, t)),
    'kpi-source-dbs: current-health label matches the "As of HH:MM:SS" format');

  // Before the first refresh completes (no lastRefreshedAt) → "Current"
  assert(window.kpiSubtitle('kpi-collectors', rangeLabel, null) === 'Current',
    'kpi-collectors: no refresh yet \u2192 "Current" fallback');
  assert(window.kpiSubtitle('kpi-source-dbs', rangeLabel, undefined) === 'Current',
    'kpi-source-dbs: no refresh yet \u2192 "Current" fallback');
  assert(window.kpiSubtitle('kpi-collectors', rangeLabel, 'not-a-date') === 'Current',
    'kpi-collectors: invalid refresh value \u2192 "Current" fallback');

  // The UI must not imply collector health is aggregated historically:
  // the current-health subtitle never carries the date-range label.
  assert(window.kpiSubtitle('kpi-collectors', rangeLabel, t).indexOf(rangeLabel) === -1,
    'kpi-collectors: subtitle never carries the date-range label');
  assert(window.kpiSubtitle('kpi-source-dbs', rangeLabel, t).indexOf(rangeLabel) === -1,
    'kpi-source-dbs: subtitle never carries the date-range label');
})();

// ── Agent Runs "Last Updated" timestamp (issue #4) ──────────────────────
// Year-inclusive absolute local datetime for the Agent Runs table, e.g.
// "Aug 11, 2026, 9:41 AM".  Pure — no wall-clock reads: the injected
// clock (now) makes the output fully deterministic, so these tests never
// depend on Date.now()/new Date() (precedent: createClientCache now-fn,
// computeDateRange now param).  Exercised through the vm-sandbox export
// of the production helper.

console.log('\u25B6 formatAgentRunTimestamp (issue #4)');

(function () {
  // Deterministic injected clock: Aug 11, 2026, 23:59:59 local (end of the
  // day — every valid same-day timestamp below is in the past, so the
  // future-clamp only affects the dedicated skew test case)
  var refNow = new Date(2026, 7, 11, 23, 59, 59);

  // Valid ISO timestamp → year-inclusive absolute local datetime
  assert(window.formatAgentRunTimestamp('2026-08-11T09:41:00', refNow) === 'Aug 11, 2026, 9:41 AM',
    'valid ISO timestamp \u2192 "Aug 11, 2026, 9:41 AM" (year-inclusive absolute local datetime)');
  assert(window.formatAgentRunTimestamp('2026-08-11T19:05:30', refNow) === 'Aug 11, 2026, 7:05 PM',
    'evening timestamp \u2192 "Aug 11, 2026, 7:05 PM" (12-hour with AM/PM, no leading-zero hour)');

  // Missing/unparseable input → "--" fallback (fmtDT/fmtRelative style)
  assert(window.formatAgentRunTimestamp(null, refNow) === '--', 'null \u2192 --');
  assert(window.formatAgentRunTimestamp(undefined, refNow) === '--', 'undefined \u2192 --');
  assert(window.formatAgentRunTimestamp('', refNow) === '--', 'empty string \u2192 --');
  assert(window.formatAgentRunTimestamp('not-a-date', refNow) === '--', 'unparseable string \u2192 --');

  // Injected-clock determinism: fixed ISO + fixed injected clock → identical
  // output, regardless of the wall clock (no Date.now() in tests)
  var a = window.formatAgentRunTimestamp('2026-08-11T09:41:00', refNow);
  var b = window.formatAgentRunTimestamp('2026-08-11T09:41:00', new Date(refNow.getTime()));
  assert(a === b, 'deterministic: same ISO + same injected clock \u2192 same output');

  // The clock may be injected as a now-fn too (createClientCache precedent)
  var t = refNow.getTime();
  assert(window.formatAgentRunTimestamp('2026-08-11T09:41:00', function () { return t; }) === 'Aug 11, 2026, 9:41 AM',
    'now may be injected as a function (createClientCache-style now-fn)');

  // Future timestamps (backend clock skew) clamp to the injected now —
  // the table never shows a "Last Updated" time that hasn't happened yet
  // (mirrors formatUpdatedAgo's future-clamp behavior)
  assert(window.formatAgentRunTimestamp('2030-01-01T00:00:00', refNow) === 'Aug 11, 2026, 11:59 PM',
    'future timestamp (clock skew) clamps to the injected now');
})();

// N2 — per-card KPI staleness: each KPI card resolves independently, so a
// single failing endpoint never freezes the other cards.  The merged
// Sessions + Agent Runs view (issue #402) backs the Sessions KPI with the
// aggregates total row (the /sessions endpoint is no longer fetched), so
// only an aggTotal failure stales kpi-sessions.
console.log('\u25B6 resolvePanelStatuses — KPI per-card staleness (issue N2)');

(function () {
  // Only aggTotal fails → kpi-tokens, kpi-cost, and kpi-sessions go stale
  var aggFail = window.resolvePanelStatuses({ aggTotal: 'boom' });
  assert(aggFail['kpi-tokens'] === 'stale', 'aggTotal fail: kpi-tokens goes stale');
  assert(aggFail['kpi-cost'] === 'stale', 'aggTotal fail: kpi-cost goes stale');
  assert(aggFail['kpi-sessions'] === 'stale', 'aggTotal fail: kpi-sessions goes stale (KPI reads the aggregates total row)');
  assert(aggFail['kpi-collectors'] === 'ok', 'aggTotal fail: kpi-collectors stays ok (health is fine)');
  assert(aggFail['kpi-source-dbs'] === 'ok', 'aggTotal fail: kpi-source-dbs stays ok (health is fine)');

  // Only health fails → only kpi-collectors and kpi-source-dbs go stale
  var healthFail = window.resolvePanelStatuses({ health: 'down' });
  assert(healthFail['kpi-tokens'] === 'ok', 'health fail: kpi-tokens stays ok (aggTotal is fine)');
  assert(healthFail['kpi-cost'] === 'ok', 'health fail: kpi-cost stays ok (aggTotal is fine)');
  assert(healthFail['kpi-sessions'] === 'ok', 'health fail: kpi-sessions stays ok (aggTotal is fine)');
  assert(healthFail['kpi-collectors'] === 'stale', 'health fail: kpi-collectors goes stale');
  assert(healthFail['kpi-source-dbs'] === 'stale', 'health fail: kpi-source-dbs goes stale');

  // Only agentRuns fails → no KPI card goes stale (the merged table keeps
  // its previous rows and shows "Showing previous data"; kpi-sessions reads
  // the aggregates total row, not the agent-runs channel)
  var runFail = window.resolvePanelStatuses({ agentRuns: 'boom' });
  assert(runFail['kpi-tokens'] === 'ok', 'agentRuns fail: kpi-tokens stays ok (aggTotal is fine)');
  assert(runFail['kpi-cost'] === 'ok', 'agentRuns fail: kpi-cost stays ok (aggTotal is fine)');
  assert(runFail['kpi-sessions'] === 'ok', 'agentRuns fail: kpi-sessions stays ok (aggTotal is fine)');
  assert(runFail['kpi-collectors'] === 'ok', 'agentRuns fail: kpi-collectors stays ok (health is fine)');
  assert(runFail['kpi-source-dbs'] === 'ok', 'agentRuns fail: kpi-source-dbs stays ok (health is fine)');

  // No errors → all KPI cards ok
  var allOk = window.resolvePanelStatuses({});
  ['kpi-tokens', 'kpi-cost', 'kpi-sessions', 'kpi-collectors', 'kpi-source-dbs']
    .forEach(function (kpiId) {
      assert(allOk[kpiId] === 'ok', 'no errors: KPI card "' + kpiId + '" resolves to ok');
    });
})();

// ── Agent Runs date filters — active state + Clear control (issue #7) ───
// The state logic lives in the production app.js helpers
// (computeArDateFilterState / syncArDateFilterUI / clearArDateFilters /
// readFiltersFromUI) exposed through the vm-sandbox window bridge, and the
// wiring is exercised through the production setupAgentRunEventHandlers()
// against the fake filter-bar elements registered before app.js loaded.

console.log('\u25B6 agent-runs date filters — active/disabled state (issue #7)');

// Pure state helper: per-input active flags + Clear disabled state, driven
// by the raw From/To input values ('' when empty).
(function () {
  var bothEmpty = window.computeArDateFilterState('', '');
  assert(bothEmpty.clearDisabled === true && bothEmpty.fromActive === false && bothEmpty.toActive === false,
    'empty From/To: Clear disabled, no active inputs');

  var fromOnly = window.computeArDateFilterState('2026-07-01', '');
  assert(fromOnly.fromActive === true && fromOnly.toActive === false && fromOnly.clearDisabled === false,
    'From only: From active, To inactive, Clear enabled');

  var toOnly = window.computeArDateFilterState('', '2026-07-31');
  assert(toOnly.toActive === true && toOnly.fromActive === false && toOnly.clearDisabled === false,
    'To only: To active, From inactive, Clear enabled');

  var both = window.computeArDateFilterState('2026-07-01', '2026-07-31');
  assert(both.fromActive === true && both.toActive === true && both.clearDisabled === false,
    'both populated: both active, Clear enabled');

  var nullish = window.computeArDateFilterState(null, undefined);
  assert(nullish.clearDisabled === true && nullish.fromActive === false && nullish.toActive === false,
    'null/undefined values treated as empty: Clear disabled, no active inputs');
})();

// Regression lock: readFiltersFromUI — partial ranges and the UTC-boundary
// conversion (from_date + T00:00:00Z, to_date + T23:59:59Z) are unchanged.
console.log('\u25B6 agent-runs date filters — readFiltersFromUI unchanged (issue #7)');

(function () {
  // From only → partial range, UTC midnight boundary
  arFilterFromEl.value = '2026-07-01';
  arFilterToEl.value = '';
  var f = window.readFiltersFromUI();
  assert(f.from_date === '2026-07-01T00:00:00Z', 'From only: from_date uses the UTC midnight boundary (T00:00:00Z)');
  assert(f.to_date === undefined, 'From only: no to_date — partial range preserved');

  // To only → partial range, UTC end-of-day boundary
  arFilterFromEl.value = '';
  arFilterToEl.value = '2026-07-31';
  var t = window.readFiltersFromUI();
  assert(t.to_date === '2026-07-31T23:59:59Z', 'To only: to_date uses the UTC end-of-day boundary (T23:59:59Z)');
  assert(t.from_date === undefined, 'To only: no from_date — partial range preserved');

  // Both → both boundaries applied
  arFilterFromEl.value = '2026-07-01';
  arFilterToEl.value = '2026-07-31';
  var b = window.readFiltersFromUI();
  assert(b.from_date === '2026-07-01T00:00:00Z' && b.to_date === '2026-07-31T23:59:59Z',
    'both set: UTC midnight + end-of-day boundaries applied');

  // Both empty → no date filters at all (unfiltered request)
  arFilterFromEl.value = '';
  arFilterToEl.value = '';
  var e = window.readFiltersFromUI();
  assert(e.from_date === undefined && e.to_date === undefined, 'both empty: no date filters (unfiltered)');
})();

// Wiring: the Clear control and the active/disabled state drive the real
// production elements through setupAgentRunEventHandlers(), and clicking
// Clear empties both date inputs and re-applies the existing filter path
// (readFiltersFromUI -> applyFilters -> buildAgentRunsUrl -> apiFetch).
console.log('\u25B6 agent-runs date filters — Clear control wiring (issue #7)');

(function () {
  // Reset fakes to a pristine state, then wire like startAutoRefresh does.
  arFilterFromEl.value = '';
  arFilterToEl.value = '';
  arFilterClearEl.disabled = false;
  arFilterFromEl.classList._classes = {};
  arFilterToEl.classList._classes = {};
  arTbodyEl.innerHTML = '';
  window.setupAgentRunEventHandlers();

  // Initial sync: both inputs empty → Clear disabled, no active classes
  assert(arFilterClearEl.disabled === true, 'initial state: Clear disabled with both inputs empty');
  assert(arFilterFromEl.classList.contains('active') === false &&
         arFilterToEl.classList.contains('active') === false,
    'initial state: no active styling on empty inputs');

  // Populate From → active styling appears on From, Clear becomes enabled
  arFilterFromEl.value = '2026-07-01';
  arFilterFromEl._handlers.input();
  assert(arFilterFromEl.classList.contains('active') === true, 'populated From: active styling applied');
  assert(arFilterToEl.classList.contains('active') === false, 'empty To: no active styling');
  assert(arFilterClearEl.disabled === false, 'populated From: Clear enabled');

  // Populate To → both inputs active
  arFilterToEl.value = '2026-07-31';
  arFilterToEl._handlers.input();
  assert(arFilterToEl.classList.contains('active') === true, 'populated To: active styling applied');
  assert(arFilterFromEl.classList.contains('active') === true, 'From still populated: stays active');
  assert(arFilterClearEl.disabled === false, 'both populated: Clear enabled');

  // Empty To again → its active styling drops, Clear stays enabled (From set)
  arFilterToEl.value = '';
  arFilterToEl._handlers.input();
  assert(arFilterToEl.classList.contains('active') === false, 'cleared To: active styling removed');
  assert(arFilterFromEl.classList.contains('active') === true, 'From still populated: active styling kept');
  assert(arFilterClearEl.disabled === false, 'From still populated: Clear enabled');

  // Click Clear → both inputs emptied, active styling removed, Clear
  // disabled, and the existing filter path re-applied with the unfiltered
  // URL (no from_date/to_date params) — the agent-runs endpoint only.
  var calls = [];
  appJsSandbox.fetch = function (url) {
    calls.push(url);
    return Promise.resolve({
      ok: true,
      json: function () { return Promise.resolve({ items: [] }); }
    });
  };

  arFilterClearEl._handlers.click();
  assert(arFilterFromEl.value === '' && arFilterToEl.value === '',
    'Clear click: both date inputs emptied');
  assert(arFilterFromEl.classList.contains('active') === false &&
         arFilterToEl.classList.contains('active') === false,
    'Clear click: active styling removed from both inputs');
  assert(arFilterClearEl.disabled === true, 'Clear click: Clear disabled again');
  assert(calls.length === 1, 'Clear click: exactly one agent-runs fetch triggered');
  assert(calls[0].indexOf('/api/v1/usage/agent-runs?') === 0,
    'Clear click: fetches the agent-runs endpoint (existing filter path, no new mechanism)');
  assert(calls[0].indexOf('from_date=') === -1 && calls[0].indexOf('to_date=') === -1,
    'Clear click: unfiltered URL — no from_date/to_date params (restores the unfiltered list)');
  assert(calls[0].indexOf('start_date=') === -1 && calls[0].indexOf('end_date=') === -1,
    'Clear click: URL carries no Overview global date-range params (filters are independent)');

  // The re-applied fetch renders the unfiltered (empty-state) table
  pendingAsyncBlocks++;
  setTimeout(function () {
    assert(arTbodyEl.innerHTML.indexOf('No agent runs') !== -1,
      'Clear click: unfiltered render completed (empty-state row written to the table)');
    pendingAsyncBlocks--;
  }, 10);
})();

// ── Agent Runs "Last Updated" cell — absolute + muted relative (issue #5) ─
// Structural row-markup coverage for the Last Updated cell: the production
// row template (renderAgentRunsTable) renders the year-inclusive absolute
// local timestamp (issue #4 formatter) as the primary value with the
// relative label as muted secondary text, and a bare '--' when the
// timestamp is missing.  Driven through the real render path
// (clearArDateFilters -> applyFilters -> apiFetch -> renderAgentRunsTable)
// with a stubbed fetch, so the assertions run against the actual app.js
// row markup.  The expected absolute string is derived FROM the
// window.formatAgentRunTimestamp seam itself (not hard-coded), proving the
// cell output comes from the production formatter — no copy-pasted
// duplicate.

console.log('\u25B6 Agent Runs Last Updated cell — absolute primary + muted relative secondary (issue #5)');

(function () {
  // Deterministic fixture: a 2025 timestamp is safely in the past for any
  // wall clock, so the issue #4 formatter's future-clamp never fires and
  // the absolute output is stable (ISO without offset parses as local time
  // and re-formats in the same local timezone — timezone-independent).
  var refNow = new Date(2025, 6, 15, 12, 0, 0);
  var expectedAbs = window.formatAgentRunTimestamp('2025-06-15T10:30:00', refNow);
  assert(expectedAbs === 'Jun 15, 2025, 10:30 AM',
    'seam sanity: window.formatAgentRunTimestamp derives "Jun 15, 2025, 10:30 AM" (issue #4 formatter)');

  // Static: the 11-column header is unchanged (no new column for the
  // relative label — it lives inside the existing Last Updated cell).
  var headerHtml = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  var arThead = headerHtml.slice(
    headerHtml.indexOf('<table id="agent-runs-table">'),
    headerHtml.indexOf('</thead>', headerHtml.indexOf('<table id="agent-runs-table">'))
  );
  // <th> followed by '>' or whitespace — <thead> does not count as a column
  var thCount = (arThead.match(/<th[\s>]/g) || []).length;
  assert(thCount === 11, 'index.html: agent-runs header keeps exactly 11 columns (' + thCount + ' found)');
  assert(arThead.indexOf('<th>Last Updated</th>') !== -1,
    'index.html: "Last Updated" header cell present and not ar-col-low (visible at all widths)');

  // Static: the muted-secondary rule for the relative label exists in the
  // real stylesheet (no inline style — class-based, per repo convention).
  var relCss = fs.readFileSync(path.join(__dirname, '..', 'style.css'), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '');
  var relRule = relCss.match(/\.ar-rel-time\s*\{[^}]*\}/);
  assert(relRule !== null && relRule[0].indexOf('var(--text-muted)') !== -1,
    'style.css: .ar-rel-time rule mutes the relative label (color: var(--text-muted))');

  // Fixtures: one row with a timestamp, one with a missing timestamp.
  var rows = [
    { id: 'run-abs-1', title: 'Alpha run', currentStatus: 'completed', model: 'gpt-4o', agent: 'alpha',
      todo_completed: 2, todo_total: 3, code_changes_total: 4, total_estimated_cost_usd: 0.12,
      total_input_tokens: 100, total_output_tokens: 50, total_cache_read_tokens: 10,
      total_cache_write_tokens: 5, child_run_count: 0, last_updated_at: '2025-06-15T10:30:00' },
    { id: 'run-missing-2', title: 'Beta run', currentStatus: 'running', model: 'claude-sonnet', agent: 'beta',
      todo_completed: 0, todo_total: 0, code_changes_total: 0, total_estimated_cost_usd: 0,
      total_input_tokens: 0, total_output_tokens: 0, total_cache_read_tokens: 0,
      total_cache_write_tokens: 0, child_run_count: 2, last_updated_at: null }
  ];

  // The render flow is deferred past the issue #7 Clear-wiring block's
  // async assertions (both blocks share the arTbodyEl fake): issue #7
  // asserts at ~10ms, so this block's fetch-driven render starts at ~20ms
  // and never clobbers the earlier block's expected empty-state markup.
  pendingAsyncBlocks++;
  setTimeout(function () {
    // Reset the fakes and wire the Clear handler like the app bootstrap does.
    arFilterFromEl.value = '';
    arFilterToEl.value = '';
    arFilterClearEl.disabled = false;
    arTbodyEl.innerHTML = '';
    window.setupAgentRunEventHandlers();

    // Render the two fixture rows through the real filter path.
    appJsSandbox.fetch = function () {
      return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ items: rows }); } });
    };
    arFilterClearEl._handlers.click();

    setTimeout(function () {
      var html = arTbodyEl.innerHTML;
      assert(html.indexOf('data-id="run-abs-1"') !== -1 && html.indexOf('data-id="run-missing-2"') !== -1,
        'render: both fixture rows written to the tbody');

      // Timestamp row: absolute primary + muted relative secondary in the SAME
      // Last Updated cell, and nothing else.
      var rowAbs = html.slice(html.indexOf('data-id="run-abs-1"'),
        html.indexOf('</tr>', html.indexOf('data-id="run-abs-1"')) + 5);
      var cellAbs = rowAbs.slice(rowAbs.indexOf('<td data-label="Last Updated">'),
        rowAbs.indexOf('</td>', rowAbs.indexOf('<td data-label="Last Updated">')) + 5);
      assert(new RegExp('^<td data-label="Last Updated">' + expectedAbs +
        ' <span class="ar-rel-time">\\d+d ago<\\/span><\\/td>$').test(cellAbs),
        'row markup: absolute timestamp primary + relative label ("Nd ago") as muted secondary span in one cell');
      assert(cellAbs.indexOf('--') === -1,
        'row markup: no -- fallback when the timestamp is present');

      // Missing-timestamp row: bare '--' only — no secondary span, row intact.
      var rowMiss = html.slice(html.indexOf('data-id="run-missing-2"'),
        html.indexOf('</tr>', html.indexOf('data-id="run-missing-2"')) + 5);
      var cellMiss = rowMiss.slice(rowMiss.indexOf('<td data-label="Last Updated">'),
        rowMiss.indexOf('</td>', rowMiss.indexOf('<td data-label="Last Updated">')) + 5);
      assert(/^<td data-label="Last Updated">--<\/td>$/.test(cellMiss),
        'row markup: missing timestamp renders bare -- without breaking the row');

      // Empty state: the colspan="11" invariant is unchanged after the cell
      // rework (row markup still spans the full 11-column table).
      appJsSandbox.fetch = function () {
        return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ items: [] }); } });
      };
      arFilterClearEl._handlers.click();
      pendingAsyncBlocks++;
      setTimeout(function () {
        assert(arTbodyEl.innerHTML.indexOf('colspan="11"') !== -1,
          'empty state: colspan="11" preserved');
        assert(arTbodyEl.innerHTML.indexOf('No agent runs') !== -1,
          'empty state: "No agent runs" message intact');
        pendingAsyncBlocks--;
      }, 10);

      pendingAsyncBlocks--;
    }, 10);
    pendingAsyncBlocks--;
  }, 20);
})();

// ── Static markup smoke check (frontend/index.html) ─────────────────────
// The repo has no browser test harness, so the "browser-level or equivalent
// smoke check" acceptance criterion maps to static assertions on the real
// index.html markup: the three top-nav tabs exist and are keyboard-focusable
// (tabindex="0"), each has a matching tab-content panel, the merged
// Sessions + Agent Runs table (issue #402) carries the responsive hooks
// (#agent-runs-table + ar-col-low header cells) that the breakpoint CSS
// targets, and the removed Sessions tab/table is gone.

console.log('\u25B6 index.html markup (smoke check)');

(function () {
  var indexPath = path.join(__dirname, '..', 'index.html');
  var html = fs.readFileSync(indexPath, 'utf8');

  // Three tabs: top-nav item + matching content panel (the Sessions tab was
  // merged into Agent Runs — issue #402)
  var tabs = ['overview', 'agent-runs', 'clients-projects'];
  tabs.forEach(function (tab) {
    assert(html.indexOf('data-tab="' + tab + '"') !== -1, 'top nav: item for tab "' + tab + '" exists');
    assert(html.indexOf('id="tab-' + tab + '"') !== -1, 'top nav: tab-content panel #tab-' + tab + ' exists');
  });

  // Keyboard reachability: every top-nav item is focusable (tabindex="0"),
  // so tabbing enters Overview → Agent Runs → Clients / Projects.
  var navItemCount = (html.match(/class="top-nav-item/g) || []).length;
  assert(navItemCount === 3, 'top nav: exactly three top-nav-item elements (' + navItemCount + ' found)');
  tabs.forEach(function (tab) {
    assert(html.indexOf('data-tab="' + tab + '" tabindex="0"') !== -1,
      'top nav: tab "' + tab + '" is keyboard-focusable (tabindex="0")');
  });

  // Responsive hooks: the merged table is addressable by id and its four
  // low-priority header cells carry ar-col-low (hidden at 761–1024px).
  assert(html.indexOf('<table id="agent-runs-table">') !== -1, 'merged table carries id="agent-runs-table"');
  ['Agent', 'Todo', 'Files', 'Children'].forEach(function (label) {
    assert(html.indexOf('<th class="ar-col-low">' + label + '</th>') !== -1,
      'merged header: "' + label + '" marked ar-col-low');
  });
  ['Title', 'Status', 'Model', 'Project / Worktree', 'Cost', 'Tokens', 'Last Updated'].forEach(function (label) {
    assert(html.indexOf('<th class="ar-col-low">' + label + '</th>') === -1,
      'merged header: retained column "' + label + '" is not marked ar-col-low');
  });

  // The separate Sessions panel/tab is gone (issue #402): no sessions table,
  // no sessions tab-content, no sessions nav item.
  assert(html.indexOf('id="sessions-table"') === -1, 'index.html: #sessions-table removed');
  assert(html.indexOf('id="sessions-tbody"') === -1, 'index.html: #sessions-tbody removed');
  assert(html.indexOf('data-tab="sessions"') === -1, 'index.html: Sessions tab removed');
  assert(html.indexOf('id="tab-sessions"') === -1, 'index.html: #tab-sessions panel removed');

  // Events panel title (issue #355): the "Live Events" panel was renamed to
  // "Operational Events" — a label-only change. The event badge and the
  // empty-state text must remain untouched.
  assert(html.indexOf('Operational Events') !== -1, 'events panel: title reads "Operational Events"');
  assert(html.indexOf('Live Events') === -1, 'events panel: "Live Events" no longer appears in the markup');
  assert(html.indexOf('id="event-badge"') !== -1, 'events panel: #event-badge element still present');
  assert(html.indexOf('Waiting for events&hellip;') !== -1, 'events panel: empty-state text unchanged');

  // Token vocabulary (issue #354): the KPI card label and the Client / Project
  // Usage Breakdown tokens column header read "Active Tokens"; the legacy
  // "Total Tokens" label is gone from the markup entirely.
  assert(html.indexOf('<span class="kpi-label">Active Tokens</span>') !== -1,
    'KPI card: label reads "Active Tokens"');
  assert(html.indexOf('<th>Active Tokens</th>') !== -1,
    'Client / Project Usage Breakdown: tokens column header reads "Active Tokens"');
  assert(html.indexOf('Total Tokens') === -1, 'index.html: no "Total Tokens" label remains');

  // KPI subtitle spans (issue #358): all five KPI cards carry a .kpi-sub
  // detail span (historical cards keep the date range; current-health
  // cards show the As-of timestamp — renderKPIs targets these elements).
  ['kpi-tokens', 'kpi-cost', 'kpi-sessions', 'kpi-collectors', 'kpi-source-dbs']
    .forEach(function (kpiId) {
      assert(html.indexOf('id="' + kpiId + '-detail"') !== -1,
        'KPI card: subtitle span #' + kpiId + '-detail exists in the markup');
    });

  // Freshness indicators (issue #357): the header carries a labeled
  // "Last refreshed" clock, and each instrumented panel carries a
  // .panel-freshness span in its title row (KPI row, Model Mix,
  // Operational Events, plus the remaining data panels — the Sessions
  // panel freshness span was removed with the merged table, issue #402).
  assert(html.indexOf('id="last-refreshed"') !== -1 && html.indexOf('Last refreshed') !== -1,
    'header: #last-refreshed element with "Last refreshed" label exists');
  ['kpi-tokens', 'kpi-cost', 'kpi-sessions', 'kpi-collectors', 'kpi-source-dbs',
   'model-mix', 'collectors', 'events', 'collector-dist', 'agents', 'agent-runs', 'client-project']
    .forEach(function (panelId) {
      assert(html.indexOf('id="freshness-' + panelId + '"') !== -1,
        'panel: freshness span #freshness-' + panelId + ' exists in the markup');
    });
  assert(html.indexOf('class="panel-freshness"') !== -1, 'panel: freshness spans use class="panel-freshness"');
})();

// ── Static CSS verification (frontend/style.css) ────────────────────────
// No browser is available in the repository test environment, so the
// responsive and reduced-motion acceptance criteria are verified statically
// against the real stylesheet: the three viewport bands (>1024px full table,
// 761–1024px condensed, ≤760px stacked), the reduced-motion animation kills,
// focus-visible rules, and the Balanced Quiet Rows regression guards (no
// green active-row cast, no cyan row stripe in live rules).  The hooks now
// target the merged Sessions + Agent Runs table (#agent-runs-table, .ar-row,
// .ar-col-low — issue #402).

console.log('\u25B6 style.css responsive + reduced-motion (static verification)');

(function () {
  var cssPath = path.join(__dirname, '..', 'style.css');
  var css = fs.readFileSync(cssPath, 'utf8');
  var live = css.replace(/\/\*[\s\S]*?\*\//g, ''); // comment-stripped: guards assert on real rules only

  // The three viewport bands
  assert(css.indexOf('@media (max-width: 1024px)') !== -1, 'style.css: tablet breakpoint @media (max-width: 1024px) exists');
  assert(css.indexOf('@media (max-width: 760px)') !== -1, 'style.css: phone breakpoint @media (max-width: 760px) exists');

  // 761–1024px tablet band: low-priority merged-table columns hidden via ar-col-low
  var tabletBlock = css.slice(css.indexOf('@media (max-width: 1024px)'), css.indexOf('@media (max-width: 760px)'));
  assert(tabletBlock.indexOf('#agent-runs-table .ar-col-low') !== -1 && tabletBlock.indexOf('display: none') !== -1,
    'tablet block hides #agent-runs-table .ar-col-low (Agent, Todo, Files, Children)');

  // ≤760px phone band: stacked agent run rows — header removed, rows become
  // block cards, cells render label/value lines via attr(data-label)
  // (lastIndexOf: the file-header comment also mentions the reduced-motion
  // media query, so anchor on the real block after the 760px one)
  var rmStart = css.lastIndexOf('@media (prefers-reduced-motion: reduce)');
  var phoneBlock = css.slice(css.indexOf('@media (max-width: 760px)'), rmStart);
  assert(phoneBlock.indexOf('#agent-runs-table thead') !== -1 && phoneBlock.indexOf('display: none') !== -1,
    'phone block removes the merged table header (stacked rows carry their own labels)');
  assert(phoneBlock.indexOf('tr.ar-row') !== -1 && phoneBlock.indexOf('display: block') !== -1,
    'phone block turns agent run rows into stacked block cards');
  assert(phoneBlock.indexOf('attr(data-label)') !== -1,
    'phone block labels stacked cells via attr(data-label)');

  // Reduced motion: aurora drift, live pulse, badge pulse disabled; status
  // badges (static border/text cues) untouched; focus ring retained
  var rmBlock = css.slice(rmStart);
  assert(rmBlock.indexOf('.aurora-bg') !== -1 && rmBlock.indexOf('animation: none') !== -1,
    'reduced motion: aurora drift disabled (.aurora-bg animation: none)');
  assert(rmBlock.indexOf('.live-indicator::before') !== -1 && rmBlock.indexOf('animation: none') !== -1,
    'reduced motion: live pulse disabled (.live-indicator::before animation: none)');
  assert(rmBlock.indexOf('.badge-running') !== -1 && rmBlock.indexOf('animation: none') !== -1,
    'reduced motion: badge pulse disabled (.badge-running animation: none)');
  assert(rmBlock.indexOf('.badge-active') === -1,
    'reduced motion: no session active-badge styling remains (removed with the sessions table)');
  assert(rmBlock.indexOf('.ar-row:focus-visible td') !== -1,
    'reduced motion: keyboard focus ring retained (focus is not motion)');

  // Focus visibility at base
  assert(css.indexOf('.ar-row:focus-visible td') !== -1, 'style.css: agent run row focus ring rule exists');
  assert(css.indexOf('.top-nav-item:focus-visible') !== -1, 'style.css: top-nav-item focus ring rule exists');

  // Balanced Quiet Rows regression guards — no green active-row cast and no
  // cyan row stripe in live rules (status visuals live in the badge only).
  // The active/idle [data-active] row hooks were removed with the sessions
  // table (issue #402); the merged table renders status via currentStatus.
  assert(live.indexOf('[data-active') === -1,
    'no status-driven [data-active] row selector in live CSS (no green active-row cast)');
  assert(!/\.ar-row[^{]*\{[^}]*border-left[^}]*\}/.test(live),
    'no .ar-row rule paints a left rail/stripe in live CSS');
  assert(live.indexOf('.session-row') === -1,
    'no .session-row rules remain in live CSS (sessions table removed)');

  // Freshness styles (issue #357): subtle muted labels in panel titles —
  // inline in the title's existing flex row (no layout shifts) — plus the
  // "Last refreshed" header clock and the stale/refreshing state variants.
  assert(live.indexOf('.panel-freshness') !== -1, 'style.css: .panel-freshness base rule exists');
  assert(live.indexOf('.freshness-refreshing') !== -1, 'style.css: .freshness-refreshing state rule exists');
  assert(live.indexOf('.freshness-stale') !== -1, 'style.css: .freshness-stale state rule exists');
  assert(live.indexOf('.last-refreshed') !== -1, 'style.css: .last-refreshed header clock rule exists');
})();

// ── Agent Runs date-filter control: markup + styling (issue #7) ─────────
// Static verification of the Clear button in the real index.html filter bar
// and the active-state / Clear-control rules in the real style.css (the
// repo's established substitute for browser-level checks).

console.log('\u25B6 index.html + style.css — Clear control + active state (issue #7)');

(function () {
  var html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  var css = fs.readFileSync(path.join(__dirname, '..', 'style.css'), 'utf8');
  var live = css.replace(/\/\*[\s\S]*?\*\//g, ''); // comment-stripped: assert on real rules only

  // Clear button: always present inside the .agent-runs-filters bar, after
  // the Apply button, and starts disabled (both date inputs are empty).
  assert(html.indexOf('id="ar-filter-clear"') !== -1,
    'index.html: #ar-filter-clear button exists in the filter bar');
  assert(html.indexOf('class="filter-clear" id="ar-filter-clear"') !== -1,
    'index.html: Clear button carries class="filter-clear"');
  assert(/<button[^>]*id="ar-filter-clear"[^>]*disabled/.test(html),
    'index.html: Clear button starts disabled (both date inputs empty)');
  assert(html.indexOf('id="ar-filter-clear"') > html.indexOf('id="ar-filter-apply"'),
    'index.html: Clear button sits after the Apply button in the filter bar');

  // Active styling for populated date inputs — distinct rule, not just focus
  assert(live.indexOf('.filter-input.active') !== -1,
    'style.css: .filter-input.active rule exists (populated input active styling)');
  assert(live.indexOf('.filter-input.active') > live.indexOf('.filter-input:focus'),
    'style.css: .filter-input.active is a separate rule from :focus');

  // Clear control styles: base rule + :disabled state
  assert(live.indexOf('.filter-clear') !== -1, 'style.css: .filter-clear base rule exists');
  assert(live.indexOf('.filter-clear:disabled') !== -1,
    'style.css: .filter-clear:disabled rule exists (disabled state styling)');
})();

// ── Summary ─────────────────────────────────────────────────────────────
// The summary is deferred until ALL pending async test callbacks have
// completed (deterministic, not a fixed timer).  A safety cap at 5 s
// prevents a genuinely hung test from holding the process open forever.

function printSummary() {
  console.log('');
  console.log('\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550');
  console.log('  Passed:', passed, ' / Failed:', failed);
  console.log('\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550');
}

var _summaryScheduled = false;
var _summaryStart = Date.now();
var _MAX_SUMMARY_WAIT_MS = 5000; // safety cap: hung test still reports

function finishWhenIdle() {
  if (pendingAsyncBlocks > 0) {
    if (Date.now() - _summaryStart > _MAX_SUMMARY_WAIT_MS) {
      console.error('  WARNING: async blocks still pending after ' + _MAX_SUMMARY_WAIT_MS + 'ms — printing summary anyway');
      printSummary();
      process.exit(failed > 0 ? 1 : 0);
      return;
    }
    setTimeout(finishWhenIdle, 10);
    return;
  }
  printSummary();
  process.exit(failed > 0 ? 1 : 0);
}

// Schedule the finish poll once at the end of the synchronous test run.
// Each async test block increments pendingAsyncBlocks before scheduling
// its callback and decrements at the end, guaranteeing the summary only
// prints after all pending async work lands.
if (!_summaryScheduled) {
  _summaryScheduled = true;
  _summaryStart = Date.now();
  setTimeout(finishWhenIdle, 10);
}
