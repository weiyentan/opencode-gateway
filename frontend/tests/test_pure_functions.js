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
// app.js runs as an IIFE that exposes resolveProjectLabel and
// buildSessionRowHtml on window.  Evaluating it inside a Node vm sandbox
// (with a minimal DOM stub) means these tests exercise the production code
// itself — not a copy-pasted duplicate — so the rendered session-row markup
// is guaranteed to match what the real dashboard renders.
var fs = require('fs');
var vm = require('vm');
var path = require('path');

(function loadRealAppJs() {
  var appJsPath = path.join(__dirname, '..', 'app.js');
  var source = fs.readFileSync(appJsPath, 'utf8');

  var documentStub = {
    readyState: 'loading',
    querySelector: function () { return null; },
    getElementById: function () { return null; },
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
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: 'app.js' });

  window.resolveProjectLabel = sandboxWindow.resolveProjectLabel;
  window.buildSessionRowHtml = sandboxWindow.buildSessionRowHtml;
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

/** Format a compact Token Breakdown HTML string.
 *  Delegates to fmtTokenBreakdownCompact for the shared compact format.
 *  @param {number|null} inputTokens
 *  @param {number|null} outputTokens
 *  @param {number|null} cacheReadTokens
 *  @param {number|null} cacheWriteTokens
 *  @returns {string} HTML for the cell content
 */
function fmtTokenBreakdown(inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens) {
  return fmtTokenBreakdownCompact(inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens);
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

// ── Tests for fmtTokenBreakdown ─────────────────────────────────────────

console.log('\u25B6 fmtTokenBreakdown');

// Delegates to fmtTokenBreakdownCompact; verify the flat two-line + optional cache line format
assert(fmtTokenBreakdown(0, 0, 0, 0) === '0 total<br>0 in | 0 out', 'all zeros, no cache line');
assert(fmtTokenBreakdown(null, null, null, null) === '0 total<br>0 in | 0 out', 'all nulls, no cache line');
assert(fmtTokenBreakdown(undefined, undefined, undefined, undefined) === '0 total<br>0 in | 0 out', 'all undefined, no cache line');

(function () {
  var result = fmtTokenBreakdown(60000, 36500, 120400, 8200);
  // total=225.1K (input + output + cacheRead + cacheWrite)
  assert(result === '225.1K total<br>60.0K in | 36.5K out<br>120.4K cache read + 8.2K cache write', 'full breakdown with cache read + write — flat two-line + cache line');
})();

assert(fmtTokenBreakdown(100000, 50000, 0, 0) === '150.0K total<br>100.0K in | 50.0K out', 'cache zero both → no cache line');
assert(fmtTokenBreakdown(100000, 50000, null, null) === '150.0K total<br>100.0K in | 50.0K out', 'cache null both → no cache line');

(function () {
  var result = fmtTokenBreakdown(1000, 2000, 3000, 0);
  // total=6.0K (input + output + cacheRead + cacheWrite)
  assert(result === '6.0K total<br>1.0K in | 2.0K out<br>3.0K cache read', 'cache read non-zero → cache read line only');
})();

(function () {
  var result = fmtTokenBreakdown(1000, 2000, 0, 3000);
  // total=6.0K
  assert(result === '6.0K total<br>1.0K in | 2.0K out<br>3.0K cache write', 'cache write non-zero → cache write line only');
})();

assert(fmtTokenBreakdown(1500, 2500, 0, 0).indexOf('active') === -1, 'no "active" label in compact format');
assert(fmtTokenBreakdown(0, 0, 500, 500) === '1.0K total<br>0 in | 0 out<br>500 cache read + 500 cache write', 'cache line present when cache values exist even if input/output are zero');

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

// ── buildSessionRowHtml (production function loaded from app.js) ────────
// The REAL implementation lives in frontend/app.js (exposed on
// window.buildSessionRowHtml by the vm sandbox loader above).  This thin
// wrapper delegates so tests always exercise the production markup contract
// — class="session-row", data-id, data-active, data-status, and exactly
// three .num cells — never a copy-pasted duplicate.

function buildSessionRowHtml(s, clientName, now) {
  return window.buildSessionRowHtml(s, clientName, now);
}

// ── Tests for buildSessionRowHtml ──────────────────────────────────────

console.log('\u25B6 buildSessionRowHtml');

// Reference timestamp for deterministic tests: 2026-08-04 12:00:00 UTC
var testNow = Date.UTC(2026, 7, 4, 12, 0, 0); // Aug 4, 2026

(function () {
  // Active session: last_message_at within SESSION_ACTIVE_WINDOW_MS
  var recentTime = new Date(testNow - 600000).toISOString(); // 10 min ago
  var session = {
    id: 'ses-abc-123',
    first_message_at: recentTime,
    last_message_at: recentTime,
    message_count: 15,
    total_input_tokens: 1000,
    total_output_tokens: 500,
    total_cache_read_tokens: 0,
    total_cache_write_tokens: 0,
    total_estimated_cost_usd: 0.05,
    session_title: 'Test Session'
  };
  var html = buildSessionRowHtml(session, 'TestClient', testNow);
  assert(html.indexOf('data-active="true"') !== -1, 'active session: data-active="true"');
  assert(html.indexOf('data-status="active"') !== -1, 'active session: data-status="active"');
  assert(html.indexOf('data-id="ses-abc-123"') !== -1, 'active session: data-id matches session id');
})();

(function () {
  // Idle session: last_message_at outside SESSION_ACTIVE_WINDOW_MS
  var oldTime = new Date(testNow - 7200000).toISOString(); // 2 hours ago
  var session = {
    id: 'ses-idle-456',
    first_message_at: oldTime,
    last_message_at: oldTime,
    message_count: 3,
    total_input_tokens: 200,
    total_output_tokens: 100,
    total_cache_read_tokens: 0,
    total_cache_write_tokens: 0,
    total_estimated_cost_usd: 0.01,
    session_title: null
  };
  var html = buildSessionRowHtml(session, 'IdleClient', testNow);
  assert(html.indexOf('data-active="false"') !== -1, 'idle session: data-active="false"');
  assert(html.indexOf('data-status="idle"') !== -1, 'idle session: data-status="idle"');
  assert(html.indexOf('data-id="ses-idle-456"') !== -1, 'idle session: data-id matches session id');
})();

(function () {
  // Defensive: the sessions API exposes no error signal today — SessionSummary
  // has no `error` field and the sessions query selects no error column — so a
  // session object carrying an unexpected extra `error` key must still render a
  // plain active/idle status. The row markup must never emit data-status="error".
  var recentTime = new Date(testNow - 300000).toISOString(); // 5 min ago
  var session = {
    id: 'ses-def-789',
    first_message_at: recentTime,
    last_message_at: recentTime,
    message_count: 1,
    total_input_tokens: 50,
    total_output_tokens: 10,
    total_cache_read_tokens: 0,
    total_cache_write_tokens: 0,
    total_estimated_cost_usd: 0.001,
    session_title: 'Defensive Session',
    error: 'unexpected extra field (not part of SessionSummary)'
  };
  var html = buildSessionRowHtml(session, 'DefClient', testNow);
  assert(html.indexOf('data-status="active"') !== -1, 'extra error key ignored: data-status="active" while active');
  assert(html.indexOf('data-status="error"') === -1, 'row markup never emits data-status="error" (no error signal in API)');
})();

(function () {
  // Idle variant: extra error key must not flip an idle session to an error state.
  var oldTime = new Date(testNow - 7200000).toISOString(); // 2 hours ago
  var session = {
    id: 'ses-def-idle',
    first_message_at: oldTime,
    last_message_at: oldTime,
    message_count: 3,
    total_input_tokens: 200,
    total_output_tokens: 100,
    total_cache_read_tokens: 0,
    total_cache_write_tokens: 0,
    total_estimated_cost_usd: 0.01,
    session_title: null,
    error: 'ignored'
  };
  var html = buildSessionRowHtml(session, 'DefClient2', testNow);
  assert(html.indexOf('data-status="idle"') !== -1, 'extra error key ignored: data-status="idle" for idle session');
  assert(html.indexOf('data-status="error"') === -1, 'idle row never emits data-status="error"');
})();

(function () {
  // Verify three .num cells in the row markup
  var recentTime = new Date(testNow - 600000).toISOString();
  var session = {
    id: 'ses-num-test',
    first_message_at: recentTime,
    last_message_at: recentTime,
    message_count: 42,
    total_input_tokens: 100,
    total_output_tokens: 50,
    total_cache_read_tokens: 0,
    total_cache_write_tokens: 0,
    total_estimated_cost_usd: 0.5,
    session_title: 'Numeric Test'
  };
  var html = buildSessionRowHtml(session, 'NumClient', testNow);
  var numMatches = html.match(/class="num"/g);
  var numCount = numMatches ? numMatches.length : 0;
  assert(numCount === 3, 'session row: exactly three .num cells (' + numCount + ' found)');
})();

(function () {
  // class="session-row" is present
  var recentTime = new Date(testNow - 600000).toISOString();
  var session = {
    id: 'ses-cls-test',
    first_message_at: recentTime,
    last_message_at: recentTime,
    message_count: 1,
    total_input_tokens: 0,
    total_output_tokens: 0,
    total_cache_read_tokens: 0,
    total_cache_write_tokens: 0,
    total_estimated_cost_usd: 0,
    session_title: null
  };
  var html = buildSessionRowHtml(session, 'ClsClient', testNow);
  assert(html.indexOf('class="session-row"') !== -1, 'session row: has class="session-row"');
})();

(function () {
  // Balanced Quiet Rows — readable titles: the flexible title column renders
  // the FULL session title (overflow is handled by CSS ellipsis on the
  // .session-title span), never a hard 40-char JS truncation. The tooltip
  // attribute and title column class are preserved.
  var recentTime = new Date(testNow - 600000).toISOString();
  var longTitle = 'A deliberately long session title that exceeds forty characters to verify full-title rendering';
  var session = {
    id: 'ses-long-title',
    first_message_at: recentTime,
    last_message_at: recentTime,
    message_count: 1,
    total_input_tokens: 0,
    total_output_tokens: 0,
    total_cache_read_tokens: 0,
    total_cache_write_tokens: 0,
    total_estimated_cost_usd: 0,
    session_title: longTitle
  };
  var html = buildSessionRowHtml(session, 'LongClient', testNow);
  assert(html.indexOf(longTitle) !== -1, 'title cell renders the full session title (no hard truncation)');
  assert(html.indexOf('&hellip;') === -1, 'title cell does not insert a JS ellipsis (CSS overflow handles clipping)');
  assert(html.indexOf('<span class="session-title">') !== -1, 'title cell wraps text in .session-title span (flexible column ellipsis hook)');
  assert(html.indexOf('class="session-title-col"') !== -1, 'title cell keeps session-title-col class');
})();

(function () {
  // Balanced Quiet Rows — Badge Only status treatment: activity is
  // communicated exclusively through the compact outlined badge in the
  // Status column. Active sessions render badge-active with "active" text;
  // idle sessions render badge-inactive with "ended" text.
  var recentTime = new Date(testNow - 600000).toISOString();
  var activeSession = {
    id: 'ses-badge-active',
    first_message_at: recentTime,
    last_message_at: recentTime,
    message_count: 5,
    total_input_tokens: 100,
    total_output_tokens: 50,
    total_cache_read_tokens: 0,
    total_cache_write_tokens: 0,
    total_estimated_cost_usd: 0.01,
    session_title: 'Badge Active'
  };
  var activeHtml = buildSessionRowHtml(activeSession, 'BadgeClient', testNow);
  assert(activeHtml.indexOf('<span class="badge badge-active">active</span>') !== -1,
    'active session: status cell is the badge-active span with "active" text');
  assert(activeHtml.indexOf('<span class="badge badge-inactive">') === -1,
    'active session: no inactive badge emitted');

  var oldTime = new Date(testNow - 7200000).toISOString(); // 2 hours ago
  var idleSession = {
    id: 'ses-badge-idle',
    first_message_at: oldTime,
    last_message_at: oldTime,
    message_count: 3,
    total_input_tokens: 200,
    total_output_tokens: 100,
    total_cache_read_tokens: 0,
    total_cache_write_tokens: 0,
    total_estimated_cost_usd: 0.01,
    session_title: null
  };
  var idleHtml = buildSessionRowHtml(idleSession, 'BadgeClient2', testNow);
  assert(idleHtml.indexOf('<span class="badge badge-inactive">ended</span>') !== -1,
    'idle session: status cell is the badge-inactive span with "ended" text');
  assert(idleHtml.indexOf('<span class="badge badge-active">') === -1,
    'idle session: no active badge emitted');
})();

// ── Summary ─────────────────────────────────────────────────────────────

console.log('');
console.log('\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550');
console.log('  Passed:', passed, ' / Failed:', failed);
console.log('\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550');

process.exit(failed > 0 ? 1 : 0);
