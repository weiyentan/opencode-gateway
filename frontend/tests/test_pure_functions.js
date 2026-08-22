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

// Completion handshake between the issue #7 Clear-wiring block and the
// issue #5 render block (Nit 2): both share the arTbodyEl fake, so the
// issue #5 block waits on this flag instead of a fixed wall-clock delay
// — deterministic under timer drift.
var arClearWiringCompleted = false;

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
    querySelectorAll: function () { return []; },
    // Attribute store (issue #427): the pagination control renderer reads
    // data-page / disabled via getAttribute, so fakes must hold attributes.
    attributes: {},
    setAttribute: function (name, value) { this.attributes[name] = String(value); },
    getAttribute: function (name) {
      return (name in this.attributes) ? this.attributes[name] : null;
    },
    removeAttribute: function (name) { delete this.attributes[name]; }
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

// Agent Usage panel tbody fake (issue #438): renderAgentUsageTable writes its
// dynamic per-agent rows into this element.  Registered before loadRealAppJs
// so app.js captures the ref in els like the other table fakes.
var agentUsageTbodyEl = makeFakeElement('agent-usage-tbody');
elementRegistry['agent-usage-tbody'] = agentUsageTbodyEl;

// Agent Runs pagination container fake (issue #427): renderAgentRunPagination
// writes the Previous/Next + numbered-page markup into this element and wires
// the buttons through querySelectorAll('button') — so the fake parses the
// rendered innerHTML into fake buttons (each with its data-page attribute and
// disabled state) that tests can click through the production handler.
var arPaginationEl = makeFakeElement('agent-runs-pagination');
arPaginationEl.querySelectorAll = function (selector) {
  if (selector !== 'button') return [];
  // Cache the parsed buttons: real DOM querySelectorAll returns the same
  // live elements (which carry the wired click handlers), so re-parse only
  // after the renderer replaces innerHTML.
  if (this._buttonsHtml === this.innerHTML && this._buttonsCache) {
    return this._buttonsCache;
  }
  var buttons = [];
  var re = /<button\b([^>]*)>[\s\S]*?<\/button>/g;
  var m;
  while ((m = re.exec(this.innerHTML)) !== null) {
    var attrs = m[1];
    var btn = makeFakeElement('pagination-button');
    var pageMatch = /data-page="(\d+)"/.exec(attrs);
    if (pageMatch) btn.setAttribute('data-page', pageMatch[1]);
    if (/\bdisabled\b/.test(attrs)) btn.disabled = true;
    buttons.push(btn);
  }
  this._buttonsCache = buttons;
  this._buttonsHtml = this.innerHTML;
  return buttons;
};
elementRegistry['agent-runs-pagination'] = arPaginationEl;

// Agent Runs filter-bar fakes for the apply/reset paths (issue #428): the
// Apply button, the agent/status inputs (so applyFilters' UI-read sees
// them), and the page-size <select> (25/50/100, default 50).  Registered
// before loadRealAppJs so app.js captures them in els like the issue #7
// fakes; empty initial values keep the existing filter behavior intact.
var arFilterApplyEl = makeFakeElement('ar-filter-apply');
var arFilterAgentEl = makeFakeElement('ar-filter-agent');
var arFilterStatusEl = makeFakeElement('ar-filter-status');
var arPageSizeEl = makeFakeElement('ar-page-size');
elementRegistry['ar-filter-apply'] = arFilterApplyEl;
elementRegistry['ar-filter-agent'] = arFilterAgentEl;
elementRegistry['ar-filter-status'] = arFilterStatusEl;
elementRegistry['ar-page-size'] = arPageSizeEl;

// AFK Outcomes fakes (issue #453): the runs-list tbody and the chain detail
// overlay elements.  Registered before loadRealAppJs so app.js captures them
// in els (like the agent-runs fakes above); renderAfkOutcomesTable writes its
// rows into afk-runs-tbody and renderAfkRunDetail writes the chain HTML into
// afk-detail-body.  The overlay/title/close fakes back the open/close wiring
// test (openAfkRunDetail flips afk-detail-overlay to visible).
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

// Transcript view fakes (issue #469): the session input, load button, header,
// view toggle, view containers, next page button, and status text.  Registered
// before loadRealAppJs so app.js captures them in els (like the AFK fakes).
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

// Agent Run detail overlay fakes (issue #473): the AFK session drill-down
// opens the existing Agent Run detail overlay via openAgentRunDetail, which
// reads/writes els.arDetailOverlay / arDetailBody / arDetailTitle.  Registered
// before loadRealAppJs so app.js captures them in els (like the AFK fakes).
var arDetailOverlayEl = makeFakeElement('ar-detail-overlay');
var arDetailTitleEl = makeFakeElement('ar-detail-title');
var arDetailBodyEl = makeFakeElement('ar-detail-body');
var arDetailCloseEl = makeFakeElement('ar-detail-close');
elementRegistry['ar-detail-overlay'] = arDetailOverlayEl;
elementRegistry['ar-detail-title'] = arDetailTitleEl;
elementRegistry['ar-detail-body'] = arDetailBodyEl;
elementRegistry['ar-detail-close'] = arDetailCloseEl;

// Browser-history stub (issue #426): records pushState URLs so tests can
// verify that Agent Runs page changes persist to the URL without touching
// the table DOM (row content changes only through the normal fetch path).
// Issue #428: replaceState is recorded separately — page-size changes and
// filter resets must REPLACE the URL state instead of pushing a history
// entry per adjustment.
var historyCalls = [];
var historyReplaceCalls = [];
var historyStub = {
  pushState: function (state, title, url) { historyCalls.push(url); },
  replaceState: function (state, title, url) { historyReplaceCalls.push(url); }
};

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
    // issue #426: location gains search/pathname for the URL-pagination
    // wiring (read on load + history persistence); URLSearchParams is
    // provided explicitly because vm contexts do not inherit Node globals.
    location: { href: '', search: '', pathname: '' },
    history: historyStub,
    URLSearchParams: URLSearchParams,
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
  // Issue #557: provider badge/missing-label, cache hit ratio, and the
  // Token Breakdown detail-section builder join the window test seam.
  window.fmtProvider = sandboxWindow.fmtProvider;
  window.fmtCacheHitRatio = sandboxWindow.fmtCacheHitRatio;
  window.fmtTokenBreakdownSection = sandboxWindow.fmtTokenBreakdownSection;
  window.readFiltersFromUI = sandboxWindow.readFiltersFromUI;
  window.computeArDateFilterState = sandboxWindow.computeArDateFilterState;
  window.syncArDateFilterUI = sandboxWindow.syncArDateFilterUI;
  window.clearArDateFilters = sandboxWindow.clearArDateFilters;
  window.setupAgentRunEventHandlers = sandboxWindow.setupAgentRunEventHandlers;
  // Issue #412: the Agent Runs URL builder derives from_date/to_date from
  // the closure state (agentRunFilters + dateRangeState), so the harness
  // gets the builder itself plus setters to drive the fallback and
  // explicit-override behavior without a DOM.
  window.buildAgentRunsUrl = sandboxWindow.buildAgentRunsUrl;
  window.setAgentRunFilters = sandboxWindow.setAgentRunFilters;
  window.setDateRangeState = sandboxWindow.setDateRangeState;
  // Issue #426: Agent Runs pagination state + URL persistence — the pure
  // URL-param parser, the on-load URL reader, and the page-change history
  // hook are exercised through the same window test seam.
  window.parseAgentRunPagination = sandboxWindow.parseAgentRunPagination;
  window.readAgentRunPaginationFromUrl = sandboxWindow.readAgentRunPaginationFromUrl;
  window.setAgentRunPage = sandboxWindow.setAgentRunPage;
  // Issue #427: Agent Runs pagination controls — the pure page-item window
  // calculator and the control renderer (which wires the page-button click
  // path through setAgentRunPage + the shared fetch/render path).
  window.computePageItems = sandboxWindow.computePageItems;
  window.renderAgentRunPagination = sandboxWindow.renderAgentRunPagination;
  // Issue #428: Agent Runs page-size validation + reset semantics — the
  // size validator (25/50/100, fallback 50) and the size-change hook
  // (reset to page 1, history.replaceState URL) join the same window seam.
  window.parseAgentRunPageSize = sandboxWindow.parseAgentRunPageSize;
  window.setAgentRunPageSize = sandboxWindow.setAgentRunPageSize;
  // Issue #429: Agent Runs pagination resilience — the pure nearest-valid-
  // page calculator and the shared agent-runs fetch path (refresh-style
  // refetch, loading/error row retention, invalid-page fallback).
  window.nearestValidAgentRunPage = sandboxWindow.nearestValidAgentRunPage;
  window.fetchAgentRunsAndRender = sandboxWindow.fetchAgentRunsAndRender;
  window.handleAgentRunPopstate = sandboxWindow.handleAgentRunPopstate;
  // Issue #438: Agent Usage panel — the pure row-derivation helper and the
  // render function (renders through the fake 'agent-usage-tbody' element).
  window.buildAgentUsageRows = sandboxWindow.buildAgentUsageRows;
  window.renderAgentUsageTable = sandboxWindow.renderAgentUsageTable;
  // Issue #453: AFK Outcomes view — the locked-vocabulary outcome/run badge
  // mappings, confidence/evidence formatters, provisional-link predicate, the
  // canonical chain composer, and the render functions (string builders plus
  // the tbody/detail-body writers), exposed on the same window test seam.
  window.outcomeStatusBadgeClass = sandboxWindow.outcomeStatusBadgeClass;
  window.outcomeStatusLabel = sandboxWindow.outcomeStatusLabel;
  window.afkRunStatusBadgeClass = sandboxWindow.afkRunStatusBadgeClass;
  window.fmtConfidence = sandboxWindow.fmtConfidence;
  window.fmtEvidence = sandboxWindow.fmtEvidence;
  window.isProvisionalLink = sandboxWindow.isProvisionalLink;
  window.resolveAfkSessionDrilldown = sandboxWindow.resolveAfkSessionDrilldown;
  window.buildAfkChain = sandboxWindow.buildAfkChain;
  window.renderAfkEntityLink = sandboxWindow.renderAfkEntityLink;
  window.renderAfkSessionLink = sandboxWindow.renderAfkSessionLink;
  window.renderAfkChainStep = sandboxWindow.renderAfkChainStep;
  window.renderAfkRunDetail = sandboxWindow.renderAfkRunDetail;
  window.renderAfkOutcomesTable = sandboxWindow.renderAfkOutcomesTable;
  window.openAfkRunDetail = sandboxWindow.openAfkRunDetail;
  // Issue #469: Transcript view — pure helpers for UUID validation, depth
  // formatting, part-type classification, and the header/timeline/message/part
  // renderers, exposed on the same window test seam.
  window.isValidSessionId = sandboxWindow.isValidSessionId;
  window.fmtTranscriptDepth = sandboxWindow.fmtTranscriptDepth;
  window.depthClass = sandboxWindow.depthClass;
  window.transcriptPartTypeClass = sandboxWindow.transcriptPartTypeClass;
  window.renderTranscriptHeader = sandboxWindow.renderTranscriptHeader;
  window.renderTimelineEvent = sandboxWindow.renderTimelineEvent;
  window.renderTranscriptMessage = sandboxWindow.renderTranscriptMessage;
  window.renderTranscriptPart = sandboxWindow.renderTranscriptPart;
  window.renderTranscriptList = sandboxWindow.renderTranscriptList;
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
  if (total == null || total <= 0) return '0/0';
  var c = Number(completed) || 0;
  if (c < 0) c = 0;
  if (c > total) c = total;
  var width = 5;
  var filled = Math.round(c / total * width);
  var bar = '';
  for (var i = 0; i < width; i++) {
    bar += i < filled ? '█' : '░';
  }
  return bar + ' ' + c + '/' + total;
}

function statusBadgeClass(status) {
  if (status === 'running') return 'badge-running';
  if (status === 'stale') return 'badge-stale';
  if (status === 'completed') return 'badge-completed';
  if (status === 'blocked') return 'badge-blocked';
  return 'badge-unknown';
}

function fmtCodeChangesDiff(additions, deletions) {
  if (additions == null || deletions == null) return '--';
  var add = Number(additions);
  var del = Number(deletions);
  if (add === 0 && del === 0) return '--';
  if (add === 0) return '-' + fmtNum(del);
  if (del === 0) return '+' + fmtNum(add);
  return '+' + fmtNum(add) + '/-' + fmtNum(del);
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

assert(fmtTodoProgress(null, null) === '0/0', 'null inputs → 0/0');
assert(fmtTodoProgress(undefined, undefined) === '0/0', 'undefined inputs → 0/0');
assert(fmtTodoProgress(0, 0) === '0/0', 'zero total → 0/0');
assert(fmtTodoProgress(3, 5) === '███░░ 3/5', '3/5 → bar + 3/5');
assert(fmtTodoProgress(0, 10) === '░░░░░ 0/10', '0/10 → empty bar + 0/10');
assert(fmtTodoProgress(5, 5) === '█████ 5/5', '5/5 → full bar + 5/5');
assert(fmtTodoProgress(2, 7) === '█░░░░ 2/7', '2/7 → bar + 2/7');
assert(fmtTodoProgress(2, null) === '0/0', 'null total → 0/0');

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

// ── Tests for fmtCodeChangesDiff ─────────────────────────────────────────

console.log('\u25B6 fmtCodeChangesDiff');

assert(fmtCodeChangesDiff(null, null) === '--', 'null/null → --');
assert(fmtCodeChangesDiff(undefined, undefined) === '--', 'undefined/undefined → --');
assert(fmtCodeChangesDiff(null, 3) === '--', 'null additions → --');
assert(fmtCodeChangesDiff(15, undefined) === '--', 'undefined deletions → --');
assert(fmtCodeChangesDiff(0, 0) === '--', '0/0 → --');
assert(fmtCodeChangesDiff(15, 3) === '+15/-3', '15/3 → +15/-3');
assert(fmtCodeChangesDiff(120, 0) === '+120', '120/0 → +120 (zero side suppressed)');
assert(fmtCodeChangesDiff(0, 42) === '-42', '0/42 → -42 (zero side suppressed)');
assert(fmtCodeChangesDiff(7, 0) === '+7', '7/0 → +7 (pure additions)');
assert(fmtCodeChangesDiff(0, 7) === '-7', '0/7 → -7 (pure deletions)');
assert(fmtCodeChangesDiff(1000, 500) === '+1.0K/-500', '1000/500 → +1.0K/-500');


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

    // Stale-while-failure: a failing background refresh keeps last-known names.
    // The 500 is intentional here: this block exercises the production
    // stale-while-failure catch (app.js refreshClientCache), which emits an
    // EXPECTED console.error.  Suppress that error for this block only and
    // restore the real console.error once the assertions have run (Nit 3).
    appJsSandbox.fetch = function (url) {
      calls.push(url);
      return Promise.resolve({ ok: false, status: 500 });
    };
    var savedConsoleError = appJsSandbox.console.error;
    appJsSandbox.console.error = function () {};
    window.ensureClientName('unknown-2'); // miss → background refresh → fetch fails
    pendingAsyncBlocks++;
    setTimeout(function () {
      assert(window.ensureClientName('c1') === 'Alpha',
        'stale-while-failure: labels survive a failed background refresh');
      assert(calls.length === 2,
        'one fetch per miss: hits never fetch, a failed refresh never clears the map');
      appJsSandbox.console.error = savedConsoleError; // restore the real console.error
      // Restore a benign default fetch stub so any subsequent background
      // refresh in later blocks never hits the intentional 500 stub (Nit 3).
      appJsSandbox.fetch = function () {
        return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ items: [] }); } });
      };
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
   'model-mix', 'events', 'collector-dist', 'collectors', 'agents', 'agent-usage', 'agent-runs', 'client-project']
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
  // disabled, and the existing filter path re-applied with no EXPLICIT
  // filter dates — the URL then falls back to the shared dashboard date
  // range (issue #412: unset boundaries inherit the dashboard range, so
  // Clear restores the dashboard-scoped list instead of an all-time one).
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
  // Issue #412: with no explicit From/To dates the URL derives the shared
  // dashboard range (default this-month preset here) instead of carrying
  // no date params at all.  The to_date end is "now" (millisecond
  // precision), so compare the start exactly and the end within a small
  // tolerance to avoid clock-skew flakes.
  var clearExpected = resolveDateRange({ preset: 'this-month' });
  assert(calls[0].indexOf('from_date=' + encodeURIComponent(clearExpected.startDate.toISOString())) !== -1,
    'Clear click: from_date derives the shared dashboard range start (issue #412)');
  var clearToMatch = calls[0].match(/to_date=([^&]+)/);
  assert(clearToMatch !== null &&
         !isNaN(new Date(decodeURIComponent(clearToMatch[1])).getTime()) &&
         Math.abs(new Date(decodeURIComponent(clearToMatch[1])).getTime() - clearExpected.endDate.getTime()) < 10000,
    'Clear click: to_date derives the shared dashboard range end (issue #412)');
  assert(calls[0].indexOf('start_date=') === -1 && calls[0].indexOf('end_date=') === -1,
    'Clear click: URL carries no Overview global date-range params (filters are independent)');

  // The re-applied fetch renders the unfiltered (empty-state) table
  pendingAsyncBlocks++;
  setTimeout(function () {
    assert(arTbodyEl.innerHTML.indexOf('No agent runs') !== -1,
      'Clear click: unfiltered render completed (empty-state row written to the table)');
    arClearWiringCompleted = true; // handshake: the issue #5 render block may proceed (Nit 2)
    pendingAsyncBlocks--;
  }, 10);
})();

// ── Agent Runs "Last Updated" cell — absolute + muted relative (issue #5) ─
// Structural row-markup coverage for the Last Updated cell: the production
// row template (renderAgentRunsTable) renders the year-inclusive absolute
// local timestamp (issue #4 formatter) as the primary value with the
// relative label as muted secondary text after a middot separator ('·'),
// and a bare '--' when the timestamp is missing.  Driven through the real
// render path (clearArDateFilters -> applyFilters -> apiFetch ->
// renderAgentRunsTable) with a stubbed fetch, so the assertions run
// against the actual app.js row markup.  The expected absolute string is
// derived FROM the window.formatAgentRunTimestamp seam itself (not
// hard-coded), proving the cell output comes from the production
// formatter — no copy-pasted duplicate.

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

  // Static: the header column count (issue #557 extended the merged table
  // from 11 to 15 columns with Provider, Cache Read, Cache Write, and
  // Reasoning — the relative label itself still lives inside the existing
  // Last Updated cell, no new column for it).
  var headerHtml = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  var arThead = headerHtml.slice(
    headerHtml.indexOf('<table id="agent-runs-table">'),
    headerHtml.indexOf('</thead>', headerHtml.indexOf('<table id="agent-runs-table">'))
  );
  // <th> followed by '>' or whitespace — <thead> does not count as a column
  var thCount = (arThead.match(/<th[\s>]/g) || []).length;
  assert(thCount === 15, 'index.html: agent-runs header carries 15 columns (' + thCount + ' found)');
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
  // async assertions (both blocks share the arTbodyEl fake): instead of a
  // fixed wall-clock delay, this block polls the arClearWiringCompleted
  // flag that the issue #7 block sets after its assertions, so ordering is
  // deterministic under timer drift and never clobbers the earlier block's
  // expected empty-state markup (Nit 2).
  // The block's single pendingAsyncBlocks++ is balanced only in the
  // innermost callback, so the summary poll can never observe
  // pendingAsyncBlocks === 0 while any nested callback is still pending.
  pendingAsyncBlocks++;
  var clearWaitAttempts = 0;
  (function proceedWhenClearWiringDone() {
    if (!arClearWiringCompleted) {
      clearWaitAttempts++;
      if (clearWaitAttempts > 200) {
        throw new Error('issue #5 block: arClearWiringCompleted never set ' +
          '(issue #7 Clear-wiring block did not complete within 1s)');
      }
      setTimeout(proceedWhenClearWiringDone, 5);
      return;
    }
    // Reset the fakes and wire the Clear handler like the app bootstrap does.
    arFilterFromEl.value = '';
    arFilterToEl.value = '';
    arFilterClearEl.disabled = false;
    arTbodyEl.innerHTML = '';
    // Stub the vm-context clock so the production render path
    // (formatAgentRunTimestamp without an injected now, and fmtRelative)
    // is deterministic — no wall-clock dependency (Finding 1).
    vm.runInContext('Date.now = function () { return ' + refNow.getTime() + '; }', appJsSandbox);
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

      // Timestamp row: absolute primary + middot + muted relative secondary
      // in the SAME Last Updated cell, and nothing else.  With Date.now
      // stubbed to refNow, the fixture (30 days + 1.5h in the past) renders
      // the deterministic label "30d ago" — asserted exactly (Finding 1).
      var rowAbs = html.slice(html.indexOf('data-id="run-abs-1"'),
        html.indexOf('</tr>', html.indexOf('data-id="run-abs-1"')) + 5);
      var cellAbs = rowAbs.slice(rowAbs.indexOf('<td data-label="Last Updated">'),
        rowAbs.indexOf('</td>', rowAbs.indexOf('<td data-label="Last Updated">')) + 5);
      assert(new RegExp('^<td data-label="Last Updated">' + expectedAbs +
        ' · <span class="ar-rel-time">30d ago<\\/span><\\/td>$').test(cellAbs),
        'row markup: absolute timestamp primary + deterministic relative label ("30d ago") as muted secondary span in one cell');
      assert(cellAbs.indexOf('--') === -1,
        'row markup: no -- fallback when the timestamp is present');

      // Missing-timestamp row: bare '--' only — no secondary span, row intact.
      var rowMiss = html.slice(html.indexOf('data-id="run-missing-2"'),
        html.indexOf('</tr>', html.indexOf('data-id="run-missing-2"')) + 5);
      var cellMiss = rowMiss.slice(rowMiss.indexOf('<td data-label="Last Updated">'),
        rowMiss.indexOf('</td>', rowMiss.indexOf('<td data-label="Last Updated">')) + 5);
      assert(/^<td data-label="Last Updated">--<\/td>$/.test(cellMiss),
        'row markup: missing timestamp renders bare -- without breaking the row');

      // Empty state: the colspan="15" invariant reflects the four v1.2
      // columns added by issue #557 (Provider, Cache Read, Cache Write,
      // Reasoning) — the row markup still spans the full 15-column table.
      appJsSandbox.fetch = function () {
        return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ items: [] }); } });
      };
      arFilterClearEl._handlers.click();
      setTimeout(function () {
        assert(arTbodyEl.innerHTML.indexOf('colspan="15"') !== -1,
          'empty state: colspan="15" preserved');
        assert(arTbodyEl.innerHTML.indexOf('No agent runs') !== -1,
          'empty state: "No agent runs" message intact');
        pendingAsyncBlocks--; // balances the block's single increment (above)
      }, 10);
    }, 10);
  })();
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

  // Five tabs: top-nav item + matching content panel (the Sessions tab was
  // merged into Agent Runs -- issue #402; AFK Outcomes added -- issue #453;
  // Transcript added -- issue #469)
  var tabs = ['overview', 'agent-runs', 'clients-projects', 'afk-outcomes', 'transcript'];
  tabs.forEach(function (tab) {
    assert(html.indexOf('data-tab="' + tab + '"') !== -1, 'top nav: item for tab "' + tab + '" exists');
    assert(html.indexOf('id="tab-' + tab + '"') !== -1, 'top nav: tab-content panel #tab-' + tab + ' exists');
  });

  // Keyboard reachability: every top-nav item is focusable (tabindex="0"),
  // so tabbing enters Overview → Agent Runs → Clients / Projects → AFK Outcomes.
  var navItemCount = (html.match(/class="top-nav-item/g) || []).length;
  assert(navItemCount === 5, 'top nav: exactly five top-nav-item elements (' + navItemCount + ' found)');
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

  // 761–1024px tablet band (issue #412): the shared chrome is taller than
  // desktop (the 5-card KPI row wraps to two rows in the 3-column grid), so
  // the Agent Runs full-viewport height gets a per-band override here.
  assert(tabletBlock.indexOf('#tab-agent-runs.active') !== -1 &&
         tabletBlock.indexOf('height: calc(100vh') !== -1,
    'tablet block scopes a per-band Agent Runs tab height (taller chrome offset, issue #412)');

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

  // ≤760px phone band (issue #412): the shared chrome is tallest here (the
  // KPI row wraps to three rows in the 2-column grid, the header wraps, the
  // date-range bar wraps), so the Agent Runs full-viewport height gets its
  // own per-band override — the stacked-card table scrolls inside the panel.
  assert(phoneBlock.indexOf('#tab-agent-runs.active') !== -1 &&
         phoneBlock.indexOf('height: calc(100vh') !== -1,
    'phone block scopes a per-band Agent Runs tab height (stacked-cards chrome offset, issue #412)');

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

// ── Agent Runs full-viewport layout + aggregate population (issue #411) ─
// Two-part dashboard fix, verified statically against the real stylesheet
// and markup (the repo's established substitute for browser-level checks):
// (1) the active Agent Runs tab fills the viewport — the panel becomes a
// flex column and its .table-scroll owns the vertical scroll region, so a
// long run list scrolls inside the panel instead of the page; (2) the
// shared date-range bar and KPI row (aggregate totals — Active Tokens,
// Est. Cost, Sessions from the aggregates total row) sit ABOVE the tab
// panels, so the aggregate totals render on the Agent Runs tab with the
// dashboard date range applied.  The viewport-derived tab height is
// band-scoped (issue #412): the 459px chrome estimate holds for >1024px
// desktop only, so the desktop height lives inside a
// @media (min-width: 1025px) wrapper, and the 761–1024px / ≤760px bands
// carry per-band height overrides inside their existing media blocks (the
// .active display/flex properties stay available across all bands).

console.log('\u25B6 Agent Runs full-viewport layout + aggregate population (issue #411)');

(function () {
  var html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  var css = fs.readFileSync(path.join(__dirname, '..', 'style.css'), 'utf8');

  // ── Part 1: full-viewport layout ────────────────────────────────────
  // The active Agent Runs tab is a flex column sized from the viewport
  // (leaving room for the fixed chrome), so the panel can fill it.  The
  // display/flex properties are shared across all viewport bands; only the
  // viewport-derived height is band-scoped (issue #412).
  var arTabRule = css.match(/#tab-agent-runs\.active\s*\{[^}]*\}/);
  assert(arTabRule !== null,
    'style.css: #tab-agent-runs.active rule exists (full-viewport layout hook)');
  assert(arTabRule[0].indexOf('display: flex') !== -1 &&
         arTabRule[0].indexOf('flex-direction: column') !== -1,
    'style.css: the active Agent Runs tab is a flex column (all viewport bands)');
  assert(arTabRule[0].indexOf('calc(100vh') === -1,
    'style.css: the viewport-derived height is band-scoped, not top-level (issue #412)');
  assert(arTabRule[0].indexOf('min-height: 340px') !== -1,
    'style.css: the active Agent Runs tab keeps the 340px min-height floor');

  // Desktop (>1024px) band: the 459px-chrome height lives inside a
  // min-width media query so it cannot leak into the narrower bands.
  var desktopIdx = css.indexOf('@media (min-width: 1025px)');
  assert(desktopIdx !== -1,
    'style.css: desktop media query @media (min-width: 1025px) exists (issue #412)');
  var desktopBlock = css.slice(desktopIdx, css.indexOf('@media (max-width: 1024px)'));
  assert(desktopBlock.indexOf('#tab-agent-runs.active') !== -1 &&
         desktopBlock.indexOf('calc(100vh - 459px)') !== -1,
    'style.css: the 459px desktop height is scoped inside @media (min-width: 1025px)');

  // The panel stretches to fill the tab and stacks title → filters →
  // scroll region vertically; min-height: 0 lets the scroll region shrink
  // instead of overflowing the fixed tab height.
  var arPanelRule = css.match(/\.panel-agent-runs\s*\{[^}]*\}/);
  assert(arPanelRule !== null,
    'style.css: .panel-agent-runs rule exists');
  assert(arPanelRule[0].indexOf('display: flex') !== -1 &&
         arPanelRule[0].indexOf('flex-direction: column') !== -1,
    'style.css: .panel-agent-runs is a flex column (title + filters + scroll region stack)');
  assert(arPanelRule[0].indexOf('flex: 1') !== -1,
    'style.css: .panel-agent-runs flexes to fill the active tab');
  assert(arPanelRule[0].indexOf('min-height: 0') !== -1,
    'style.css: .panel-agent-runs min-height: 0 (inner scroll region may shrink, no clip)');

  // The table scroll region owns BOTH axes: horizontal overflow from the
  // base .table-scroll rule, vertical overflow added for the full-viewport
  // panel — the page itself must not scroll for long run lists.
  var arScrollRule = css.match(/\.panel-agent-runs \.table-scroll\s*\{[^}]*\}/);
  assert(arScrollRule !== null,
    'style.css: .panel-agent-runs .table-scroll rule exists');
  assert(arScrollRule[0].indexOf('overflow-y: auto') !== -1,
    'style.css: the Agent Runs table-scroll owns the vertical scroll region');
  assert(arScrollRule[0].indexOf('flex: 1') !== -1,
    'style.css: the Agent Runs table-scroll flexes to fill the panel');

  // ── Part 2: aggregate data population in the Agent Runs view ────────
  // The date-range bar and the KPI row (aggregate totals from the
  // aggregates total row) live ABOVE the tab panels — outside
  // #tab-overview — so they render on every tab, including Agent Runs,
  // with the dashboard date range applied (renderKPIs already populates
  // them from aggTotal on every refresh cycle).
  var tabOverviewIdx = html.indexOf('id="tab-overview"');
  assert(tabOverviewIdx !== -1, 'index.html: #tab-overview still exists');
  var drBarIdx = html.indexOf('id="date-range-bar"');
  var kpiRowIdx = html.indexOf('id="kpi-row"');
  assert(drBarIdx !== -1 && drBarIdx < tabOverviewIdx,
    'index.html: #date-range-bar sits above the tab panels (shared across tabs, not scoped to Overview)');
  assert(kpiRowIdx !== -1 && kpiRowIdx < tabOverviewIdx,
    'index.html: #kpi-row (aggregate totals) sits above the tab panels (visible on the Agent Runs tab)');

  // The Agent Runs tab itself keeps its full structure: panel, filters,
  // table id, and tbody — the layout/aggregate fix must not drop markup.
  var arTabHtml = html.slice(html.indexOf('id="tab-agent-runs"'));
  assert(arTabHtml.indexOf('panel-agent-runs') !== -1,
    'index.html: Agent Runs tab still carries the .panel-agent-runs panel');
  assert(arTabHtml.indexOf('id="agent-runs-filters"') !== -1,
    'index.html: Agent Runs tab still carries the filter bar');
  assert(arTabHtml.indexOf('<table id="agent-runs-table">') !== -1 &&
         arTabHtml.indexOf('id="agent-runs-tbody"') !== -1,
    'index.html: Agent Runs tab still carries the merged table + tbody');
})();

// ── Agent Runs date-range fallback (issue #412) ─────────────────────────
// buildAgentRunsUrl() must share the dashboard date range: when the user
// has NOT explicitly set From/To filter dates, from_date/to_date derive
// from dateRangeState via resolveDateRange (the same helper the aggregates
// URLs use), so the run list shares the KPI time window.  Explicit filter
// dates (set via Apply) always win; a dashboard range change re-derives
// the URL on the next fetch (buildAgentRunsUrl is called from fetchAll()).
// The builder is exercised through the window test seam against the REAL
// app.js closure state (set via the setAgentRunFilters/setDateRangeState
// hooks) — a custom preset pins the expected date values exactly.

console.log('\u25B6 buildAgentRunsUrl date-range fallback (issue #412)');

(function () {
  if (typeof window.buildAgentRunsUrl !== 'function' ||
      typeof window.setAgentRunFilters !== 'function' ||
      typeof window.setDateRangeState !== 'function') {
    assert(false, 'app.js: buildAgentRunsUrl + state setters exposed on the window test seam');
    return;
  }

  // Fallback: empty filters + dashboard range → URL derives from/to dates
  // from dateRangeState.  Custom preset makes the expected values exact.
  window.setAgentRunFilters({});
  window.setDateRangeState({ preset: 'custom', customStartDate: '2026-06-01', customEndDate: '2026-06-30' });
  var url = window.buildAgentRunsUrl();
  assert(url.indexOf('/api/v1/usage/agent-runs?') === 0,
    'fallback: URL still targets the agent-runs endpoint');
  assert(url.indexOf('from_date=2026-06-01T00%3A00%3A00.000Z') !== -1,
    'fallback: empty filters derive from_date from dateRangeState (2026-06-01)');
  assert(url.indexOf('to_date=2026-06-30T23%3A59%3A59.000Z') !== -1,
    'fallback: empty filters derive to_date from dateRangeState (2026-06-30)');

  // Explicit filter dates (set via Apply) always win over the derived ones.
  window.setAgentRunFilters({ from_date: '2026-01-15T00:00:00Z', to_date: '2026-01-31T23:59:59Z' });
  url = window.buildAgentRunsUrl();
  assert(url.indexOf('from_date=2026-01-15T00%3A00%3A00Z') !== -1 &&
         url.indexOf('to_date=2026-01-31T23%3A59%3A59Z') !== -1,
    'explicit: Apply-set From/To dates win over the derived range');
  assert(url.indexOf('2026-06-01') === -1,
    'explicit: the derived from_date is suppressed when an explicit one is set');

  // Per-side fallback: an unset boundary inherits from the dashboard range
  // while the explicitly-set boundary stays.
  window.setAgentRunFilters({ from_date: '2026-01-15T00:00:00Z' });
  url = window.buildAgentRunsUrl();
  assert(url.indexOf('from_date=2026-01-15T00%3A00%3A00Z') !== -1 &&
         url.indexOf('to_date=2026-06-30T23%3A59%3A59.000Z') !== -1,
    'per-side: explicit from_date kept, unset to_date inherits the dashboard range');

  // Re-derivation: changing the dashboard date range changes the fallback
  // dates (buildAgentRunsUrl is re-called from fetchAll on every refresh).
  window.setAgentRunFilters({});
  window.setDateRangeState({ preset: 'custom', customStartDate: '2026-07-01', customEndDate: '2026-07-31' });
  url = window.buildAgentRunsUrl();
  assert(url.indexOf('from_date=2026-07-01T00%3A00%3A00.000Z') !== -1 &&
         url.indexOf('to_date=2026-07-31T23%3A59%3A59.000Z') !== -1,
    're-derive: a dashboard range change re-derives from_date/to_date');
  assert(url.indexOf('2026-06-01') === -1,
    're-derive: the previous derived range is gone after the range change');

  // Default this-month preset: falls back to the same derivation the
  // aggregates URLs use (compare against the harness resolveDateRange).
  // The to_date end is "now" (millisecond precision), so compare it within
  // a small tolerance to avoid clock-skew flakes.
  window.setAgentRunFilters({});
  window.setDateRangeState({ preset: 'this-month' });
  url = window.buildAgentRunsUrl();
  var expected = resolveDateRange({ preset: 'this-month' });
  assert(url.indexOf('from_date=' + encodeURIComponent(expected.startDate.toISOString())) !== -1,
    'this-month: default preset derives from_date matching resolveDateRange');
  var toMatch = url.match(/to_date=([^&]+)/);
  assert(toMatch !== null &&
         !isNaN(new Date(decodeURIComponent(toMatch[1])).getTime()) &&
         Math.abs(new Date(decodeURIComponent(toMatch[1])).getTime() - expected.endDate.getTime()) < 10000,
    'this-month: default preset derives to_date matching resolveDateRange');

  // Agent/status filters still ride along with the derived dates.
  window.setAgentRunFilters({ agent: 'bob', status: 'completed' });
  url = window.buildAgentRunsUrl();
  assert(url.indexOf('agent=bob') !== -1 && url.indexOf('status=completed') !== -1 &&
         url.indexOf('from_date=') !== -1 && url.indexOf('to_date=') !== -1,
    'agent/status: non-date filters are appended alongside the derived range');
})();

// ── Agent Runs pagination state (issue #426) ─────────────────────────────
// The dashboard reads `page` and `page_size` from the URL on load, safely
// defaults missing/malformed/unsupported values to page 1 and 50 rows,
// translates page state to the existing agent-runs `limit`/`offset` API
// params, and persists page changes through browser history.  Agent Runs
// row content, columns, ordering, and filters are untouched, and the API
// contract is unchanged (the backend already supports limit/offset/total).

console.log('\u25B6 Agent Runs pagination state (issue #426)');

// ── Parser: defaulting and validation ───────────────────────────────────
// parseAgentRunPagination() reads page/page_size from a query string;
// missing, malformed, or unsupported values fall back to page 1 and the
// default page size (50).  The supported page_size range mirrors the API's
// limit bounds (1–1000); a valid page is a whole number >= 1.

(function () {
  if (typeof window.parseAgentRunPagination !== 'function') {
    assert(false, 'app.js: parseAgentRunPagination exposed on the window test seam');
    return;
  }

  // Missing params → page 1 / page size 50.
  var p = window.parseAgentRunPagination('');
  assert(p.page === 1 && p.pageSize === 50,
    'parse: empty query defaults to page 1 and page size 50');

  // Valid values are read through.
  p = window.parseAgentRunPagination('page=2&page_size=100');
  assert(p.page === 2 && p.pageSize === 100,
    'parse: page=2&page_size=100 is read through');

  // Malformed values fall back per-field.
  p = window.parseAgentRunPagination('page=abc&page_size=100');
  assert(p.page === 1 && p.pageSize === 100,
    'parse: non-numeric page falls back to 1, valid page_size kept');
  p = window.parseAgentRunPagination('page=2&page_size=abc');
  assert(p.page === 2 && p.pageSize === 50,
    'parse: valid page kept, non-numeric page_size falls back to 50');

  // Out-of-range / non-whole values fall back.
  p = window.parseAgentRunPagination('page=0');
  assert(p.page === 1,
    'parse: page=0 falls back to page 1');
  p = window.parseAgentRunPagination('page=-3');
  assert(p.page === 1,
    'parse: negative page falls back to page 1');
  p = window.parseAgentRunPagination('page=2.5');
  assert(p.page === 1,
    'parse: fractional page falls back to page 1');
  p = window.parseAgentRunPagination('page_size=0');
  assert(p.pageSize === 50,
    'parse: page_size=0 falls back to page size 50');
  p = window.parseAgentRunPagination('page_size=-10');
  assert(p.pageSize === 50,
    'parse: negative page_size falls back to page size 50');
  p = window.parseAgentRunPagination('page_size=5000');
  assert(p.pageSize === 50,
    'parse: page_size above the API limit bound (1000) falls back to 50');
})();

// ── URL read + limit/offset translation ─────────────────────────────────
// readAgentRunPaginationFromUrl() seeds the pagination state from
// location.search on dashboard load; buildAgentRunsUrl() then translates
// page state to the existing API params — limit=page_size and
// offset=(page - 1) * page_size — while preserving every existing filter.

(function () {
  if (typeof window.readAgentRunPaginationFromUrl !== 'function' ||
      !appJsSandbox.location) {
    assert(false, 'app.js: readAgentRunPaginationFromUrl exposed + sandbox location present');
    return;
  }

  // Reset: no URL pagination → page 1, size 50 → limit=50&offset=0.
  appJsSandbox.location.search = '';
  window.readAgentRunPaginationFromUrl();
  window.setAgentRunFilters({});
  window.setDateRangeState({ preset: 'custom', customStartDate: '2026-06-01', customEndDate: '2026-06-30' });
  var url = window.buildAgentRunsUrl();
  assert(url.indexOf('limit=50') !== -1 && url.indexOf('offset=0') !== -1,
    'translate: default state requests limit=50 and offset=0');

  // URL ?page=2&page_size=100 on load → limit=100, offset=100.
  appJsSandbox.location.search = '?page=2&page_size=100';
  window.readAgentRunPaginationFromUrl();
  url = window.buildAgentRunsUrl();
  assert(url.indexOf('limit=100') !== -1 && url.indexOf('offset=100') !== -1,
    'translate: page 2 of 100 rows requests limit=100 and offset=100');

  // ?page=3&page_size=25 → offset=(3-1)*25=50.
  appJsSandbox.location.search = '?page=3&page_size=25';
  window.readAgentRunPaginationFromUrl();
  url = window.buildAgentRunsUrl();
  assert(url.indexOf('limit=25') !== -1 && url.indexOf('offset=50') !== -1,
    'translate: page 3 of 25 rows requests limit=25 and offset=50');

  // Invalid URL values fall back to page 1 / 50 rows.
  appJsSandbox.location.search = '?page=oops&page_size=0';
  window.readAgentRunPaginationFromUrl();
  url = window.buildAgentRunsUrl();
  assert(url.indexOf('limit=50') !== -1 && url.indexOf('offset=0') !== -1,
    'translate: invalid URL values fall back to limit=50 and offset=0');

  // Filters are preserved while paging: explicit dates, agent, and status
  // all ride along with the pagination params.
  appJsSandbox.location.search = '?page=2&page_size=100';
  window.readAgentRunPaginationFromUrl();
  window.setAgentRunFilters({ from_date: '2026-01-15T00:00:00Z', to_date: '2026-01-31T23:59:59Z', agent: 'bob', status: 'completed' });
  url = window.buildAgentRunsUrl();
  assert(url.indexOf('from_date=2026-01-15T00%3A00%3A00Z') !== -1 &&
         url.indexOf('to_date=2026-01-31T23%3A59%3A59Z') !== -1 &&
         url.indexOf('agent=bob') !== -1 && url.indexOf('status=completed') !== -1 &&
         url.indexOf('limit=100') !== -1 && url.indexOf('offset=100') !== -1,
    'translate: date/agent/status filters remain in the URL while paging');
})();

// ── Page changes persist via browser history ────────────────────────────
// setAgentRunPage() updates the closure page state and pushes the new URL
// through history.pushState, preserving any other query params already in
// the URL.  The URL update alone never changes Agent Runs row content —
// the table DOM is untouched and the fetch path is not invoked by it.

(function () {
  if (typeof window.setAgentRunPage !== 'function' || !appJsSandbox.history) {
    assert(false, 'app.js: setAgentRunPage exposed + sandbox history stub present');
    return;
  }

  historyCalls.length = 0;

  // Page change from the default state: URL gains page/page_size, closure
  // state updates so the next request uses the translated offset.
  appJsSandbox.location.search = '';
  appJsSandbox.location.pathname = '/index.html';
  window.readAgentRunPaginationFromUrl();
  window.setAgentRunFilters({ agent: 'bob', status: 'completed' });
  window.setAgentRunPage(3);
  assert(historyCalls.length === 1,
    'history: setAgentRunPage pushes exactly one history entry');
  assert(historyCalls[0] === '/index.html?page=3&page_size=50',
    'history: pushed URL carries page=3 and the current page_size');
  var url = window.buildAgentRunsUrl();
  assert(url.indexOf('limit=50') !== -1 && url.indexOf('offset=100') !== -1 &&
         url.indexOf('agent=bob') !== -1 && url.indexOf('status=completed') !== -1,
    'history: after the page change the next request uses offset=100 and keeps filters');

  // Unrelated query params already in the URL survive the page change.
  appJsSandbox.location.search = '?tab=agent-runs&page=1&page_size=50';
  window.readAgentRunPaginationFromUrl();
  historyCalls.length = 0;
  window.setAgentRunPage(2);
  assert(historyCalls.length === 1 && historyCalls[0] === '/index.html?tab=agent-runs&page=2&page_size=50',
    'history: unrelated URL params are preserved when the page changes');

  // The history update never touches the Agent Runs row content.
  assert(arTbodyEl.innerHTML === '',
    'history: the URL update leaves the Agent Runs table DOM untouched');

  // Invalid page values fall back to page 1 before persisting.
  appJsSandbox.location.search = '';
  window.readAgentRunPaginationFromUrl();
  historyCalls.length = 0;
  window.setAgentRunPage(-2);
  assert(historyCalls.length === 1 && historyCalls[0] === '/index.html?page=1&page_size=50',
    'history: an invalid page value falls back to page 1 in the pushed URL');
})();

// ── Agent Runs pagination controls (issue #427) ──────────────────────────
// One compact pagination control block below the Agent Runs panel: Previous,
// Next, and numbered page buttons with ellipses for large page counts.  The
// pure page-item window calculator drives the rendered items; the renderer
// derives the page count from the API response `total` and the current page
// size, disables Previous on page 1 / Next on the final page, marks the
// current page with aria-current="page", and wires clicks through
// setAgentRunPage + the shared fetch path (filters preserved).

console.log('\u25B6 Agent Runs pagination controls — page-item calculation (issue #427)');

// ── Pure calculation: page items (small / boundary / large) ─────────────
// computePageItems(currentPage, pageCount) returns the compact page-item
// window: all pages for small counts (<= 7), and for larger counts the
// first/last pages plus a window around the current page with ellipsis
// separators filling the gaps.  Zero/invalid page counts render no items.

(function () {
  if (typeof window.computePageItems !== 'function') {
    assert(false, 'app.js: computePageItems exposed on the window test seam');
    return;
  }

  function pageList(items) {
    return items.map(function (i) { return i.type === 'page' ? i.page : '\u2026'; }).join(',');
  }

  // No pages: zero, negative, and non-integer page counts render no items.
  assert(window.computePageItems(1, 0).length === 0,
    'items: zero page count renders no items');
  assert(window.computePageItems(1, -3).length === 0,
    'items: negative page count renders no items');
  assert(window.computePageItems(1, 2.5).length === 0,
    'items: non-integer page count renders no items');

  // Single page: only page 1, as a page item.
  var one = window.computePageItems(1, 1);
  assert(one.length === 1 && one[0].type === 'page' && one[0].page === 1,
    'items: single page renders only page 1');

  // Small counts (<= 7): every page, no ellipsis.
  assert(pageList(window.computePageItems(4, 7)) === '1,2,3,4,5,6,7',
    'items: 7 pages render every page with no ellipsis');
  assert(pageList(window.computePageItems(1, 2)) === '1,2',
    'items: 2 pages render pages 1 and 2');

  // Boundary (8 pages): first/last always visible, window around the
  // current page, a single ellipsis fills the gap.
  assert(pageList(window.computePageItems(1, 8)) === '1,2,\u2026,8',
    'items: page 1 of 8 renders 1,2,\u2026,8');
  assert(pageList(window.computePageItems(8, 8)) === '1,\u2026,7,8',
    'items: page 8 of 8 renders 1,\u2026,7,8');

  // Large counts: compact window with both ellipses.
  assert(pageList(window.computePageItems(13, 25)) === '1,\u2026,12,13,14,\u2026,25',
    'items: page 13 of 25 renders 1,\u2026,12,13,14,\u2026,25');

  // Out-of-range current pages clamp into the valid window.
  assert(pageList(window.computePageItems(99, 25)) === '1,\u2026,24,25',
    'items: current page beyond the last page clamps to the final-page window');
  assert(pageList(window.computePageItems(-1, 25)) === '1,2,\u2026,25',
    'items: current page below 1 clamps to the first-page window');
})();

// ── Control render (issue #427) ─────────────────────────────────────────
// renderAgentRunPagination(data) derives the page count from the API
// response `total` and the current page size, renders Previous/Next plus
// the numbered page items into the container below the panel, and marks
// the current page with aria-current="page".

console.log('\u25B6 Agent Runs pagination controls — render (issue #427)');

(function () {
  if (typeof window.renderAgentRunPagination !== 'function') {
    assert(false, 'app.js: renderAgentRunPagination exposed on the window test seam');
    return;
  }

  // Reset pagination state: page 1 of 50 rows, dashboard range active.
  appJsSandbox.location.search = '';
  appJsSandbox.location.pathname = '/index.html';
  window.readAgentRunPaginationFromUrl();
  window.setAgentRunFilters({});
  window.setDateRangeState({ preset: 'this-month' });

  // 125 runs at 50/page → 3 pages.  Page 1 is current: Previous disabled,
  // numbered buttons 1/2/3, Next enabled pointing at page 2.
  window.renderAgentRunPagination({ items: [{}], total: 125, limit: 50, offset: 0 });
  var html = arPaginationEl.innerHTML;
  assert(/data-page="1"[^>]*aria-current="page"/.test(html),
    'render: the current page button carries aria-current="page"');
  assert(html.indexOf('aria-label="Page 1, current page"') !== -1,
    'render: the current page button carries a "current page" label');
  assert(/data-page="0"[^>]*disabled/.test(html) &&
         html.indexOf('aria-label="Previous page"') !== -1,
    'render: Previous is a labeled button, disabled on page 1');
  assert(html.indexOf('data-page="2"') !== -1 &&
         html.indexOf('aria-label="Next page"') !== -1 &&
         /data-page="2"[^>]*aria-label="Next page"/.test(html),
    'render: Next is a labeled button pointing at page 2 (enabled)');
  assert(html.indexOf('data-page="1"') !== -1 && html.indexOf('data-page="2"') !== -1 &&
         html.indexOf('data-page="3"') !== -1,
    'render: numbered page buttons 1, 2, and 3 are present');
  assert(html.indexOf('pagination-ellipsis') === -1,
    'render: small page counts render no ellipsis');

  // Zero runs: no pages → the control renders nothing.
  window.renderAgentRunPagination({ items: [], total: 0, limit: 50, offset: 0 });
  assert(arPaginationEl.innerHTML === '',
    'render: zero runs hides the pagination control');

  // Large counts: 1250 runs at 50/page → 25 pages; page 13 renders the
  // compact ellipsis presentation with first/last always present.
  window.setAgentRunPage(13);
  window.renderAgentRunPagination({ items: [{}], total: 1250, limit: 50, offset: 600 });
  html = arPaginationEl.innerHTML;
  assert(html.indexOf('class="pagination-ellipsis"') !== -1,
    'render: large page counts render ellipsis separators');
  assert(html.indexOf('data-page="1"') !== -1 && html.indexOf('data-page="25"') !== -1,
    'render: first and last page buttons are always present');
  assert(html.indexOf('aria-label="Page 13, current page"') !== -1,
    'render: page 13 renders as the current page');
})();

// ── Page selection wiring (issue #427) ──────────────────────────────────
// Clicking a page control updates the pagination state (setAgentRunPage →
// history) and re-fetches that server-side page through the shared fetch
// path, which preserves the active from_date/to_date/agent/status filters.
// The click handler's URL/history/fetch effects are all synchronous, and
// the post-fetch re-render behavior is covered by the direct render calls
// above — so this block stays synchronous (the issue #5 render block's
// deferred poll chain shares the fakes and must not interleave with it).

console.log('\u25B6 Agent Runs pagination controls — page selection (issue #427)');

(function () {
  if (typeof window.renderAgentRunPagination !== 'function') {
    assert(false, 'app.js: renderAgentRunPagination exposed on the window test seam');
    return;
  }

  // Reset: page 1 of 50 rows with explicit filters active.
  appJsSandbox.location.search = '';
  appJsSandbox.location.pathname = '/index.html';
  window.readAgentRunPaginationFromUrl();
  window.setAgentRunFilters({ from_date: '2026-01-15T00:00:00Z', agent: 'bob', status: 'completed' });
  window.setDateRangeState({ preset: 'this-month' });
  historyCalls.length = 0;

  var fetched = [];
  appJsSandbox.fetch = function (url) {
    fetched.push(url);
    return Promise.resolve({
      ok: true,
      json: function () { return Promise.resolve({ items: [], total: 125, limit: 50, offset: 0 }); }
    });
  };

  // 125 runs / 50 per page → 3 pages; page 1 is current.  Click page 2.
  window.renderAgentRunPagination({ items: [{}], total: 125, limit: 50, offset: 0 });
  var page2 = arPaginationEl.querySelectorAll('button')
    .filter(function (b) { return b.getAttribute('data-page') === '2'; })[0];
  assert(!!page2, 'click: the page-2 button is rendered');
  page2._handlers.click();

  // Synchronous effects: URL persists the new page, and the fetch fires
  // with the page-2 offset while preserving the active filters.
  assert(historyCalls.length === 1 && historyCalls[0] === '/index.html?page=2&page_size=50',
    'click: selecting page 2 persists page=2 into the URL');
  assert(fetched.length === 1 && fetched[0].indexOf('limit=50&offset=50') !== -1,
    'click: the page-2 fetch carries limit=50 and offset=50');
  assert(fetched[0].indexOf('agent=bob') !== -1 && fetched[0].indexOf('status=completed') !== -1 &&
         fetched[0].indexOf('from_date=2026-01-15T00%3A00%3A00Z') !== -1,
    'click: the page-2 fetch preserves date/agent/status filters');
  assert(window.buildAgentRunsUrl().indexOf('offset=50') !== -1,
    'click: subsequent requests use the selected page offset');

  // Re-render from the new page state and advance to the final page (3).
  window.renderAgentRunPagination({ items: [{}], total: 125, limit: 50, offset: 50 });
  assert(/data-page="2"[^>]*aria-current="page"/.test(arPaginationEl.innerHTML),
    'render: after selection, page 2 carries aria-current="page"');
  assert(/data-page="1"[^>]*aria-label="Previous page"/.test(arPaginationEl.innerHTML) &&
         arPaginationEl.innerHTML.indexOf('data-page="0"') === -1,
    'render: Previous is enabled on page 2');
  var page3 = arPaginationEl.querySelectorAll('button')
    .filter(function (b) { return b.getAttribute('data-page') === '3'; })[0];
  assert(!!page3, 'click: the page-3 button is rendered on page 2');
  historyCalls.length = 0;
  page3._handlers.click();
  assert(historyCalls.length === 1 && historyCalls[0] === '/index.html?page=3&page_size=50',
    'click: selecting page 3 persists page=3 into the URL');
  assert(fetched.length === 2 && fetched[1].indexOf('limit=50&offset=100') !== -1,
    'click: the page-3 fetch carries limit=50 and offset=100');

  // The final page renders with Next disabled; clicking it must not
  // navigate or refetch.
  window.renderAgentRunPagination({ items: [{}], total: 125, limit: 50, offset: 100 });
  assert(arPaginationEl.innerHTML.indexOf('data-page="4" disabled') !== -1,
    'render: Next is disabled on the final page (3 of 3)');
  assert(/data-page="2"[^>]*aria-label="Previous page"/.test(arPaginationEl.innerHTML),
    'render: Previous points at page 2 on the final page');
  var nextBtn = arPaginationEl.querySelectorAll('button')
    .filter(function (b) { return b.getAttribute('data-page') === '4'; })[0];
  assert(!!nextBtn && nextBtn.disabled === true,
    'render: the disabled Next button carries the disabled state');
  historyCalls.length = 0;
  nextBtn._handlers.click();
  assert(historyCalls.length === 0 && fetched.length === 2,
    'click: clicking the disabled Next does not navigate or refetch');

  // Previous is disabled on page 1; clicking it must not navigate either.
  appJsSandbox.location.search = '';
  window.readAgentRunPaginationFromUrl(); // back to page 1
  window.renderAgentRunPagination({ items: [{}], total: 125, limit: 50, offset: 0 });
  var prevBtn = arPaginationEl.querySelectorAll('button')
    .filter(function (b) { return b.getAttribute('data-page') === '0'; })[0];
  assert(!!prevBtn && prevBtn.disabled === true,
    'render: the disabled Previous button carries the disabled state');
  historyCalls.length = 0;
  prevBtn._handlers.click();
  assert(historyCalls.length === 0 && fetched.length === 2,
    'click: clicking the disabled Previous does not navigate or refetch');
})();

// ── Pagination control block markup (issue #427) ─────────────────────────
// Static verification against the real index.html (the repo's established
// substitute for browser-level checks): the control container lives BELOW
// the .panel-agent-runs panel — after its closing </section>, outside the
// panel box — and inside #tab-agent-runs.

console.log('\u25B6 index.html — Agent Runs pagination control block (issue #427)');

(function () {
  var html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

  var panelEnd = html.indexOf('</section>', html.indexOf('panel-agent-runs'));
  var pagIdx = html.indexOf('id="agent-runs-pagination"');
  assert(pagIdx !== -1, 'index.html: #agent-runs-pagination container exists');
  assert(panelEnd !== -1 && pagIdx > panelEnd,
    'index.html: pagination container sits after the panel section (outside the panel box)');
  var tabEnd = html.indexOf('<!-- #tab-agent-runs -->');
  assert(tabEnd !== -1 && pagIdx < tabEnd,
    'index.html: pagination container lives inside #tab-agent-runs, below the panel');
  assert(html.indexOf('aria-label="Agent Runs pages"') !== -1,
    'index.html: pagination container carries an accessible navigation label');
  // The nav owns its layout (.agent-runs-pagination) and must stay decoupled
  // from the filter-bar class it used to share (PR #431 review finding 5).
  var navClassMatch = html.match(/<nav\b[^>]*class="([^"]*)"/);
  assert(navClassMatch !== null && navClassMatch[1].indexOf('agent-runs-filters') === -1,
    'index.html: pagination nav is decoupled from .agent-runs-filters (own layout)');
})();

// ── Agent Runs page-size + filter reset (issue #428) ─────────────────────
// The page-size selector offers exactly 25/50/100 rows per page (default
// 50); unsupported values fall back to 50.  Changing the page size or
// applying filters resets to page 1 and REPLACES the URL state
// (history.replaceState) so adjustments do not add a history entry per
// change — explicit page navigation (setAgentRunPage, issue #426) keeps
// using pushState.

console.log('\u25B6 Agent Runs page-size + filter reset (issue #428)');

// ── Page-size validation: exactly 25/50/100, fallback 50 ────────────────
// parseAgentRunPageSize() accepts the selector's three choices; any other
// value (including malformed input) falls back to the default (50).

(function () {
  if (typeof window.parseAgentRunPageSize !== 'function') {
    assert(false, 'app.js: parseAgentRunPageSize exposed on the window test seam');
    return;
  }

  // The selector's choices pass through.
  assert(window.parseAgentRunPageSize('25') === 25,
    'size: 25 rows per page is accepted');
  assert(window.parseAgentRunPageSize('50') === 50,
    'size: 50 rows per page is accepted (default)');
  assert(window.parseAgentRunPageSize('100') === 100,
    'size: 100 rows per page is accepted');

  // Unsupported values fall back to the default.
  assert(window.parseAgentRunPageSize('30') === 50,
    'size: 30 falls back to 50 (only 25/50/100 are offered)');
  assert(window.parseAgentRunPageSize('200') === 50,
    'size: 200 falls back to 50');
  assert(window.parseAgentRunPageSize('abc') === 50,
    'size: non-numeric value falls back to 50');
  assert(window.parseAgentRunPageSize('0') === 50,
    'size: 0 falls back to 50');
  assert(window.parseAgentRunPageSize('') === 50,
    'size: empty value falls back to 50');

  // The URL parser clamps page_size through the same rule (a deep link to
  // an unsupported size still lands on a supported size).
  var p = window.parseAgentRunPagination('page=2&page_size=200');
  assert(p.page === 2 && p.pageSize === 50,
    'parse: unsupported page_size=200 in the URL falls back to 50 (page kept)');
})();

// ── Page-size changes reset to page 1 and REPLACE URL state ─────────────
// setAgentRunPageSize() validates the choice (25/50/100), updates the
// closure page size, resets the page to 1, and persists the new state
// through history.replaceState — NOT pushState, so adjusting the selector
// does not create a browser-history entry per change.  Filters ride along
// unchanged (they live in the request, not the URL, per issue #412).

(function () {
  if (typeof window.setAgentRunPageSize !== 'function' || !appJsSandbox.history) {
    assert(false, 'app.js: setAgentRunPageSize exposed + sandbox history stub present');
    return;
  }

  // Page 3 of 50 with active filters → switching to 100 rows per page
  // resets to page 1, replaces the URL, and keeps the filters.
  appJsSandbox.location.search = '?page=3&page_size=50';
  appJsSandbox.location.pathname = '/index.html';
  window.readAgentRunPaginationFromUrl();
  window.setAgentRunFilters({ agent: 'bob', status: 'completed' });
  historyCalls.length = 0;
  historyReplaceCalls.length = 0;
  window.setAgentRunPageSize(100);
  assert(historyReplaceCalls.length === 1 && historyCalls.length === 0,
    'size: page-size change REPLACES the URL state (no pushState history entry)');
  assert(historyReplaceCalls[0] === '/index.html?page=1&page_size=100',
    'size: replaced URL carries page=1 and the new page_size');
  var url = window.buildAgentRunsUrl();
  assert(url.indexOf('limit=100') !== -1 && url.indexOf('offset=0') !== -1,
    'size: after the size change the next request uses limit=100 and offset=0 (page 1)');
  assert(url.indexOf('agent=bob') !== -1 && url.indexOf('status=completed') !== -1,
    'size: filters remain in the request after the page-size reset');

  // Unsupported sizes fall back to 50 (and still replace, page 1).
  historyCalls.length = 0;
  historyReplaceCalls.length = 0;
  window.setAgentRunPageSize(200);
  assert(historyReplaceCalls.length === 1 &&
         historyReplaceCalls[0] === '/index.html?page=1&page_size=50',
    'size: an unsupported size falls back to page_size=50 in the replaced URL');
  url = window.buildAgentRunsUrl();
  assert(url.indexOf('limit=50') !== -1 && url.indexOf('offset=0') !== -1,
    'size: fallback size requests limit=50 and offset=0');
})();

// ── Filter applies reset to page 1 and REPLACE URL state ────────────────
// applyFilters() (wired to the Apply button) re-scopes the list to page 1
// whenever filters are applied or changed, REPLACING the URL state (no
// history entry per adjustment) while the chosen filter values ride along
// in the request.  The Clear path (clearArDateFilters) shares this via
// applyFilters.

(function () {
  if (typeof window.setupAgentRunEventHandlers !== 'function') {
    assert(false, 'app.js: setupAgentRunEventHandlers exposed on the window test seam');
    return;
  }

  // Fixture: the user is on page 3 of 50 rows with date/agent/status
  // filters typed into the filter bar, then hits Apply.
  appJsSandbox.location.search = '?page=3&page_size=50';
  appJsSandbox.location.pathname = '/index.html';
  window.readAgentRunPaginationFromUrl();
  window.setAgentRunFilters({});
  arFilterFromEl.value = '2026-07-01';
  arFilterToEl.value = '2026-07-31';
  arFilterAgentEl.value = 'bob';
  arFilterStatusEl.value = 'completed';
  window.setupAgentRunEventHandlers();

  var calls = [];
  appJsSandbox.fetch = function (url) {
    calls.push(url);
    return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ items: [] }); } });
  };
  historyCalls.length = 0;
  historyReplaceCalls.length = 0;

  arFilterApplyEl._handlers.click();

  assert(historyReplaceCalls.length === 1 && historyCalls.length === 0,
    'apply: applying filters REPLACES the URL state (no pushState history entry)');
  assert(historyReplaceCalls[0] === '/index.html?page=1&page_size=50',
    'apply: replaced URL carries page=1 and the current page_size');
  assert(calls.length === 1 && calls[0].indexOf('/api/v1/usage/agent-runs?') === 0,
    'apply: exactly one agent-runs fetch through the existing filter path');
  assert(calls[0].indexOf('offset=0') !== -1 && calls[0].indexOf('limit=50') !== -1,
    'apply: the re-fetch requests page 1 (offset=0) at the current page size');
  assert(calls[0].indexOf('from_date=2026-07-01T00%3A00%3A00Z') !== -1 &&
         calls[0].indexOf('to_date=2026-07-31T23%3A59%3A59Z') !== -1 &&
         calls[0].indexOf('agent=bob') !== -1 && calls[0].indexOf('status=completed') !== -1,
    'apply: date/agent/status filter values remain in the request after the reset');
})();

// ── Page-size selector markup (issue #428) ──────────────────────────────
// Static verification against the real index.html: the selector lives in
// the Agent Runs filter bar and offers exactly 25/50/100 rows per page
// with 50 as the default (selected) choice.

(function () {
  var html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  var filterBar = html.slice(html.indexOf('id="agent-runs-filters"'),
    html.indexOf('id="ar-filter-apply"'));
  var selectMatch = filterBar.match(/<select[^>]*id="ar-page-size"[^>]*>([\s\S]*?)<\/select>/);
  assert(selectMatch !== null,
    'index.html: #ar-page-size select exists inside the Agent Runs filter bar');
  var options = (selectMatch ? selectMatch[1].match(/<option[^>]*value="(\d+)"[^>]*>/g) : null) || [];
  assert(options.length === 3 &&
         options[0].indexOf('value="25"') !== -1 &&
         options[1].indexOf('value="50"') !== -1 &&
         options[2].indexOf('value="100"') !== -1,
    'index.html: page-size select offers exactly 25, 50, and 100');
  assert(filterBar.indexOf('<option value="50" selected>') !== -1,
    'index.html: 50 is the default (selected) page size');
})();

// ── Page-size selector wiring: change → page 1 refetch + replace ────────
// The selector's change handler validates the choice, resets to page 1
// (replaceState), and immediately re-fetches through the existing filter
// path — the table reflects the new limit without waiting for the next
// auto-refresh, and the current filter values ride along.

(function () {
  if (typeof window.setupAgentRunEventHandlers !== 'function') {
    assert(false, 'app.js: setupAgentRunEventHandlers exposed on the window test seam');
    return;
  }

  // Fixture: page 3 of 50 rows with an agent filter applied; the user
  // picks 25 rows per page from the selector.
  appJsSandbox.location.search = '?page=3&page_size=50';
  appJsSandbox.location.pathname = '/index.html';
  window.readAgentRunPaginationFromUrl();
  window.setAgentRunFilters({ agent: 'bob' });
  arPageSizeEl.value = '25';
  window.setupAgentRunEventHandlers();

  if (typeof arPageSizeEl._handlers.change !== 'function') {
    assert(false, 'app.js: page-size change handler wired by setupAgentRunEventHandlers');
    return;
  }

  var calls = [];
  appJsSandbox.fetch = function (url) {
    calls.push(url);
    return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ items: [] }); } });
  };
  historyCalls.length = 0;
  historyReplaceCalls.length = 0;

  arPageSizeEl._handlers.change();

  assert(historyReplaceCalls.length === 1 && historyCalls.length === 0,
    'selector: changing page size REPLACES the URL state (no pushState history entry)');
  assert(historyReplaceCalls[0] === '/index.html?page=1&page_size=25',
    'selector: replaced URL carries page=1 and the new page_size');
  assert(calls.length === 1 && calls[0].indexOf('/api/v1/usage/agent-runs?') === 0,
    'selector: exactly one agent-runs fetch triggered by the change');
  assert(calls[0].indexOf('limit=25') !== -1 && calls[0].indexOf('offset=0') !== -1,
    'selector: the re-fetch requests limit=25 and offset=0 (page 1)');
  assert(calls[0].indexOf('agent=bob') !== -1,
    'selector: the current agent filter rides along after the reset');
})();

// ── Deep-link page size syncs the page-size selector (PR #431 finding 1) ─
// A deep link such as ?page_size=100 drives the fetch (limit=100) but the
// #ar-page-size select kept showing "50" — the selector lied about the
// active page size.  readAgentRunPaginationFromUrl() now syncs the visible
// selector to the URL's effective page size as well.

console.log('\u25B6 Agent Runs deep-link page-size selector sync (PR #431)');

(function () {
  if (typeof window.readAgentRunPaginationFromUrl !== 'function' ||
      !appJsSandbox.location) {
    assert(false, 'app.js: readAgentRunPaginationFromUrl exposed + sandbox location present');
    return;
  }

  arPageSizeEl.value = '50';

  appJsSandbox.location.search = '?page=2&page_size=100';
  window.readAgentRunPaginationFromUrl();
  assert(arPageSizeEl.value === '100',
    'deep link: ?page_size=100 syncs the page-size selector to 100');

  appJsSandbox.location.search = '?page_size=25';
  window.readAgentRunPaginationFromUrl();
  assert(arPageSizeEl.value === '25',
    'deep link: ?page_size=25 syncs the page-size selector to 25');

  appJsSandbox.location.search = '';
  window.readAgentRunPaginationFromUrl();
  assert(arPageSizeEl.value === '50',
    'deep link: no page_size syncs the page-size selector to the default 50');
})();

// ── Pagination control styling (PR #431 finding 2) ───────────────────────
// The pagination <nav> now carries its own layout rules (independent of
// .agent-runs-filters) and visually distinguishes the current page and
// ellipses via the dedicated classes renderAgentRunPagination() emits.

console.log('\u25B6 style.css — Agent Runs pagination control rules (PR #431)');

(function () {
  var css = fs.readFileSync(path.join(__dirname, '..', 'style.css'), 'utf8');
  var live = css.replace(/\/\*[\s\S]*?\*\//g, ''); // comment-stripped: assert on real rules only

  assert(live.indexOf('.agent-runs-pagination') !== -1,
    'style.css: .agent-runs-pagination layout rule exists');
  assert(live.indexOf('.pagination-btn.pagination-current') !== -1,
    'style.css: .pagination-btn.pagination-current current-page rule exists');
  assert(live.indexOf('.pagination-ellipsis') !== -1,
    'style.css: .pagination-ellipsis rule exists');
})();

// ── Same-page clicks are a no-op (PR #431 finding 3) ─────────────────────
// Clicking the already-current page button must not push a duplicate history
// entry or refetch — the guard makes the click a no-op while keeping the
// button focusable (its aria-current="page" + "current page" label stay).

console.log('\u25B6 Agent Runs pagination controls — current-page click is a no-op (PR #431)');

(function () {
  if (typeof window.renderAgentRunPagination !== 'function') {
    assert(false, 'app.js: renderAgentRunPagination exposed on the window test seam');
    return;
  }

  appJsSandbox.location.search = '?page=2&page_size=50';
  appJsSandbox.location.pathname = '/index.html';
  window.readAgentRunPaginationFromUrl();
  window.setAgentRunFilters({});
  window.setDateRangeState({ preset: 'this-month' });
  historyCalls.length = 0;

  var fetched = [];
  appJsSandbox.fetch = function (url) {
    fetched.push(url);
    return Promise.resolve({
      ok: true,
      json: function () { return Promise.resolve({ items: [{}], total: 125, limit: 50, offset: 50 }); }
    });
  };

  window.renderAgentRunPagination({ items: [{}], total: 125, limit: 50, offset: 50 });
  var page2 = arPaginationEl.querySelectorAll('button')
    .filter(function (b) { return b.getAttribute('data-page') === '2'; })[0];
  assert(!!page2 && page2.disabled !== true,
    'current: the page-2 button is rendered and stays focusable (not disabled)');
  page2._handlers.click();
  assert(historyCalls.length === 0,
    'current: clicking the current page pushes no duplicate history entry');
  assert(fetched.length === 0,
    'current: clicking the current page triggers no redundant refetch');

  // Restore page state (page 1 / size 50) so the deferred issue #7 Clear
  // block's fetch .then — which runs after this synchronous block as a
  // microtask — still sees a page-1 fallback no-op and renders its expected
  // empty state instead of being diverted into a fallback refetch.
  appJsSandbox.location.search = '';
  window.readAgentRunPaginationFromUrl();
})();

// ── Back/Forward (popstate) re-sync (PR #431 finding 4) ──────────────────
// Back/Forward changes location.search without re-running the load-time URL
// read, so a popstate handler re-reads the URL and refetches only when the
// effective page or page size changed — never pushing/replacing history.

console.log('\u25B6 Agent Runs Back/Forward popstate re-sync (PR #431)');

// Deferred (60ms) so its fetch/render effects drain AFTER the earlier issue
// #7/#5 deferred async blocks that assert on the shared arTbodyEl empty-state
// markup — never clobbering their expected rows (same pattern as the issue
// #429 resilience chain below).
pendingAsyncBlocks++;
setTimeout(function () {
  if (typeof window.handleAgentRunPopstate !== 'function' ||
      !appJsSandbox.location) {
    assert(false, 'app.js: handleAgentRunPopstate exposed + sandbox location present');
    pendingAsyncBlocks--;
    return;
  }

  // Fixture: page 2 of 50 rows deep-linked; the handler with an unchanged
  // URL must be a no-op (no fetch, no history change).
  appJsSandbox.location.search = '?page=2&page_size=50';
  appJsSandbox.location.pathname = '/index.html';
  window.readAgentRunPaginationFromUrl();
  window.setAgentRunFilters({});
  window.setDateRangeState({ preset: 'this-month' });
  historyCalls.length = 0;
  historyReplaceCalls.length = 0;

  var fetched = [];
  appJsSandbox.fetch = function (url) {
    fetched.push(url);
    return Promise.resolve({
      ok: true,
      json: function () { return Promise.resolve({ items: [{}], total: 125, limit: 50, offset: 50 }); }
    });
  };

  window.handleAgentRunPopstate();
  assert(fetched.length === 0,
    'popstate: an unchanged URL triggers no fetch');
  assert(historyCalls.length === 0 && historyReplaceCalls.length === 0,
    'popstate: an unchanged URL pushes/replaces no history');

  // Back/Forward to page 3: exactly one fetch at offset=100, no history.
  appJsSandbox.location.search = '?page=3&page_size=50';
  window.handleAgentRunPopstate();
  assert(fetched.length === 1 && fetched[0].indexOf('limit=50&offset=100') !== -1,
    'popstate: navigating to page 3 refetches offset=100');
  assert(historyCalls.length === 0 && historyReplaceCalls.length === 0,
    'popstate: a Back/Forward page change pushes/replaces no history');

  // Page-size-only change: ?page=3&page_size=25 → refetch limit=25&offset=50,
  // and the selector syncs (Fix 1) through the popstate path.
  fetched.length = 0;
  appJsSandbox.location.search = '?page=3&page_size=25';
  window.handleAgentRunPopstate();
  assert(fetched.length === 1 && fetched[0].indexOf('limit=25&offset=50') !== -1,
    'popstate: a page-size-only change refetches limit=25 at the same page offset');
  assert(arPageSizeEl.value === '25',
    'popstate: the page-size selector syncs to 25 through the popstate path');
  assert(historyCalls.length === 0 && historyReplaceCalls.length === 0,
    'popstate: a page-size change pushes/replaces no history');
  pendingAsyncBlocks--;
}, 60);

// ── Agent Runs pagination resilience (issue #429) ────────────────────────
// The pagination flow must stay resilient during automatic refreshes, page
// loads, request failures, and changing result totals: the current page
// stays selected and refetches the same offset on refresh; rows and the
// control remain visible during loading and failures (existing stale/error
// treatment preserved); and when the fetched total no longer covers the
// current page, the UI moves to the nearest valid page, updates the URL via
// replaceState, and refetches it.  Empty results stay on page 1 with the
// control cleared and never loop.
//
// These blocks share the same fakes as every earlier block (fetch stub,
// table, pagination control, history, page state), so they are SERIALIZED
// as a completion chain that starts on a delayed timer (60ms) — after the
// client-cache and issue #7/#5 async chains have fully drained — and each
// block re-establishes its own fixture inside its callback before running
// the next, following the file's established deferred-async pattern.

// Shared row fixtures (same field shape as the issue #5 render block).
var arResilRowA = { id: 'res-a', title: 'Resilient alpha', currentStatus: 'running', model: 'gpt-4o', agent: 'alpha',
  todo_completed: 1, todo_total: 2, code_changes_total: 0, total_estimated_cost_usd: 0.1,
  total_input_tokens: 10, total_output_tokens: 5, total_cache_read_tokens: 0,
  total_cache_write_tokens: 0, child_run_count: 0, last_updated_at: '2026-07-01T10:00:00' };
var arResilRowB = { id: 'res-b', title: 'Resilient beta', currentStatus: 'completed', model: 'claude-sonnet', agent: 'beta',
  todo_completed: 2, todo_total: 2, code_changes_total: 3, total_estimated_cost_usd: 0.2,
  total_input_tokens: 20, total_output_tokens: 10, total_cache_read_tokens: 0,
  total_cache_write_tokens: 0, child_run_count: 1, last_updated_at: '2026-07-01T09:00:00' };
var arResilRowC = { id: 'res-c', title: 'Resilient gamma', currentStatus: 'running', model: 'gpt-4o', agent: 'gamma',
  todo_completed: 0, todo_total: 1, code_changes_total: 0, total_estimated_cost_usd: 0,
  total_input_tokens: 5, total_output_tokens: 2, total_cache_read_tokens: 0,
  total_cache_write_tokens: 0, child_run_count: 0, last_updated_at: '2026-07-01T08:00:00' };

// ── Pure calculation: nearest valid page from a fetched total ────────────
// nearestValidAgentRunPage(total, currentPage, pageSize) returns the page
// the UI must land on: the current page unchanged while it is still covered
// by the fetched total, otherwise the last valid page.  An empty result
// (total=0) resolves to page 1 — from page 1 it returns 1 unchanged so a
// refetch can never loop, and from a higher page it lands on page 1, never
// page 0.

console.log('\u25B6 Agent Runs pagination resilience — nearest-valid-page calculation (issue #429)');

(function () {
  if (typeof window.nearestValidAgentRunPage !== 'function') {
    assert(false, 'app.js: nearestValidAgentRunPage exposed on the window test seam');
    return;
  }

  // Current page still covered by the result total → unchanged.
  assert(window.nearestValidAgentRunPage(125, 3, 50) === 3,
    'valid: page 3 of 125 rows (3 pages) stays on page 3');
  assert(window.nearestValidAgentRunPage(125, 3, 25) === 3,
    'valid: page 3 of 125 rows at 25/page (5 pages) stays on page 3');
  assert(window.nearestValidAgentRunPage(50, 1, 50) === 1,
    'valid: page 1 of exactly one full page stays on page 1');

  // Total shrinks below the current page → nearest valid page.
  assert(window.nearestValidAgentRunPage(60, 3, 50) === 2,
    'shrink: 60 rows (2 pages) moves page 3 to page 2');
  assert(window.nearestValidAgentRunPage(200, 5, 50) === 4,
    'shrink: 200 rows (4 pages) moves page 5 to page 4');
  assert(window.nearestValidAgentRunPage(10, 5, 50) === 1,
    'shrink: 10 rows (1 page) moves page 5 to page 1');
  assert(window.nearestValidAgentRunPage(50, 2, 50) === 1,
    'shrink: exactly one full page (50 rows) moves page 2 to page 1');

  // Empty result: resolves to page 1, never page 0; page 1 stays put.
  assert(window.nearestValidAgentRunPage(0, 3, 50) === 1,
    'empty: total=0 moves page 3 to page 1 (never page 0)');
  assert(window.nearestValidAgentRunPage(0, 1, 50) === 1,
    'empty: total=0 on page 1 stays on page 1 (no navigation)');
  assert(window.nearestValidAgentRunPage(0, 0, 50) === 1,
    'guard: a current page below 1 clamps to page 1');
})();

// ── Refresh refetches the currently selected page and offset ─────────────
// The automatic-refresh path (and every refresh-style refetch) re-requests
// the selected page through buildAgentRunsUrl — page state translates to
// the same offset every cycle, so a refresh on page 3 refetches offset=100
// rather than resetting to page 1.

console.log('\u25B6 Agent Runs pagination resilience — refresh refetches the selected page (issue #429)');

function resilienceBlockRefresh(next) {
  // Fixture: the user is on page 3 of 50 rows (deep-link state).
  appJsSandbox.location.search = '?page=3&page_size=50';
  appJsSandbox.location.pathname = '/index.html';
  window.readAgentRunPaginationFromUrl();
  window.setAgentRunFilters({});

  var fetched = [];
  appJsSandbox.fetch = function (url) {
    fetched.push(url);
    return Promise.resolve({
      ok: true,
      json: function () { return Promise.resolve({ items: [{}], total: 125, limit: 50, offset: 100 }); }
    });
  };

  window.fetchAgentRunsAndRender();
  // The refresh-style refetch fires synchronously with the selected page's offset.
  assert(fetched.length === 1 && fetched[0].indexOf('/api/v1/usage/agent-runs?') === 0,
    'refresh: exactly one agent-runs refetch triggered');
  assert(fetched[0].indexOf('limit=50&offset=100') !== -1,
    'refresh: the refetch requests the currently selected page 3 (offset=100)');

  setTimeout(function () {
    assert(arPaginationEl.innerHTML.indexOf('aria-label="Page 3, current page"') !== -1,
      'refresh: the refetched page 3 renders as the current page');
    assert(window.buildAgentRunsUrl().indexOf('offset=100') !== -1,
      'refresh: subsequent requests keep the selected page offset');
    next();
  }, 0);
}

// ── Rows stay visible while a new page is loading ────────────────────────
// A page request marks the panel 'refreshing' (which renders) and only
// repaints the table once the response lands — so the previously displayed
// rows and the pagination control remain on screen during the in-flight
// request, with no empty-state flash.

console.log('\u25B6 Agent Runs pagination resilience — rows stay visible during loading (issue #429)');

function resilienceBlockLoading(next) {
  appJsSandbox.location.search = '?page=2&page_size=50';
  appJsSandbox.location.pathname = '/index.html';
  window.readAgentRunPaginationFromUrl();
  window.setAgentRunFilters({});

  // Prime the previously displayed page through the real fetch path: two
  // rows on page 2 of 125 (also resolves the agent-runs panel to 'ok').
  appJsSandbox.fetch = function () {
    return Promise.resolve({
      ok: true,
      json: function () { return Promise.resolve({ items: [arResilRowA, arResilRowB], total: 125, limit: 50, offset: 50 }); }
    });
  };
  window.fetchAgentRunsAndRender();
  setTimeout(function () {
    assert(arTbodyEl.innerHTML.indexOf('data-id="res-a"') !== -1,
      'loading: fixture rows rendered before the refetch');
    assert(arPaginationEl.innerHTML.indexOf('aria-label="Page 2, current page"') !== -1,
      'loading: pagination control rendered (page 2 current)');

    // A slow page request: the fetch stays pending while we assert.
    var resolveFetch = null;
    appJsSandbox.fetch = function () {
      return new Promise(function (resolve) { resolveFetch = resolve; });
    };
    window.fetchAgentRunsAndRender();

    // While the new page is loading: previous rows and the control remain.
    assert(arTbodyEl.innerHTML.indexOf('data-id="res-a"') !== -1 && arTbodyEl.innerHTML.indexOf('data-id="res-b"') !== -1,
      'loading: previously displayed rows remain visible while the page request is in flight');
    assert(arPaginationEl.innerHTML.indexOf('aria-label="Page 2, current page"') !== -1,
      'loading: the pagination control remains visible with the current page while loading');
    assert(arTbodyEl.innerHTML.indexOf('No agent runs') === -1,
      'loading: no empty-state flash while the page request is in flight');

    // Now the page lands: the new rows replace the previous ones.
    resolveFetch({
      ok: true,
      json: function () { return Promise.resolve({ items: [arResilRowC], total: 125, limit: 50, offset: 50 }); }
    });
    setTimeout(function () {
      assert(arTbodyEl.innerHTML.indexOf('data-id="res-c"') !== -1 && arTbodyEl.innerHTML.indexOf('data-id="res-a"') === -1,
        'loading: the newly loaded page replaces the previous rows');
      next();
    }, 0);
  }, 0);
}

// ── Page-request failure keeps previous rows + error treatment ───────────
// A failed page request resolves the panel to 'stale' with the previous
// updatedAt: the table keeps its previously displayed rows, the pagination
// control keeps its last-known page info (both gated by shouldRenderPanel),
// and the freshness label swaps to the existing "Showing previous data"
// treatment — nothing is cleared or replaced.

console.log('\u25B6 Agent Runs pagination resilience — page-request failure keeps rows + error treatment (issue #429)');

function resilienceBlockFailure(next) {
  // Freshness-label fake: applyPanelFreshness looks the span up at render
  // time (document.getElementById), so registering it here is enough for
  // the stale "Showing previous data" label to be observable.
  var freshnessEl = makeFakeElement('freshness-agent-runs');
  elementRegistry['freshness-agent-runs'] = freshnessEl;

  appJsSandbox.location.search = '?page=2&page_size=50';
  appJsSandbox.location.pathname = '/index.html';
  window.readAgentRunPaginationFromUrl();
  window.setAgentRunFilters({});

  // Establish the previously displayed page through a successful fetch.
  appJsSandbox.fetch = function () {
    return Promise.resolve({
      ok: true,
      json: function () { return Promise.resolve({ items: [arResilRowA, arResilRowB], total: 125, limit: 50, offset: 50 }); }
    });
  };
  window.fetchAgentRunsAndRender();
  setTimeout(function () {
    assert(arTbodyEl.innerHTML.indexOf('data-id="res-a"') !== -1,
      'failure: previous page rendered before the failing request');

    // Now the page request fails.  The rejection is intentional: suppress
    // the expected console.error for this block and restore it after
    // (Nit 3 pattern).
    var savedConsoleError = appJsSandbox.console.error;
    appJsSandbox.console.error = function () {};
    appJsSandbox.fetch = function () {
      return Promise.reject(new Error('network down'));
    };
    window.fetchAgentRunsAndRender();
    setTimeout(function () {
      assert(arTbodyEl.innerHTML.indexOf('data-id="res-a"') !== -1 && arTbodyEl.innerHTML.indexOf('data-id="res-b"') !== -1,
        'failure: previously displayed rows are preserved after the failed page request');
      assert(arTbodyEl.innerHTML.indexOf('No agent runs') === -1,
        'failure: no empty-state replacement after the failed page request');
      assert(arPaginationEl.innerHTML.indexOf('aria-label="Page 2, current page"') !== -1,
        'failure: the pagination control keeps the last-known page info');
      assert(freshnessEl.textContent === 'Showing previous data',
        'failure: the "Showing previous data" stale label is preserved (existing error treatment)');
      appJsSandbox.console.error = savedConsoleError;
      next();
    }, 0);
  }, 0);
}

// ── Nearest-valid-page fallback when the total shrinks ───────────────────
// When the fetched total implies fewer pages than the current page (the
// result set shrank), the fallback hook moves the closure state to the
// nearest valid page, REPLACES the URL (no history entry), and refetches
// the corrected page through the shared path — the UI never sits on an
// empty offset.

console.log('\u25B6 Agent Runs pagination resilience — nearest-valid-page fallback (issue #429)');

function resilienceBlockFallback(next) {
  appJsSandbox.location.search = '?page=3&page_size=50';
  appJsSandbox.location.pathname = '/index.html';
  window.readAgentRunPaginationFromUrl();
  window.setAgentRunFilters({});

  // Prime through the real fetch path: page 3 of 125 runs (3 pages)
  // renders and the agent-runs panel resolves to 'ok'.
  appJsSandbox.fetch = function () {
    return Promise.resolve({
      ok: true,
      json: function () { return Promise.resolve({ items: [{}], total: 125, limit: 50, offset: 100 }); }
    });
  };
  window.fetchAgentRunsAndRender();
  setTimeout(function () {
    var page3 = arPaginationEl.querySelectorAll('button')
      .filter(function (b) { return b.getAttribute('data-page') === '3'; })[0];
    assert(!!page3, 'fallback: the page-3 button is rendered on page 3 of 125');

    // The result set shrank to 60 runs (2 pages): every subsequent fetch
    // reports the new total, whichever offset was requested.
    var fetchCount = 0;
    var fetched = [];
    appJsSandbox.fetch = function (url) {
      fetched.push(url);
      fetchCount++;
      return Promise.resolve({
        ok: true,
        json: function () {
          return Promise.resolve({
            items: [{}],
            total: 60,
            limit: 50,
            offset: fetchCount === 1 ? 100 : 50
          });
        }
      });
    };
    historyCalls.length = 0;
    historyReplaceCalls.length = 0;
    // PR #431 review (finding 3): clicking the current page is now a no-op,
    // so the shrink-triggering refetch is driven through the shared refresh
    // path (the same path auto-refresh uses) instead of a current-page click.
    window.fetchAgentRunsAndRender();

    // Synchronous effects: the refetch requests the stale page 3 (offset=100)
    // without pushing a history entry.
    assert(fetched.length === 1 && fetched[0].indexOf('limit=50&offset=100') !== -1,
      'fallback: the first fetch requests the stale page 3 (offset=100)');
    assert(historyCalls.length === 0,
      'fallback: the current-page refetch pushes no history entry');

    setTimeout(function () {
      // The stale page's response (total=60 → 2 pages) triggered the
      // fallback: URL replaced with the nearest valid page and refetched.
      assert(historyReplaceCalls.length === 1 && historyReplaceCalls[0] === '/index.html?page=2&page_size=50',
        'fallback: the URL is updated to the nearest valid page via replaceState');
      assert(historyCalls.length === 0,
        'fallback: the fallback adds no new browser-history entry');
      assert(fetched.length === 2 && fetched[1].indexOf('limit=50&offset=50') !== -1,
        'fallback: the nearest valid page (2, offset=50) is refetched');
      assert(arPaginationEl.innerHTML.indexOf('aria-label="Page 2, current page"') !== -1,
        'fallback: the control renders the nearest valid page as current');
      assert(window.buildAgentRunsUrl().indexOf('offset=50') !== -1,
        'fallback: subsequent requests use the corrected page offset');
      next();
    }, 0);
  }, 0);
}

// ── Empty-result guard ───────────────────────────────────────────────────
// No matching runs (total=0) must stay correct: on page 1 the guard makes
// no navigation at all (no refetch, no URL change — the control clears and
// the empty state renders); from a higher page it falls back to page 1
// exactly once (URL replaced with page=1 — never page 0 — and one refetch),
// which terminates because page 1 is always valid.

console.log('\u25B6 Agent Runs pagination resilience — empty-result guard (issue #429)');

function resilienceBlockEmpty(next) {
  // Case 1: already on page 1 when the result set is empty.
  appJsSandbox.location.search = '?page=1&page_size=50';
  appJsSandbox.location.pathname = '/index.html';
  window.readAgentRunPaginationFromUrl();
  window.setAgentRunFilters({});
  historyCalls.length = 0;
  historyReplaceCalls.length = 0;

  var fetched = [];
  appJsSandbox.fetch = function (url) {
    fetched.push(url);
    return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ items: [], total: 0, limit: 50, offset: 0 }); } });
  };
  window.fetchAgentRunsAndRender();
  assert(fetched.length === 1,
    'empty: page 1 fetch fires once');
  setTimeout(function () {
    assert(fetched.length === 1,
      'empty: total=0 on page 1 triggers no refetch (guard prevents a navigation loop)');
    assert(historyReplaceCalls.length === 0 && historyCalls.length === 0,
      'empty: total=0 on page 1 changes no URL state');
    assert(arPaginationEl.innerHTML === '',
      'empty: the pagination control clears when no runs match');
    assert(arTbodyEl.innerHTML.indexOf('No agent runs') !== -1,
      'empty: the empty-state message renders on page 1');

    // Case 2: on page 3 when the result set becomes empty → fall back to
    // page 1 exactly once (URL replaced, one refetch, control cleared).
    appJsSandbox.location.search = '?page=3&page_size=50';
    window.readAgentRunPaginationFromUrl();
    historyCalls.length = 0;
    historyReplaceCalls.length = 0;
    fetched.length = 0;
    arTbodyEl.innerHTML = '';
    window.renderAgentRunPagination({ items: [{}], total: 125, limit: 50, offset: 100 }); // 3 pages visible
    var page3 = arPaginationEl.querySelectorAll('button')
      .filter(function (b) { return b.getAttribute('data-page') === '3'; })[0];
    assert(!!page3, 'empty: the page-3 button is rendered');
    // PR #431 review (finding 3): the current-page click is a no-op, so the
    // empty-result fallback is driven through the shared refresh path.
    window.fetchAgentRunsAndRender();
    setTimeout(function () {
      assert(historyReplaceCalls.length === 1 &&
             historyReplaceCalls[0].indexOf('page=1&page_size=50') !== -1,
        'empty: total=0 from page 3 replaces the URL with page=1 (never page 0)');
      assert(fetched.length === 2,
        'empty: total=0 from page 3 refetches exactly once (page 1) — no loop');
      assert(fetched[1].indexOf('limit=50&offset=0') !== -1,
        'empty: the refetch requests page 1 (offset=0)');
      assert(arPaginationEl.innerHTML === '',
        'empty: the control clears after the empty-result fallback');
      assert(arTbodyEl.innerHTML.indexOf('No agent runs') !== -1,
        'empty: the empty-state message renders after the fallback');
      // Restore a benign default fetch stub for any later background work.
      appJsSandbox.fetch = function () {
        return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ items: [] }); } });
      };
      next();
    }, 0);
  }, 0);
}

// The five resilience blocks share the same fakes (fetch stub, table,
// pagination control, history, page state), so they run SERIALIZED — each
// block calls the next on completion instead of scheduling in parallel —
// following the file's deferred-async pattern.  The chain starts on a
// delayed timer so every earlier async block (client-cache, issue #7/#5
// render chains) has fully drained.
pendingAsyncBlocks++;
setTimeout(function () {
  resilienceBlockRefresh(function () {
    resilienceBlockLoading(function () {
      resilienceBlockFailure(function () {
        resilienceBlockFallback(function () {
          resilienceBlockEmpty(function () {
            pendingAsyncBlocks--;
          });
        });
      });
    });
  });
}, 60);

// ── Records view ordering: source-created, not ingest time (issue #401) ──
// The Records table presents "most recent" as most recently created at the
// source, so the /api/v1/usage/records fetch must request
// sort_by=source_created_at (the backend default) — never sort_by=ingested_at.
// The URL is built inline in fetchAll(), so this pins the production source
// (same readFileSync pattern used for index.html assertions above) rather
// than duplicating the URL builder.
console.log('\u25B6 Records view — sort_by=source_created_at, never ingested_at (issue #401)');

(function () {
  var appJsSource = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
  assert(appJsSource.indexOf('sort_by=source_created_at') !== -1,
    'records fetch requests sort_by=source_created_at (most recent = source-created message time, issue #401)');
  assert(appJsSource.indexOf('sort_by=ingested_at') === -1,
    'records fetch no longer requests sort_by=ingested_at (ingest time is not "most recent", issue #401)');
})();

// ── Agent Usage panel — group_by=agent fetch wiring (issue #438) ────────
// The Agent Usage panel reads GET /api/v1/usage/aggregates?group_by=agent in
// the same parallel refresh cycle as the other aggregate panels, sharing the
// dashboard date range (aggStart/aggEnd) and the per-cycle fetchErrors /
// panelStates handling.  The URL is built inline in fetchAll(), so this pins
// the production source (same readFileSync pattern used for the issue #401
// records-URL assertions).
console.log('\u25B6 Agent Usage — group_by=agent aggregate fetch wiring (issue #438)');

(function () {
  var appJsSource = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
  assert(appJsSource.indexOf('group_by=agent') !== -1,
    'app.js: the parallel refresh cycle issues a group_by=agent aggregates request');
  assert(appJsSource.indexOf("'&end_date=' + aggEnd + '&group_by=agent'") !== -1,
    'app.js: the group_by=agent fetch shares the dashboard date range (aggStart/aggEnd)');
  assert(appJsSource.indexOf('results.aggByAgent') !== -1,
    'app.js: the group_by=agent response is retained on the refresh results');
  assert(appJsSource.indexOf('fetchErrors.aggByAgent') !== -1,
    'app.js: the group_by=agent endpoint feeds the per-cycle fetchErrors tracking');
  assert(/'agent-usage':\s*\['aggByAgent'\]/.test(appJsSource),
    'app.js: the Agent Usage panel resolves stale from the aggByAgent endpoint (panelStates)');
})();

// ── Agent Usage panel — dynamic agent rows (issues #438/#439) ───────────
// buildAgentUsageRows derives one row per observed agent from the
// group_by=agent aggregates response: the agent identity (with the
// 'unknown' fallback for rows without a recorded agent) and the full
// Token Breakdown contract fields — the four independent counters
// (input/output/cacheRead/cacheWrite), the full total (total = input +
// output + cache read + cache write, NOT Active Tokens), the estimated
// cost, and the request count.  Rows order by total token usage
// descending, agent name ascending as the tie-breaker (the Agent Usage
// contract from CONTEXT.md).  Exercised through the vm-sandbox window seam
// against the REAL production helper.
console.log('\u25B6 Agent Usage — dynamic agent rows + ordering (issues #438/#439)');

(function () {
  if (typeof window.buildAgentUsageRows !== 'function') {
    assert(false, 'app.js: buildAgentUsageRows exposed on the window test seam');
    return;
  }

  // One row per observed agent; tokens = the FULL total per the Token
  // Breakdown contract (input + output + cache read + cache write).  These
  // fixtures carry no cache fields, so the totals equal Active Tokens.
  var rows = window.buildAgentUsageRows([
    { agent: 'bob',    total_input_tokens: 1000, total_output_tokens: 500 },
    { agent: 'alice',  total_input_tokens: 3000, total_output_tokens: 2000 },
    { agent: 'carol',  total_input_tokens: 100,  total_output_tokens: 50 }
  ]);
  assert(rows.length === 3, 'one row per observed agent (3 agents \u2192 3 rows)');
  assert(rows[0].agent === 'alice' && rows[0].tokens === 5000,
    'alice: tokens = input + output (5000) \u2014 highest total first');
  assert(rows[1].agent === 'bob' && rows[1].tokens === 1500,
    'bob: 1500 total tokens, second by total');
  assert(rows[2].agent === 'carol' && rows[2].tokens === 150,
    'carol: 150 total tokens, last by total');

  // Issue #439: each row carries the four independent counters, the full
  // total (Token Breakdown contract), the estimated cost, and the request
  // count (record_count — each usage_events row is one request).
  var enriched = window.buildAgentUsageRows([
    { agent: 'alice', total_input_tokens: 3000, total_output_tokens: 2000,
      total_cache_read_tokens: 1000, total_cache_write_tokens: 500,
      total_estimated_cost_usd: 1.25, record_count: 42 }
  ]);
  assert(enriched.length === 1 && enriched[0].agent === 'alice',
    'enriched row derives from the group_by=agent aggregate row');
  assert(enriched[0].input === 3000 && enriched[0].output === 2000 &&
         enriched[0].cacheRead === 1000 && enriched[0].cacheWrite === 500,
    'row carries the four independent counters (input/output/cacheRead/cacheWrite)');
  assert(enriched[0].tokens === 6500,
    'tokens = full total: input + output + cache read + cache write (6500)');
  assert(enriched[0].cost === 1.25 && enriched[0].requests === 42,
    'row carries estimated cost (1.25) and request count (42, from record_count)');

  // Issue #439: the sort key is the FULL total, not Active Tokens — cache
  // activity counts toward ordering, so an agent with large cache usage
  // outranks one with more active tokens but no cache.
  var cacheHeavy = window.buildAgentUsageRows([
    { agent: 'bob',   total_input_tokens: 1000, total_output_tokens: 500,
      total_cache_read_tokens: 0,    total_cache_write_tokens: 0 },
    { agent: 'alice', total_input_tokens: 500,  total_output_tokens: 250,
      total_cache_read_tokens: 5000, total_cache_write_tokens: 0 }
  ]);
  assert(cacheHeavy.length === 2 && cacheHeavy[0].agent === 'alice' && cacheHeavy[0].tokens === 5750,
    'sort key is the full total: alice (5750 incl. cache read) outranks bob (1500)');
  assert(cacheHeavy[1].agent === 'bob' && cacheHeavy[1].tokens === 1500,
    'bob: 1500 full total, second by total');

  // Tie-breaker: equal totals order by agent name ascending
  var tie = window.buildAgentUsageRows([
    { agent: 'zeta',  total_input_tokens: 100, total_output_tokens: 0 },
    { agent: 'alpha', total_input_tokens: 100, total_output_tokens: 0 }
  ]);
  assert(tie.length === 2 && tie[0].agent === 'alpha' && tie[1].agent === 'zeta',
    'equal totals: agent name ascending breaks the tie (alpha before zeta)');

  // Rows without an agent identity display as 'unknown'
  var unknown = window.buildAgentUsageRows([
    { agent: null, total_input_tokens: 10, total_output_tokens: 5 },
    { agent: '',   total_input_tokens: 20, total_output_tokens: 5 },
    { total_input_tokens: 30, total_output_tokens: 5 }
  ]);
  assert(unknown.length === 3 && unknown.every(function (r) { return r.agent === 'unknown'; }),
    'missing/null/empty agent identity falls back to \'unknown\'');

  // Empty input \u2192 empty row list
  assert(window.buildAgentUsageRows([]).length === 0, 'empty response \u2192 no rows');
  assert(window.buildAgentUsageRows(null).length === 0, 'null response \u2192 no rows');
})();

// ── Agent Usage panel — rendering + markup (issues #438/#439) ───────────
// renderAgentUsageTable paints one row per observed agent (name + compact
// Token Breakdown via the shared fmtTokenBreakdownCompact + Est. Cost via
// fmtCost + Requests) into the #agent-usage-tbody element, with the
// 'unknown' fallback for rows without an agent identity and the exact
// "No agent usage available" empty state.  The panel sits in the
// bottom-left area of the Overview grid (.col-left) using the existing
// glass-panel styling; the model-based Agents & LLMs In Use panel is left
// untouched.
console.log('\u25B6 Agent Usage — panel rendering + markup (issues #438/#439)');

(function () {
  if (typeof window.renderAgentUsageTable !== 'function') {
    assert(false, 'app.js: renderAgentUsageTable exposed on the window test seam');
    return;
  }

  // One row per agent: name + compact Token Breakdown + cost + requests
  window.renderAgentUsageTable({
    aggByAgent: [
      { agent: 'bob',   total_input_tokens: 1000, total_output_tokens: 500 },
      { agent: 'alice', total_input_tokens: 3000, total_output_tokens: 2000 }
    ]
  });
  assert(agentUsageTbodyEl.innerHTML.indexOf('alice') !== -1 &&
         agentUsageTbodyEl.innerHTML.indexOf('bob') !== -1,
    'render: one row per observed agent (alice + bob both rendered)');
  assert(agentUsageTbodyEl.innerHTML.indexOf('5.0K') !== -1 &&
         agentUsageTbodyEl.innerHTML.indexOf('1.5K') !== -1,
    'render: rows show the full token total (compact-formatted)');
  assert(agentUsageTbodyEl.innerHTML.indexOf('alice') < agentUsageTbodyEl.innerHTML.indexOf('bob'),
    'render: rows paint in the derived order (alice first — higher total)');

  // Issue #439: the token cell delegates to the shared
  // fmtTokenBreakdownCompact — the exact formatter output (two-line +
  // conditional cache line) appears in the row.
  window.renderAgentUsageTable({
    aggByAgent: [
      { agent: 'carol', total_input_tokens: 38800, total_output_tokens: 5200,
        total_cache_read_tokens: 23400, total_cache_write_tokens: 0,
        total_estimated_cost_usd: 0.005, record_count: 7 }
    ]
  });
  assert(agentUsageTbodyEl.innerHTML.indexOf(
    fmtTokenBreakdownCompact(38800, 5200, 23400, 0)) !== -1,
    'render: token cell equals the shared fmtTokenBreakdownCompact output ({total} total / {input} in | {output} out / {cr} cache read)');
  assert(agentUsageTbodyEl.innerHTML.indexOf('$0.0050') !== -1,
    'render: Est. Cost cell formats via fmtCost ($0.0050 for 0.005)');
  assert(agentUsageTbodyEl.innerHTML.indexOf('>7<') !== -1,
    'render: Requests cell shows the record_count (7)');

  // Issue #439: the cache line is omitted when BOTH cache counters are zero
  window.renderAgentUsageTable({
    aggByAgent: [
      { agent: 'dave', total_input_tokens: 1000, total_output_tokens: 500,
        total_cache_read_tokens: 0, total_cache_write_tokens: 0,
        total_estimated_cost_usd: 0.01, record_count: 3 }
    ]
  });
  assert(agentUsageTbodyEl.innerHTML.indexOf(
    fmtTokenBreakdownCompact(1000, 500, 0, 0)) !== -1 &&
    agentUsageTbodyEl.innerHTML.indexOf('cache') === -1,
    'render: both cache counters zero \u2192 cache line omitted entirely');
  assert(agentUsageTbodyEl.innerHTML.indexOf('$0.01') !== -1 &&
         agentUsageTbodyEl.innerHTML.indexOf('>3<') !== -1,
    'render: cost + request cells render for a no-cache row');

  // Issue #439: combined cache line when both cache categories are nonzero
  window.renderAgentUsageTable({
    aggByAgent: [
      { agent: 'erin', total_input_tokens: 10000, total_output_tokens: 5000,
        total_cache_read_tokens: 23400, total_cache_write_tokens: 4200,
        total_estimated_cost_usd: 0.02, record_count: 11 }
    ]
  });
  assert(agentUsageTbodyEl.innerHTML.indexOf(
    fmtTokenBreakdownCompact(10000, 5000, 23400, 4200)) !== -1,
    'render: token cell shows the combined "{cr} cache read + {cw} cache write" line');

  // 'unknown' fallback renders for rows without an agent identity
  window.renderAgentUsageTable({ aggByAgent: [{ total_input_tokens: 10, total_output_tokens: 5 }] });
  assert(agentUsageTbodyEl.innerHTML.indexOf('unknown') !== -1 &&
         agentUsageTbodyEl.innerHTML.indexOf('15') !== -1,
    "render: a row without an agent identity displays as 'unknown' with its token total");

  // Empty / missing response \u2192 the exact #439 empty-state message,
  // spanning the enriched 4-column row (no crash)
  window.renderAgentUsageTable({ aggByAgent: [] });
  assert(agentUsageTbodyEl.innerHTML.indexOf('No agent usage available') !== -1,
    'render: empty response \u2192 "No agent usage available" empty-state row');
  assert(agentUsageTbodyEl.innerHTML.indexOf('colspan="4"') !== -1,
    'render: empty-state row spans the 4 enriched columns');
  window.renderAgentUsageTable({});
  assert(agentUsageTbodyEl.innerHTML.indexOf('No agent usage available') !== -1,
    'render: missing aggByAgent field \u2192 "No agent usage available" empty-state row (no crash)');

  // Markup: the panel lives in the bottom-left Overview column as a glass
  // panel; the existing Agents & LLMs In Use (model-based) panel is unchanged.
  var html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  var overviewHtml = html.slice(html.indexOf('id="tab-overview"'));
  var colLeft = overviewHtml.slice(0, overviewHtml.indexOf('class="col-right"'));
  assert(colLeft.indexOf('glass-panel panel-agent-usage') !== -1,
    'index.html: the Agent Usage panel is a glass panel in the Overview left column (bottom-left area)');
  assert(colLeft.indexOf('id="agent-usage-tbody"') !== -1,
    'index.html: the Agent Usage panel carries the #agent-usage-tbody rows container');
  assert(colLeft.indexOf('id="freshness-agent-usage"') !== -1,
    'index.html: the Agent Usage panel carries a freshness label span');
  // Issue #439: the Agent Usage table carries the enriched column set
  // (Agent, Token Breakdown, Est. Cost, Requests) and the loading row
  // spans all four columns; the minimal #438 header is gone.
  var agentUsagePanelHtml = colLeft.slice(colLeft.indexOf('panel-agent-usage'));
  assert(agentUsagePanelHtml.indexOf('<th>Agent</th>') !== -1 &&
         agentUsagePanelHtml.indexOf('<th>Token Breakdown</th>') !== -1 &&
         agentUsagePanelHtml.indexOf('<th>Est. Cost</th>') !== -1 &&
         agentUsagePanelHtml.indexOf('<th>Requests</th>') !== -1,
    'index.html: the Agent Usage header carries Agent / Token Breakdown / Est. Cost / Requests');
  assert(agentUsagePanelHtml.indexOf('<th>Active Tokens</th>') === -1,
    'index.html: the minimal #438 "Active Tokens" header is gone');
  assert(agentUsagePanelHtml.indexOf('<td colspan="4" class="empty-state">Loading') !== -1,
    'index.html: the Agent Usage loading row spans the 4 enriched columns');
  assert(html.indexOf('id="collectors-tbody"') < html.indexOf('panel-agent-usage'),
    'index.html: the Agent Usage panel sits below the Collectors panel (bottom-left area)');
  assert(html.indexOf('glass-panel panel-agents') !== -1 && html.indexOf('id="agents-tbody"') !== -1,
    'index.html: the existing Agents & LLMs In Use (model-based) panel markup is unchanged');

  var appJsSource = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
  assert(appJsSource.indexOf('&group_by=model') !== -1 &&
         appJsSource.indexOf('renderAgentsTable(data)') !== -1,
    'app.js: the model-based Agents & LLMs In Use panel fetch + render are untouched');
})();

// ── Agent Usage panel — resilience + responsive placement (issue #440) ──
// The Agent Usage panel is independently refreshable: a failed group_by=agent
// (aggByAgent) request marks ONLY this panel stale (via the PANEL_ENDPOINTS
// mapping), the panel keeps its last successful rows with the existing
// stale/error indicator, and its bottom-left Overview placement is backed by
// responsive CSS following the sibling table-panel conventions.  Exercised
// through the vm-sandbox window seam (resolvePanelStatuses /
// shouldRenderPanel / computePanelFreshness) plus the established static
// source/markup verification pattern for the render wiring and layout.
console.log('\u25B6 Agent Usage — panel status isolation on aggByAgent failure (issue #440)');

(function () {
  // aggByAgent failure stales ONLY the Agent Usage panel — every other
  // dashboard panel (including the model-based Agents & LLMs In Use panel)
  // stays usable.
  var agentFail = window.resolvePanelStatuses({ aggByAgent: 'boom' });
  assert(agentFail['agent-usage'] === 'stale',
    'aggByAgent failure: the Agent Usage panel resolves to stale (PANEL_ENDPOINTS entry)');
  ['kpi-tokens', 'kpi-cost', 'kpi-sessions', 'kpi-collectors', 'kpi-source-dbs',
   'model-mix', 'events', 'collector-dist', 'collectors', 'agents', 'agent-runs', 'client-project']
    .forEach(function (panelId) {
      assert(agentFail[panelId] === 'ok',
        'aggByAgent failure: unrelated panel "' + panelId + '" stays ok');
    });

  // No aggByAgent error \u2192 the panel is ok (freshness resolves normally)
  var allOk = window.resolvePanelStatuses({});
  assert(allOk['agent-usage'] === 'ok', 'no errors: the Agent Usage panel resolves to ok');

  // Other single-endpoint failures do NOT stale the Agent Usage panel
  assert(window.resolvePanelStatuses({ aggByModel: 'boom' })['agent-usage'] === 'ok' &&
         window.resolvePanelStatuses({ health: 'down' })['agent-usage'] === 'ok' &&
         window.resolvePanelStatuses({ agentRuns: 'boom' })['agent-usage'] === 'ok' &&
         window.resolvePanelStatuses({ aggClientProject: 'boom' })['agent-usage'] === 'ok',
    'model/health/agent-runs/client-project failures leave the Agent Usage panel ok');
})();

console.log('\u25B6 Agent Usage — last-successful-rows retention + stale indicator (issue #440)');

(function () {
  // A stale Agent Usage panel with previous data skips the re-render, so the
  // last successful rows stay on screen (shouldRenderPanel discipline).
  assert(window.shouldRenderPanel({ 'agent-usage': { status: 'stale', updatedAt: 500000 } }, 'agent-usage') === false,
    'stale Agent Usage panel with previous data \u2192 render skipped (last rows retained)');
  assert(window.shouldRenderPanel({ 'agent-usage': { status: 'ok', updatedAt: 500000 } }, 'agent-usage') === true,
    'ok Agent Usage panel still renders');
  assert(window.shouldRenderPanel({ 'agent-usage': { status: 'stale', updatedAt: null } }, 'agent-usage') === true,
    'stale Agent Usage panel with NO previous data renders (empty/error state shown)');

  // The panel title swaps in the existing "Showing previous data" warning.
  var now = 1000000;
  var f = window.computePanelFreshness({ 'agent-usage': { status: 'stale', updatedAt: 500000 } }, 'agent-usage', now);
  assert(f !== null && f.status === 'stale' && f.label === 'Showing previous data',
    'stale Agent Usage panel shows the "Showing previous data" freshness label');

  // Render wiring: the panel honors the retention guard and paints the
  // existing error indicator into its empty state on a failed fetch.
  var appJsSource = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
  var renderSrc = appJsSource.slice(appJsSource.indexOf('function renderAgentUsageTable'),
                                    appJsSource.indexOf('function renderAgentRunsTable'));
  assert(renderSrc.indexOf("applyPanelFreshness('agent-usage')") !== -1 &&
         renderSrc.indexOf("shouldRenderPanel(panelStates, 'agent-usage')") !== -1,
    'app.js: renderAgentUsageTable applies freshness and skips the re-render when stale');
  assert(renderSrc.indexOf("errorIndicator('aggByAgent')") !== -1,
    'app.js: renderAgentUsageTable shows the fetch-error indicator (aggByAgent)');
})();

console.log('\u25B6 Agent Usage — responsive placement CSS (issue #440)');

(function () {
  var css = fs.readFileSync(path.join(__dirname, '..', 'style.css'), 'utf8');
  var live = css.replace(/\/\*[\s\S]*?\*\//g, ''); // comment-stripped: assert on real rules only

  // The panel has a dedicated rule following the sibling panel conventions
  // (shared glass-panel/table styling plus panel-specific table proportions).
  assert(live.indexOf('.panel-agent-usage') !== -1,
    'style.css: .panel-agent-usage rules exist (base band)');

  // Base band (>1024px): fixed table layout so the four-column table
  // (Agent | Token Breakdown | Est. Cost | Requests) always fits the
  // panel's column width — the Agent identity column truncates long names
  // with an ellipsis and the numeric Est. Cost column right-aligns (the
  // .num/.dist-tokens numeric convention), so no horizontal overflow at any
  // viewport width.
  var auTableRule = live.match(/\.panel-agent-usage table\s*\{[^}]*\}/);
  assert(auTableRule !== null && auTableRule[0].indexOf('table-layout: fixed') !== -1,
    'style.css: .panel-agent-usage table uses fixed layout (columns fit the panel)');
  var auFirstRule = live.match(/\.panel-agent-usage (?:th|td):first-child\s*\{[^}]*\}/);
  assert(auFirstRule !== null && auFirstRule[0].indexOf('text-overflow: ellipsis') !== -1,
    'style.css: the Agent identity column truncates long names with an ellipsis');
  var auCostRule = live.match(/\.panel-agent-usage (?:th|td):nth-child\(3\)\s*\{[^}]*\}/);
  assert(auCostRule !== null && auCostRule[0].indexOf('text-align: right') !== -1,
    'style.css: the Est. Cost column is right-aligned (numeric convention)');

  // Tablet band (761–1024px): the content grid stays, so the base
  // fixed-layout rule carries the panel — no per-band override needed.
  // Phone band (≤760px): the grid collapses to one column and the panel goes
  // full-width; the Agent identity column is given an explicit larger share
  // (30%) so long names stay readable on narrow screens.
  var phoneBlock = css.slice(css.indexOf('@media (max-width: 760px)'),
                             css.lastIndexOf('@media (prefers-reduced-motion: reduce)'));
  assert(phoneBlock.indexOf('.panel-agent-usage') !== -1 &&
         phoneBlock.indexOf('width: 30%') !== -1,
    'style.css: the phone band gives the Agent identity column the larger share (.panel-agent-usage)');

  // Markup: the panel is the LAST panel in the Overview left column
  // (bottom-left placement), below the Collectors table, inside .col-left.
  var html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  var overviewHtml = html.slice(html.indexOf('id="tab-overview"'));
  var colLeft = overviewHtml.slice(0, overviewHtml.indexOf('class="col-right"'));
  assert(colLeft.indexOf('panel-agent-usage') !== -1 &&
         colLeft.indexOf('panel-agent-usage') < colLeft.indexOf('</div><!-- .col-left -->'),
    'index.html: the Agent Usage panel is the last panel in the Overview left column (bottom-left)');
  assert(colLeft.indexOf('panel-collectors') < colLeft.indexOf('panel-agent-usage'),
    'index.html: the Agent Usage panel sits below the Collectors panel');
})();

// ── AFK Outcomes view (issue #453) ──────────────────────────────────────
// Pure helpers for the first AFK-outcomes UI: outcome/run badge mapping
// (locked EngineeringOutcomeStatus + RunStatus vocabulary), confidence and
// evidence formatting, provisional-link predicate, canonical chain
// composition, and the render functions (string builders + tbody/detail-body
// writers exercised through the fakes registered above).

console.log('\u25B6 AFK Outcomes — outcome badge mapping (issue #453)');

(function () {
  assert(window.outcomeStatusBadgeClass('merged') === 'badge-merged', 'merged \u2192 badge-merged');
  assert(window.outcomeStatusBadgeClass('closed') === 'badge-closed', 'closed \u2192 badge-closed');
  assert(window.outcomeStatusBadgeClass('abandoned') === 'badge-abandoned', 'abandoned \u2192 badge-abandoned');
  assert(window.outcomeStatusBadgeClass('open') === 'badge-open', 'open \u2192 badge-open');
  assert(window.outcomeStatusBadgeClass(null) === 'badge-unknown', 'null \u2192 badge-unknown');
  assert(window.outcomeStatusBadgeClass('anything') === 'badge-unknown', 'unknown \u2192 badge-unknown');

  assert(window.outcomeStatusLabel('merged') === 'merged', 'label: merged verbatim');
  assert(window.outcomeStatusLabel('closed') === 'closed', 'label: closed verbatim');
  assert(window.outcomeStatusLabel('abandoned') === 'abandoned', 'label: abandoned verbatim');
  assert(window.outcomeStatusLabel('open') === 'still open', 'label: open \u2192 "still open" (issue\'s still_open)');
  assert(window.outcomeStatusLabel(null) === '--', 'label: null \u2192 --');
})();

console.log('\u25B6 AFK Outcomes — run status badge mapping (issue #453)');

(function () {
  assert(window.afkRunStatusBadgeClass('running') === 'badge-running', 'running \u2192 badge-running');
  assert(window.afkRunStatusBadgeClass('completed') === 'badge-completed', 'completed \u2192 badge-completed');
  assert(window.afkRunStatusBadgeClass('blocked') === 'badge-blocked', 'blocked \u2192 badge-blocked');
  assert(window.afkRunStatusBadgeClass('stale') === 'badge-stale', 'stale \u2192 badge-stale');
  assert(window.afkRunStatusBadgeClass('failed') === 'badge-failed', 'failed \u2192 badge-failed');
  assert(window.afkRunStatusBadgeClass('cancelled') === 'badge-unknown', 'cancelled \u2192 badge-unknown');
  assert(window.afkRunStatusBadgeClass('timed_out') === 'badge-stale', 'timed_out \u2192 badge-stale');
  assert(window.afkRunStatusBadgeClass('nonsense') === 'badge-unknown', 'unknown \u2192 badge-unknown');
})();

console.log('\u25B6 AFK Outcomes — confidence + evidence formatting (issue #453)');

(function () {
  assert(window.fmtConfidence(1.0) === '100%', '1.0 \u2192 100%');
  assert(window.fmtConfidence(0.1) === '10%', '0.1 \u2192 10%');
  assert(window.fmtConfidence(0.05) === '5%', '0.05 \u2192 5%');
  assert(window.fmtConfidence(0) === '0%', '0 \u2192 0%');
  assert(window.fmtConfidence('0.25') === '25%', '"0.25" \u2192 25%');
  assert(window.fmtConfidence(null) === '--', 'null \u2192 --');
  assert(window.fmtConfidence(NaN) === '--', 'NaN \u2192 --');

  assert(window.fmtEvidence(null) === '', 'evidence null \u2192 empty');
  assert(window.fmtEvidence([]) === '', 'evidence [] \u2192 empty');
  assert(window.fmtEvidence([{ kind: 'issue_reference', source_entity_id: 'change_request:442', detail: 'resolves #437' }]) ===
    'issue_reference \u2190 change_request:442 (resolves #437)',
    'single evidence item \u2192 kind \u2190 source (detail)');
  assert(window.fmtEvidence([
    { kind: 'issue_reference', source_entity_id: 'change_request:442', detail: 'resolves #437' },
    { kind: 'title_match', source_entity_id: 'change_request:442' }
  ]) === 'issue_reference \u2190 change_request:442 (resolves #437); title_match \u2190 change_request:442',
    'multiple evidence items joined by "; " (no detail \u2192 no parens)');
})();

console.log('\u25B6 AFK Outcomes — provisional/inferred link predicate (issue #453)');

(function () {
  assert(window.isProvisionalLink({ provisional: true }) === true, 'provisional:true \u2192 provisional');
  assert(window.isProvisionalLink({ inferred: true }) === true, 'inferred:true \u2192 provisional');
  assert(window.isProvisionalLink({ provisional: false, inferred: false }) === false, 'both false \u2192 not provisional');
  assert(window.isProvisionalLink({}) === false, 'empty link \u2192 not provisional');
  assert(window.isProvisionalLink(null) === false, 'null \u2192 not provisional');
})();

console.log('\u25B6 AFK Outcomes — canonical chain composition (issue #453)');

(function () {
  var detail = {
    issues: [{ entity_id: 'issue:437' }],
    run: { afk_run_id: 'run-1' },
    sessions: [{ session_id: 's1' }],
    agents: ['code-editor-senior'],
    usage: { active_tokens: 1 },
    change_requests: [{ entity_id: 'change_request:442' }],
    commits: [{ entity_id: 'commit:abc1234' }],
    reviews: [{ entity_id: 'review:1' }],
    merge_events: [{ entity_id: 'merge_event:442' }],
    outcome: { status: 'merged' }
  };
  var steps = window.buildAfkChain(detail);
  var keys = steps.map(function (s) { return s.key; });
  assert(keys.join(',') === 'issues,run,sessions,agents,usage,change_requests,commits,reviews,outcome',
    'canonical step order: issue \u2192 run \u2192 sessions \u2192 agents \u2192 tokens/cost \u2192 change_request \u2192 commits \u2192 review cycles \u2192 outcome');
  assert(steps[0].items[0].entity_id === 'issue:437', 'issues step carries the issue links');
  assert(steps[1].run.afk_run_id === 'run-1', 'run step carries the run aggregate');
  assert(steps[2].items[0].session_id === 's1', 'sessions step carries the session links');
  assert(steps[3].items[0] === 'code-editor-senior', 'agents step carries the agent identities');
  assert(steps[4].usage.active_tokens === 1, 'usage step carries the tokens/cost aggregate');
  assert(steps[5].items[0].entity_id === 'change_request:442', 'change_request step carries the change request');
  assert(steps[6].items[0].entity_id === 'commit:abc1234', 'commits step carries the commit links');
  assert(steps[7].items[0].entity_id === 'review:1', 'reviews step carries the review cycles');
  assert(steps[8].outcome.status === 'merged' && steps[8].mergeEvents[0].entity_id === 'merge_event:442',
    'outcome step carries the outcome + merge events');
  assert(window.buildAfkChain(null).length === 9, 'null detail \u2192 9 empty steps (no throw)');
})();

console.log('\u25B6 AFK Outcomes — entity/session link rendering (issue #453)');

(function () {
  var resolved = window.renderAfkEntityLink({
    entity_id: 'issue:437', entity_type: 'issue', role: 'resolved',
    correlation_method: 'issue_reference', correlation_confidence: 1.0,
    resolver_version: '1', provisional: false,
    evidence: [{ kind: 'issue_reference', source_entity_id: 'change_request:442', detail: 'resolves #437' }]
  });
  assert(resolved.indexOf('afk-provisional') === -1, 'resolved link has no provisional marker');
  assert(resolved.indexOf('badge-completed') !== -1, 'resolved role \u2192 badge-completed');
  assert(resolved.indexOf('issue_reference') !== -1 && resolved.indexOf('100%') !== -1 &&
         resolved.indexOf('resolver v1') !== -1,
    'provenance line carries method + confidence + resolver version');
  assert(resolved.indexOf('evidence:') !== -1 && resolved.indexOf('change_request:442') !== -1,
    'evidence line rendered');

  var provisional = window.renderAfkEntityLink({
    entity_id: 'issue:436', entity_type: 'issue', role: 'referenced',
    correlation_method: 'issue_reference', correlation_confidence: 0.1,
    resolver_version: '1', provisional: true, evidence: []
  });
  assert(provisional.indexOf('afk-provisional') !== -1 && provisional.indexOf('provisional') !== -1,
    'referenced (provisional) link is visibly marked');
  assert(provisional.indexOf('badge-stale') !== -1, 'referenced role \u2192 badge-stale');
  assert(provisional.indexOf('10%') !== -1, 'provisional link still shows its confidence');

  var session = window.renderAfkSessionLink({
    external_session_id: 'ses_01', agent: 'code-editor-senior', inferred: true,
    message_count: 42, total_input_tokens: 5000, total_output_tokens: 3000,
    total_cache_read_tokens: 1000, total_cache_write_tokens: 0,
    total_estimated_cost_usd: 1.2345
  });
  assert(session.indexOf('afk-provisional') !== -1 && session.indexOf('inferred') !== -1,
    'session attachment is visibly marked inferred');
  assert(session.indexOf('5000') !== -1 || session.indexOf('5.0K') !== -1,
    'session carries the compact Token Breakdown');
  assert(session.indexOf('cache read') !== -1, 'session token breakdown shows the cache line when cache_read > 0');
})();

console.log('\u25B6 AFK Outcomes — chain detail + runs-list rendering (issue #453)');

(function () {
  var canon = {
    run: { afk_run_id: '01KZX9M4G80000000000000000', provider: 'github', status: 'completed',
           title: 'Develop-Loop: Consolidated run', started_at: '2026-08-13T09:00:00Z',
           finished_at: '2026-08-13T10:10:29Z', outcome_status: 'merged' },
    outcome: { status: 'merged', change_request_ids: ['change_request:442'],
               resolved_issue_ids: ['issue:437', 'issue:438', 'issue:439', 'issue:440'],
               merge_event_id: 'merge_event:442', merged_at: '2026-08-13T10:10:29Z' },
    issues: [
      { entity_id: 'issue:437', entity_type: 'issue', external_id: '437', provider: 'github',
        repository: 'weiyentan/opencode-gateway', role: 'resolved', correlation_method: 'issue_reference',
        correlation_confidence: 1.0, evidence: [{ kind: 'issue_reference', source_entity_id: 'change_request:442', detail: 'resolves #437' }],
        resolver_version: '1', provisional: false },
      { entity_id: 'issue:436', entity_type: 'issue', external_id: '436', provider: 'github',
        repository: 'weiyentan/opencode-gateway', role: 'referenced', correlation_method: 'issue_reference',
        correlation_confidence: 0.1, evidence: [{ kind: 'issue_reference', source_entity_id: 'change_request:442', detail: 'mentioned #436' }],
        resolver_version: '1', provisional: true }
    ],
    change_requests: [
      { entity_id: 'change_request:442', entity_type: 'change_request', external_id: '442', provider: 'github',
        repository: 'weiyentan/opencode-gateway', role: 'resolved', correlation_method: 'issue_reference',
        correlation_confidence: 1.0, evidence: [{ kind: 'title_match', source_entity_id: 'change_request:442', detail: 'exact title' }],
        resolver_version: '1', provisional: false }
    ],
    reviews: [
      { entity_id: 'review:442', entity_type: 'review', external_id: '442', provider: 'github',
        repository: 'weiyentan/opencode-gateway', role: 'resolved', correlation_method: 'temporal_inference',
        correlation_confidence: 0.9, evidence: [], resolver_version: '1', provisional: false }
    ],
    commits: [
      { entity_id: 'commit:abc1234', entity_type: 'commit', external_id: 'abc1234', provider: 'github',
        repository: 'weiyentan/opencode-gateway', role: 'resolved', correlation_method: 'commit_issue_reference',
        correlation_confidence: 1.0, evidence: [], resolver_version: '1', provisional: false }
    ],
    merge_events: [
      { entity_id: 'merge_event:442', entity_type: 'merge_event', external_id: '442', provider: 'github',
        repository: 'weiyentan/opencode-gateway', role: 'resolved', correlation_method: 'issue_reference',
        correlation_confidence: 1.0, evidence: [], resolver_version: '1', provisional: false }
    ],
    sessions: [
      { session_id: '1f9c3a6e-0000-4000-8000-000000000001', external_session_id: 'ses_01J4T2P0000000000000000000',
        started_at: '2026-08-13T09:00:00Z', finished_at: '2026-08-13T10:10:29Z', inferred: true,
        agent: 'code-editor-senior', message_count: 42, total_input_tokens: 5000, total_output_tokens: 3000,
        total_cache_read_tokens: 1000, total_cache_write_tokens: 500, total_estimated_cost_usd: 1.2345 }
    ],
    agents: ['code-editor-senior'],
    usage: { active_tokens: 8000, input_tokens: 5000, output_tokens: 3000, cache_read_tokens: 1000,
             cache_write_tokens: 500, estimated_cost_usd: 1.2345, message_count: 42, session_count: 1 }
  };

  window.renderAfkRunDetail(canon);
  var html = afkDetailBodyEl.innerHTML;

  // Canonical order is preserved in the rendered chain.
  var order = ['issues', 'run', 'sessions', 'agents', 'usage', 'change_requests', 'commits', 'reviews', 'outcome'];
  var lastIdx = -1;
  var ordered = true;
  order.forEach(function (key) {
    var idx = html.indexOf('data-step="' + key + '"');
    if (idx === -1 || idx <= lastIdx) ordered = false;
    lastIdx = idx;
  });
  assert(ordered, 'rendered chain follows the canonical step order');

  assert(html.indexOf('badge-merged') !== -1 && html.indexOf('merged') !== -1,
    'outcome renders the merged status badge');
  assert(html.indexOf('provisional') !== -1 && html.indexOf('afk-provisional') !== -1,
    'provisional issue link (#436) is visibly marked');
  assert(html.indexOf('inferred') !== -1, 'inferred session attachment is visibly marked');
  assert(html.indexOf('100%') !== -1, 'confidence is visible on reconstructed links');
  assert(html.indexOf('issue_reference') !== -1 && html.indexOf('change_request:442') !== -1,
    'evidence source identifiers are visible');
  assert(html.indexOf('Active Tokens') !== -1, 'tokens/cost step shows the Active Tokens aggregate');
  assert(html.indexOf('data-step="commits"') !== -1 && html.indexOf('commit:abc1234') !== -1 &&
         html.indexOf('commit_issue_reference') !== -1,
    'commits step renders the commit links with correlation provenance');

  // Escaping: an entity id with HTML metacharacters must not inject markup.
  window.renderAfkRunDetail({
    run: { afk_run_id: 'r1', status: 'completed', outcome_status: 'open' },
    issues: [{ entity_id: '<script>alert(1)</script>', entity_type: 'issue', role: 'resolved',
               correlation_method: 'issue_reference', correlation_confidence: 1.0, evidence: [], resolver_version: '1', provisional: false }]
  });
  assert(afkDetailBodyEl.innerHTML.indexOf('<script>alert') === -1,
    'entity id is HTML-escaped (no markup injection)');

  // Runs list rendering.
  window.renderAfkOutcomesTable({
    items: [
      { afk_run_id: 'run-1', provider: 'github', status: 'completed', title: 'Consolidated run',
        outcome_status: 'merged', started_at: '2026-08-13T09:00:00Z', last_seen_at: '2026-08-13T10:10:29Z' },
      { afk_run_id: 'run-2', provider: 'gitlab', status: 'failed', title: null,
        outcome_status: 'abandoned', started_at: null, last_seen_at: null }
    ],
    total: 2
  });
  var runsHtml = afkRunsTbodyEl.innerHTML;
  assert(runsHtml.indexOf('afk-run-row') !== -1 && runsHtml.indexOf('data-id="run-1"') !== -1,
    'runs list renders clickable rows keyed by afk_run_id');
  assert(runsHtml.indexOf('badge-completed') !== -1 && runsHtml.indexOf('badge-merged') !== -1,
    'runs list renders Status (completed) + Outcome (merged) badges');
  assert(runsHtml.indexOf('badge-failed') !== -1 && runsHtml.indexOf('badge-abandoned') !== -1,
    'runs list renders the failed run + abandoned outcome distinctly');

  window.renderAfkOutcomesTable({ items: [] });
  assert(afkRunsTbodyEl.innerHTML.indexOf('No AFK runs') !== -1,
    'runs list renders the empty state');
})();

// A failed /afk-outcomes/runs fetch must mark ONLY the AFK Outcomes panel
// stale (via the PANEL_ENDPOINTS mapping + the afkRunsFetchError channel),
// so the panel retains its last successful rows with the stale/error
// indicator — matching the established convention for every other panel
// (acceptance criterion 4).  Mirrors the Agent Usage panel isolation test.
console.log('\u25B6 AFK Outcomes — panel status isolation on afkRuns failure (issue #453)');

(function () {
  var afkFail = window.resolvePanelStatuses({ afkRuns: 'boom' });
  assert(afkFail['afk-outcomes'] === 'stale',
    'afkRuns failure: the AFK Outcomes panel resolves to stale (PANEL_ENDPOINTS entry)');
  ['kpi-tokens', 'kpi-cost', 'kpi-sessions', 'kpi-collectors', 'kpi-source-dbs',
   'model-mix', 'events', 'collector-dist', 'collectors', 'agents', 'agent-usage', 'agent-runs', 'client-project']
    .forEach(function (panelId) {
      assert(afkFail[panelId] === 'ok',
        'afkRuns failure: unrelated panel "' + panelId + '" stays ok');
    });

  // No afkRuns error \u2192 the panel is ok (freshness resolves normally)
  var allOk = window.resolvePanelStatuses({});
  assert(allOk['afk-outcomes'] === 'ok', 'no errors: the AFK Outcomes panel resolves to ok');

  // Other single-endpoint failures do NOT stale the AFK Outcomes panel
  assert(window.resolvePanelStatuses({ aggByModel: 'boom' })['afk-outcomes'] === 'ok' &&
         window.resolvePanelStatuses({ health: 'down' })['afk-outcomes'] === 'ok' &&
         window.resolvePanelStatuses({ agentRuns: 'boom' })['afk-outcomes'] === 'ok' &&
         window.resolvePanelStatuses({ aggClientProject: 'boom' })['afk-outcomes'] === 'ok' &&
         window.resolvePanelStatuses({ aggByAgent: 'boom' })['afk-outcomes'] === 'ok',
    'model/health/agent-runs/client-project/agent failures leave the AFK Outcomes panel ok');
})();

console.log('\u25B6 AFK Outcomes — last-successful-rows retention + stale indicator (issue #453)');

(function () {
  // A stale AFK Outcomes panel with previous data skips the re-render, so the
  // last successful rows stay on screen (shouldRenderPanel discipline).
  assert(window.shouldRenderPanel({ 'afk-outcomes': { status: 'stale', updatedAt: 500000 } }, 'afk-outcomes') === false,
    'stale AFK Outcomes panel with previous data \u2192 render skipped (last rows retained)');
  assert(window.shouldRenderPanel({ 'afk-outcomes': { status: 'ok', updatedAt: 500000 } }, 'afk-outcomes') === true,
    'ok AFK Outcomes panel still renders');
  assert(window.shouldRenderPanel({ 'afk-outcomes': { status: 'stale', updatedAt: null } }, 'afk-outcomes') === true,
    'stale AFK Outcomes panel with NO previous data renders (empty/error state shown)');

  // The panel title swaps in the existing "Showing previous data" warning.
  var now = 1000000;
  var f = window.computePanelFreshness({ 'afk-outcomes': { status: 'stale', updatedAt: 500000 } }, 'afk-outcomes', now);
  assert(f !== null && f.status === 'stale' && f.label === 'Showing previous data',
    'stale AFK Outcomes panel shows the "Showing previous data" freshness label');

  // Render wiring: the panel honors the retention guard and paints the
  // existing error indicator into its empty state on a failed fetch.
  var appJsSource = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
  var renderSrc = appJsSource.slice(appJsSource.indexOf('function renderAfkOutcomesTable'),
                                    appJsSource.indexOf('function openAfkRunDetail'));
  assert(renderSrc.indexOf("applyPanelFreshness('afk-outcomes')") !== -1 &&
         renderSrc.indexOf("shouldRenderPanel(panelStates, 'afk-outcomes')") !== -1,
    'app.js: renderAfkOutcomesTable applies freshness and skips the re-render when stale');
  assert(renderSrc.indexOf('afkRunsFetchError') !== -1,
    'app.js: renderAfkOutcomesTable shows the fetch-error indicator (afkRunsFetchError)');

  // The critical wiring: resolvePanelStatesAfterFetch must merge the separate
  // afkRunsFetchError channel into the error map (exactly like agentRuns), so
  // a failed runs fetch resolves the panel to 'stale' instead of 'ok'.
  var resolveSrc = appJsSource.slice(appJsSource.indexOf('function resolvePanelStatesAfterFetch'),
                                     appJsSource.indexOf('function updateLastRefreshed'));
  assert(resolveSrc.indexOf('afkRuns: afkRunsFetchError') !== -1,
    'app.js: resolvePanelStatesAfterFetch merges afkRuns: afkRunsFetchError into the error map');
})();

console.log('\u25B6 AFK Outcomes — openAfkRunDetail fetch + 404 handling (issue #453)');

(function () {
  pendingAsyncBlocks++;
  appJsSandbox.fetch = function (url) {
    return Promise.resolve({
      ok: true,
      json: function () {
        return Promise.resolve({ status: 'ok', data: { run: { afk_run_id: 'run-1', status: 'completed', outcome_status: 'merged' } } });
      }
    });
  };
  window.openAfkRunDetail('run-1').then(function () {
    assert(afkDetailOverlayEl.classList.contains('visible'), 'openAfkRunDetail shows the overlay');
    assert(afkDetailBodyEl.innerHTML.indexOf('afk-chain') !== -1, 'openAfkRunDetail renders the chain');

    // 404 → distinct "not found" empty state (no unhandled rejection).
    appJsSandbox.fetch = function () {
      return Promise.resolve({ ok: false, status: 404 });
    };
    pendingAsyncBlocks++;
    // The 404 is intentional: the production openAfkRunDetail catch emits an
    // EXPECTED console.error — suppress it for this block and restore it once
    // the assertions run (mirrors the stale-while-failure Nit 3 convention).
    var savedConsoleError = appJsSandbox.console.error;
    appJsSandbox.console.error = function () {};
    window.openAfkRunDetail('missing').then(function () {
      assert(afkDetailBodyEl.innerHTML.indexOf('AFK run not found') !== -1,
        '404 renders the distinct "not found" empty state');
      appJsSandbox.console.error = savedConsoleError; // restore the real console.error
      // Restore a benign default fetch stub for later blocks.
      appJsSandbox.fetch = function () {
        return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } });
      };
      pendingAsyncBlocks--;
    });
    pendingAsyncBlocks--;
  });
})();

// ── AFK Outcomes — session drill-down (issue #473) ─────────────────────
// The AFK chain's session attachments become actionable: a session with a
// resolvable internal session_id opens the existing Agent Run detail overlay
// (openAgentRunDetail), while a session without one stays non-clickable but
// remains visibly inferred.  The pure target resolver, the render markers,
// the empty state, and the end-to-end click wiring are each exercised.

console.log('\u25B6 AFK Outcomes — session drill-down target resolution (issue #473)');

(function () {
  var sid = '1f9c3a6e-0000-4000-8000-000000000001';
  assert(window.resolveAfkSessionDrilldown({ session_id: sid, external_session_id: 'ses_01' }) === sid,
    'resolved session: returns the internal session_id');
  assert(window.resolveAfkSessionDrilldown({ external_session_id: 'ses_01', session_id: null }) === null,
    'unresolved session (null session_id): returns null');
  assert(window.resolveAfkSessionDrilldown({ external_session_id: 'ses_01' }) === null,
    'unresolved session (missing session_id): returns null');
  assert(window.resolveAfkSessionDrilldown({}) === null, 'empty session: returns null');
  assert(window.resolveAfkSessionDrilldown(null) === null, 'null session: returns null');
})();

console.log('\u25B6 AFK Outcomes — session link drill-down rendering (issue #473)');

(function () {
  var resolved = window.renderAfkSessionLink({
    session_id: '1f9c3a6e-0000-4000-8000-000000000001', external_session_id: 'ses_01',
    agent: 'code-editor-senior', inferred: true, message_count: 42,
    total_input_tokens: 5000, total_output_tokens: 3000,
    total_cache_read_tokens: 1000, total_cache_write_tokens: 0, total_estimated_cost_usd: 1.2345
  });
  assert(resolved.indexOf('afk-session-clickable') !== -1, 'resolved session is clickable');
  assert(resolved.indexOf('data-session-id="1f9c3a6e-0000-4000-8000-000000000001"') !== -1,
    'resolved session carries data-session-id (internal id)');
  assert(resolved.indexOf('open run') !== -1, 'resolved session shows the open-run affordance');
  assert(resolved.indexOf('afk-provisional') !== -1 && resolved.indexOf('inferred') !== -1,
    'resolved session is still visibly marked inferred');

  var unresolved = window.renderAfkSessionLink({
    external_session_id: 'ses_02', agent: 'code-editor-senior', inferred: true,
    message_count: 0, total_input_tokens: 0, total_output_tokens: 0,
    total_cache_read_tokens: 0, total_cache_write_tokens: 0, total_estimated_cost_usd: 0
  });
  assert(unresolved.indexOf('afk-session-clickable') === -1, 'unresolved session is NOT clickable');
  assert(unresolved.indexOf('data-session-id=') === -1, 'unresolved session carries no data-session-id');
  assert(unresolved.indexOf('open run') === -1, 'unresolved session shows no open-run affordance');
  assert(unresolved.indexOf('afk-provisional') !== -1 && unresolved.indexOf('inferred') !== -1,
    'unresolved session is still visibly marked inferred');
})();

console.log('\u25B6 AFK Outcomes — empty session state (issue #473)');

(function () {
  window.renderAfkRunDetail({ run: { afk_run_id: 'r1', status: 'completed', outcome_status: 'open' } });
  assert(afkDetailBodyEl.innerHTML.indexOf('No sessions linked') !== -1,
    'no sessions \u2192 "No sessions linked" empty state');
})();

console.log('\u25B6 AFK Outcomes — session drill-down click wiring (issue #473)');

(function () {
  // Parse the rendered session links so a click can be driven through the
  // production wireAfkSessionDrilldown handler (same pattern as the
  // agent-runs pagination fake).
  var clickableLinks = [];
  afkDetailBodyEl.querySelectorAll = function (selector) {
    if (selector !== '.afk-session-clickable') return [];
    clickableLinks = [];
    var re = /data-session-id="([^"]+)"/g;
    var m;
    while ((m = re.exec(this.innerHTML)) !== null) {
      var el = makeFakeElement('afk-session-link');
      el.setAttribute('data-session-id', m[1]);
      clickableLinks.push(el);
    }
    return clickableLinks;
  };

  var sid = '1f9c3a6e-0000-4000-8000-000000000001';
  window.renderAfkRunDetail({
    run: { afk_run_id: 'r1', status: 'completed', outcome_status: 'merged' },
    sessions: [
      { session_id: sid, external_session_id: 'ses_01', inferred: true },
      { external_session_id: 'ses_02', inferred: true } // unresolved \u2192 non-clickable
    ]
  });

  // Only the resolved session is wired as a clickable link.
  assert(clickableLinks.length === 1,
    'exactly one resolved session link is clickable (the unresolved one is not)');
  assert(clickableLinks[0].getAttribute('data-session-id') === sid,
    'the clickable link targets the internal session_id');

  // Simulate the click: openAgentRunDetail(sid) fetches the Agent Run detail.
  // The AFK chain overlay starts visible (as openAfkRunDetail left it); the
  // drill-down must close it so the Agent Run overlay is not stacked behind it.
  var wasVisible = afkDetailOverlayEl.classList.contains('visible');
  afkDetailOverlayEl.classList.add('visible');
  var fetched = [];
  appJsSandbox.fetch = function (url) {
    fetched.push(url);
    return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ status: 'ok', data: {} }); } });
  };
  clickableLinks[0]._handlers.click();
  assert(fetched.length === 1 &&
         fetched[0] === '/api/v1/usage/agent-runs/' + encodeURIComponent(sid),
    'clicking a resolved session opens the Agent Run detail for its internal id');
  assert(afkDetailOverlayEl.classList.contains('visible') === false,
    'clicking a resolved session closes the AFK chain overlay (removes visible)');

  // Restore the shared AFK overlay state so the deferred openAfkRunDetail
  // assertion (a microtask that runs after this synchronous block) still sees
  // the overlay as visible; restore a benign fetch + the generic
  // querySelectorAll for later blocks.
  if (wasVisible) afkDetailOverlayEl.classList.add('visible');
  afkDetailBodyEl.querySelectorAll = function () { return []; };
  appJsSandbox.fetch = function () {
    return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } });
  };
})();


// ════════════════════════════════════════════════════════════════════════════
// Issue #577: AFK Outcomes — VM-Sandbox Regression Coverage
// ════════════════════════════════════════════════════════════════════════════
// Comprehensive deterministic regression coverage for the repository-first
// AFK Outcomes flow using the VM-sandbox approach and GitHub/GitLab fixtures.
// No provider credentials, AWX, or network access required.
// ── Fixture loading ─────────────────────────────────────────────────────

var issue577FixturesDir = path.join(__dirname, '..', 'fixtures');

var issue577GithubRunDetail = JSON.parse(fs.readFileSync(
  path.join(issue577FixturesDir, 'github_afk_run_detail.json'), 'utf8'));
var issue577GitlabRunDetail = JSON.parse(fs.readFileSync(
  path.join(issue577FixturesDir, 'gitlab_afk_run_detail.json'), 'utf8'));
var issue577GithubAmbiguousDetail = JSON.parse(fs.readFileSync(
  path.join(issue577FixturesDir, 'github_ambiguous_detail.json'), 'utf8'));
var issue577GithubParkedDetail = JSON.parse(fs.readFileSync(
  path.join(issue577FixturesDir, 'github_parked_detail.json'), 'utf8'));
var issue577GitlabUnresolvedDetail = JSON.parse(fs.readFileSync(
  path.join(issue577FixturesDir, 'gitlab_unresolved_detail.json'), 'utf8'));

// ── 1. Repository summary tests ─────────────────────────────────────────

console.log('\u25B6 issue #577 \u2014 repository summary: mixed providers');

(function () {
  window.renderAfkOutcomesTable({
    items: [
      { afk_run_id: 'gh-1', provider: 'github', status: 'completed', title: 'GH run',
        outcome_status: 'merged', started_at: '2026-08-13T09:00:00Z', last_seen_at: '2026-08-13T10:00:00Z' },
      { afk_run_id: 'gl-1', provider: 'gitlab', status: 'completed', title: 'GL run',
        outcome_status: 'merged', started_at: '2026-08-14T08:00:00Z', last_seen_at: '2026-08-14T09:00:00Z' }
    ],
    total: 2
  });
  var html = afkRunsTbodyEl.innerHTML;
  assert(html.indexOf('data-id="gh-1"') !== -1, 'mixed providers: GitHub run rendered');
  assert(html.indexOf('data-id="gl-1"') !== -1, 'mixed providers: GitLab run rendered');
  assert(html.indexOf('>github<') !== -1, 'mixed providers: GitHub label visible');
  assert(html.indexOf('>gitlab<') !== -1, 'mixed providers: GitLab label visible');
  assert(html.indexOf('badge-completed') !== -1, 'mixed providers: status badge rendered');
  assert(html.indexOf('badge-merged') !== -1, 'mixed providers: outcome badge rendered');
})();

console.log('\u25B6 issue #577 \u2014 repository summary: empty period');

(function () {
  window.renderAfkOutcomesTable({ items: [], total: 0 });
  assert(afkRunsTbodyEl.innerHTML.indexOf('No AFK runs') !== -1,
    'empty period: no-AFK-runs message rendered');
  assert(afkRunsTbodyEl.innerHTML.indexOf('colspan="6"') !== -1,
    'empty period: empty-state spans all 6 columns');
})();

console.log('\u25B6 issue #577 \u2014 repository summary: API error isolation');

(function () {
  var afkFail = window.resolvePanelStatuses({ afkRuns: 'boom' });
  assert(afkFail['afk-outcomes'] === 'stale',
    'API error: afkRuns failure stales AFK Outcomes panel');
  assert(window.shouldRenderPanel({ 'afk-outcomes': { status: 'stale', updatedAt: 500000 } }, 'afk-outcomes') === false,
    'API error: stale panel with previous data skips re-render');
  assert(window.shouldRenderPanel({ 'afk-outcomes': { status: 'stale', updatedAt: null } }, 'afk-outcomes') === true,
    'API error: stale panel without previous data renders');
  var f = window.computePanelFreshness({ 'afk-outcomes': { status: 'stale', updatedAt: 500000 } }, 'afk-outcomes', 1000000);
  assert(f !== null && f.status === 'stale' && f.label === 'Showing previous data',
    'API error: stale panel shows Showing previous data label');
  assert(window.resolvePanelStatuses({})['afk-outcomes'] === 'ok',
    'API error: no errors resolves panel to ok');
  assert(window.resolvePanelStatuses({ aggByModel: 'boom' })['afk-outcomes'] === 'ok',
    'API error: aggByModel failure leaves AFK panel ok');
  assert(window.resolvePanelStatuses({ health: 'down' })['afk-outcomes'] === 'ok',
    'API error: health failure leaves AFK panel ok');
})();

// ── 2. Change-request tests ─────────────────────────────────────────────

console.log('\u25B6 issue #577 \u2014 change-request: all-results mode');

(function () {
  window.renderAfkOutcomesTable({
    items: [
      { afk_run_id: 'cr-1', provider: 'github', status: 'completed', title: 'With CR',
        outcome_status: 'merged', started_at: '2026-08-13T09:00:00Z', last_seen_at: '2026-08-13T10:00:00Z' },
      { afk_run_id: 'cr-2', provider: 'github', status: 'running', title: 'No CR',
        outcome_status: 'open', started_at: '2026-08-13T11:00:00Z', last_seen_at: null }
    ],
    total: 2
  });
  var html = afkRunsTbodyEl.innerHTML;
  assert(html.indexOf('data-id="cr-1"') !== -1 && html.indexOf('data-id="cr-2"') !== -1,
    'all-results: both runs rendered');
  assert(html.indexOf('badge-completed') !== -1 && html.indexOf('badge-running') !== -1,
    'all-results: distinct status badges');
  assert(html.indexOf('badge-merged') !== -1 && html.indexOf('badge-open') !== -1,
    'all-results: distinct outcome badges');
})();

console.log('\u25B6 issue #577 \u2014 change-request: provider labels');

(function () {
  window.renderAfkOutcomesTable({
    items: [
      { afk_run_id: 'pl-1', provider: 'github', status: 'completed', title: 'GH PR',
        outcome_status: 'merged', started_at: null, last_seen_at: null },
      { afk_run_id: 'pl-2', provider: 'gitlab', status: 'completed', title: 'GL MR',
        outcome_status: 'merged', started_at: null, last_seen_at: null },
      { afk_run_id: 'pl-3', provider: null, status: 'completed', title: 'No provider',
        outcome_status: 'merged', started_at: null, last_seen_at: null }
    ],
    total: 3
  });
  var html = afkRunsTbodyEl.innerHTML;
  assert(html.indexOf('>github<') !== -1, 'provider labels: GitHub rendered');
  assert(html.indexOf('>gitlab<') !== -1, 'provider labels: GitLab rendered');
  assert(html.indexOf('data-label="Provider">--') !== -1, 'provider labels: null renders dash');
})();

console.log('\u25B6 issue #577 \u2014 change-request: selection fallback on missing title');

(function () {
  window.renderAfkOutcomesTable({
    items: [
      { afk_run_id: 'fb-1', provider: 'github', status: 'completed', title: null,
        outcome_status: 'merged', started_at: null, last_seen_at: null }
    ],
    total: 1
  });
  var html = afkRunsTbodyEl.innerHTML;
  assert(html.indexOf('fb-1') !== -1, 'selection fallback: shortUUID used when title is null');
})();

// ── 3. Provenance tests ─────────────────────────────────────────────────

console.log('\u25B6 issue #577 \u2014 provenance: GitHub fixture chain detail');

(function () {
  window.renderAfkRunDetail(issue577GithubRunDetail.data);
  var html = afkDetailBodyEl.innerHTML;
  assert(html.indexOf('afk-chain') !== -1, 'GitHub: chain div rendered');
  assert(html.indexOf('badge-completed') !== -1, 'GitHub: run status badge');
  assert(html.indexOf('badge-merged') !== -1, 'GitHub: outcome merged badge');
  assert(html.indexOf('100%') !== -1, 'GitHub: confidence visible');
  assert(html.indexOf('issue_reference') !== -1, 'GitHub: correlation method visible');
  assert(html.indexOf('change_request:501') !== -1, 'GitHub: change request entity id');
  assert(html.indexOf('commit:abc1234') !== -1, 'GitHub: commit entity id');
  assert(html.indexOf('review:501') !== -1, 'GitHub: review entity id');
  assert(html.indexOf('merge_event:501') !== -1, 'GitHub: merge event entity id');
  assert(html.indexOf('code-editor-senior') !== -1, 'GitHub: agent identity');
  assert(html.indexOf('Active Tokens') !== -1, 'GitHub: usage step Active Tokens');
  assert(html.indexOf('data-step="issues"') !== -1, 'GitHub: issues step');
  assert(html.indexOf('data-step="run"') !== -1, 'GitHub: run step');
  assert(html.indexOf('data-step="sessions"') !== -1, 'GitHub: sessions step');
  assert(html.indexOf('data-step="change_requests"') !== -1, 'GitHub: change_requests step');
  assert(html.indexOf('data-step="commits"') !== -1, 'GitHub: commits step');
  assert(html.indexOf('data-step="reviews"') !== -1, 'GitHub: reviews step');
  assert(html.indexOf('data-step="outcome"') !== -1, 'GitHub: outcome step');
})();

console.log('\u25B6 issue #577 \u2014 provenance: GitLab fixture chain detail');

(function () {
  window.renderAfkRunDetail(issue577GitlabRunDetail.data);
  var html = afkDetailBodyEl.innerHTML;
  assert(html.indexOf('afk-chain') !== -1, 'GitLab: chain div rendered');
  assert(html.indexOf('badge-completed') !== -1, 'GitLab: run status badge');
  assert(html.indexOf('badge-merged') !== -1, 'GitLab: outcome merged badge');
  assert(html.indexOf('change_request:601') !== -1, 'GitLab: change request entity id');
  assert(html.indexOf('commit:gl789abc') !== -1, 'GitLab: commit entity id');
  assert(html.indexOf('review:gl601') !== -1, 'GitLab: review entity id');
  assert(html.indexOf('merge_event:601') !== -1, 'GitLab: merge event entity id');
  assert(html.indexOf('resolves #501') !== -1, 'GitLab: evidence detail');
  assert(html.indexOf('Active Tokens') !== -1, 'GitLab: usage step Active Tokens');
  // entity_id format confirms correct GitLab fixture loaded
  assert(html.indexOf('change_request:601') !== -1, 'GitLab: entity id confirms GitLab fixture');
})();

console.log('\u25B6 issue #577 \u2014 provenance: cost + cache data');

(function () {
  window.renderAfkRunDetail(issue577GitlabRunDetail.data);
  var html = afkDetailBodyEl.innerHTML;
  assert(html.indexOf('estimated cost') !== -1 || html.indexOf('$') !== -1,
    'GitLab: estimated cost label visible');
  assert(html.indexOf('cache') !== -1, 'GitLab: cache data visible');
  window.renderAfkRunDetail(issue577GithubRunDetail.data);
  html = afkDetailBodyEl.innerHTML;
  assert(html.indexOf('estimated cost') !== -1 || html.indexOf('$') !== -1,
    'GitHub: estimated cost label visible');
  assert(html.indexOf('cache') !== -1, 'GitHub: cache data visible');
})();

console.log('\u25B6 issue #577 \u2014 provenance: status distinction (RunStatus vs OutcomeStatus)');

(function () {
  window.renderAfkRunDetail({
    run: { afk_run_id: 'sd-1', status: 'running', outcome_status: 'open' },
    outcome: { status: 'open' },
    issues: [], sessions: [], agents: [], usage: {},
    change_requests: [], commits: [], reviews: [], merge_events: []
  });
  var html = afkDetailBodyEl.innerHTML;
  assert(html.indexOf('badge-running') !== -1, 'status distinction: run status running badge');
  assert(html.indexOf('badge-open') !== -1, 'status distinction: outcome status open badge');
  assert(html.indexOf('badge-running') !== html.indexOf('badge-open'),
    'status distinction: run status and outcome status are distinct badges');
})();

console.log('\u25B6 issue #577 \u2014 provenance: missing optional data');

(function () {
  window.renderAfkRunDetail({
    run: { afk_run_id: 'mo-1', status: 'completed', outcome_status: 'open', title: null },
    outcome: { status: 'open', change_request_ids: [], resolved_issue_ids: [], merge_event_id: null, merged_at: null },
    issues: [], sessions: [], agents: [], usage: {},
    change_requests: [], commits: [], reviews: [], merge_events: []
  });
  var html = afkDetailBodyEl.innerHTML;
  assert(html.indexOf('No sessions linked') !== -1, 'missing data: empty sessions shows empty state');
  assert(html.indexOf('afk-chain') !== -1, 'missing data: chain still rendered');
  assert(html.indexOf('badge-completed') !== -1, 'missing data: run status still rendered');
  assert(html.indexOf('badge-open') !== -1, 'missing data: outcome status still rendered');
})();

// ── 4. Session tests (nesting, missing parents, unresolved, deep links) ─

console.log('\u25B6 issue #577 \u2014 session: root/child nesting');

(function () {
  window.renderAfkRunDetail({
    run: { afk_run_id: 'sn-1', status: 'completed', outcome_status: 'merged' },
    outcome: { status: 'merged' },
    sessions: [
      { session_id: '1f9c3a6e-0000-4000-8000-000000000001', external_session_id: 'ses-root',
        agent: 'coordinator', inferred: true, message_count: 10,
        total_input_tokens: 1000, total_output_tokens: 500,
        total_cache_read_tokens: 0, total_cache_write_tokens: 0, total_estimated_cost_usd: 0.1 },
      { session_id: '2f9c3a6e-0000-4000-8000-000000000002', external_session_id: 'ses-child',
        agent: 'code-editor', inferred: true, message_count: 5,
        total_input_tokens: 500, total_output_tokens: 200,
        total_cache_read_tokens: 0, total_cache_write_tokens: 0, total_estimated_cost_usd: 0.05 }
    ],
    issues: [], agents: ['coordinator', 'code-editor'], usage: { active_tokens: 2200 },
    change_requests: [], commits: [], reviews: [], merge_events: []
  });
  var html = afkDetailBodyEl.innerHTML;
  assert(html.indexOf('ses-root') !== -1, 'nesting: root session external id visible');
  assert(html.indexOf('ses-child') !== -1, 'nesting: child session external id visible');
  assert(html.indexOf('coordinator') !== -1, 'nesting: root session agent visible');
  assert(html.indexOf('code-editor') !== -1, 'nesting: child session agent visible');
  assert(html.indexOf('afk-session-clickable') !== -1, 'nesting: resolved sessions are clickable');
  assert(html.indexOf('data-session-id="1f9c3a6e-0000-4000-8000-000000000001"') !== -1,
    'nesting: root session carries data-session-id');
  assert(html.indexOf('data-session-id="2f9c3a6e-0000-4000-8000-000000000002"') !== -1,
    'nesting: child session carries data-session-id');
})();

console.log('\u25B6 issue #577 \u2014 session: missing parent (no session_id)');

(function () {
  window.renderAfkRunDetail({
    run: { afk_run_id: 'mp-1', status: 'completed', outcome_status: 'merged' },
    outcome: { status: 'merged' },
    sessions: [
      { external_session_id: 'ses-orphan', agent: 'orphan-agent', inferred: true,
        message_count: 3, total_input_tokens: 100, total_output_tokens: 50,
        total_cache_read_tokens: 0, total_cache_write_tokens: 0, total_estimated_cost_usd: 0.01 }
    ],
    issues: [], agents: ['orphan-agent'], usage: { active_tokens: 150 },
    change_requests: [], commits: [], reviews: [], merge_events: []
  });
  var html = afkDetailBodyEl.innerHTML;
  assert(html.indexOf('afk-session-clickable') === -1,
    'missing parent: unresolved session is NOT clickable');
  assert(html.indexOf('data-session-id=') === -1,
    'missing parent: no data-session-id attribute');
  assert(html.indexOf('ses-orphan') !== -1,
    'missing parent: external session id still visible');
  assert(html.indexOf('inferred') !== -1,
    'missing parent: still visibly marked inferred');
})();

console.log('\u25B6 issue #577 \u2014 session: unresolved session (missing session_id field)');

(function () {
  var result = window.resolveAfkSessionDrilldown({ external_session_id: 'ses-x', agent: 'test', inferred: true });
  assert(result === null, 'unresolved: resolveAfkSessionDrilldown returns null for missing session_id');
  result = window.resolveAfkSessionDrilldown({ external_session_id: 'ses-x', session_id: null, agent: 'test' });
  assert(result === null, 'unresolved: resolveAfkSessionDrilldown returns null for null session_id');
  result = window.resolveAfkSessionDrilldown({ external_session_id: 'ses-x',
    session_id: '1f9c3a6e-0000-4000-8000-000000000001', agent: 'test' });
  assert(result === '1f9c3a6e-0000-4000-8000-000000000001',
    'resolved: resolveAfkSessionDrilldown returns session_id');
})();

console.log('\u25B6 issue #577 \u2014 session: deep link to Agent Run detail');

(function () {
  // Render with a resolved session, then parse clickable links and click
  var clickableLinks = [];
  afkDetailBodyEl.querySelectorAll = function (selector) {
    if (selector !== '.afk-session-clickable') return [];
    clickableLinks = [];
    var re = /data-session-id="([^"]+)"/g;
    var m;
    while ((m = re.exec(this.innerHTML)) !== null) {
      var el = makeFakeElement('afk-session-link');
      el.setAttribute('data-session-id', m[1]);
      clickableLinks.push(el);
    }
    return clickableLinks;
  };

  window.renderAfkRunDetail({
    run: { afk_run_id: 'dl-1', status: 'completed', outcome_status: 'merged' },
    outcome: { status: 'merged' },
    sessions: [
      { session_id: '1f9c3a6e-0000-4000-8000-000000000001', external_session_id: 'ses-resolved',
        agent: 'test', inferred: false },
      { external_session_id: 'ses-unresolved', agent: 'test', inferred: true }
    ],
    issues: [], agents: ['test'], usage: { active_tokens: 0 },
    change_requests: [], commits: [], reviews: [], merge_events: []
  });

  assert(clickableLinks.length === 1, 'deep link: exactly one clickable session link');
  assert(clickableLinks[0].getAttribute('data-session-id') === '1f9c3a6e-0000-4000-8000-000000000001',
    'deep link: clickable link targets the resolved session_id');

  // Simulate click - should close AFK overlay and open Agent Run detail
  afkDetailOverlayEl.classList.add('visible');
  var fetched = [];
  appJsSandbox.fetch = function (url) { fetched.push(url); return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ status: 'ok', data: {} }); } }); };
  clickableLinks[0]._handlers.click();
  assert(fetched.length === 1, 'deep link: click triggers Agent Run detail fetch');
  assert(fetched[0].indexOf('/api/v1/usage/agent-runs/') !== -1, 'deep link: fetch targets agent-runs endpoint');
  assert(afkDetailOverlayEl.classList.contains('visible') === false,
    'deep link: AFK chain overlay closed after click');
  afkDetailBodyEl.querySelectorAll = function () { return []; };
  appJsSandbox.fetch = function () { return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } }); };
})();

// ── 5. Relationship tests (resolved, provisional, ambiguous, parked) ────

console.log('\u25B6 issue #577 \u2014 relationship: resolved links');

(function () {
  var link = window.renderAfkEntityLink({
    entity_id: 'issue:437', entity_type: 'issue', role: 'resolved',
    correlation_method: 'issue_reference', correlation_confidence: 1.0,
    resolver_version: '2', provisional: false,
    evidence: [{ kind: 'issue_reference', source_entity_id: 'change_request:501', detail: 'resolves #437' }]
  });
  assert(link.indexOf('afk-provisional') === -1, 'resolved: no provisional marker');
  assert(link.indexOf('badge-completed') !== -1, 'resolved: role badge-completed');
  assert(link.indexOf('100%') !== -1, 'resolved: confidence 100%');
  assert(link.indexOf('issue_reference') !== -1, 'resolved: correlation method visible');
  assert(link.indexOf('resolver v2') !== -1, 'resolved: resolver version visible');
  assert(link.indexOf('change_request:501') !== -1, 'resolved: evidence source visible');
})();

console.log('\u25B6 issue #577 \u2014 relationship: provisional links');

(function () {
  var link = window.renderAfkEntityLink({
    entity_id: 'issue:436', entity_type: 'issue', role: 'referenced',
    correlation_method: 'temporal_inference', correlation_confidence: 0.1,
    resolver_version: '2', provisional: true,
    evidence: [{ kind: 'temporal', source_entity_id: 'issue:436', detail: 'same time window' }]
  });
  assert(link.indexOf('afk-provisional') !== -1, 'provisional: visibly marked');
  assert(link.indexOf('provisional') !== -1, 'provisional: text "provisional" visible');
  assert(link.indexOf('10%') !== -1, 'provisional: confidence 10%');
  assert(link.indexOf('temporal_inference') !== -1, 'provisional: method visible');
})();

console.log('\u25B6 issue #577 \u2014 relationship: ambiguous fixture (GitHub)');

(function () {
  window.renderAfkRunDetail(issue577GithubAmbiguousDetail.data);
  var html = afkDetailBodyEl.innerHTML;
  assert(html.indexOf('badge-running') !== -1, 'ambiguous: run status running');
  assert(html.indexOf('badge-open') !== -1, 'ambiguous: outcome status open');
  assert(html.indexOf('issue:701') !== -1, 'ambiguous: referenced issue visible');
  assert(html.indexOf('issue:702') !== -1, 'ambiguous: noise issue visible');
  assert(html.indexOf('provisional') !== -1, 'ambiguous: provisional links visible');
  assert(html.indexOf('0%') !== -1, 'ambiguous: zero confidence for noise');
  assert(html.indexOf('10%') !== -1, 'ambiguous: low confidence for referenced');
  assert(html.indexOf('temporal_inference') !== -1, 'ambiguous: temporal method visible');
  assert(html.indexOf('afk-session-clickable') !== -1 || html.indexOf('ses_amb') !== -1,
    'ambiguous: sessions visible');
  // No resolved issue IDs in outcome (both are referenced/noise)
  assert(html.indexOf('No sessions linked') === -1, 'ambiguous: sessions exist');
})();

console.log('\u25B6 issue #577 \u2014 relationship: parked fixture (GitHub)');

(function () {
  window.renderAfkRunDetail(issue577GithubParkedDetail.data);
  var html = afkDetailBodyEl.innerHTML;
  assert(html.indexOf('badge-stale') !== -1, 'parked: run status stale');
  assert(html.indexOf('badge-open') !== -1, 'parked: outcome open');
  assert(html.indexOf('issue:901') !== -1, 'parked: issue entity visible');
  assert(html.indexOf('provisional') !== -1, 'parked: provisional links visible');
  assert(html.indexOf('change_request:901a') !== -1, 'parked: first CR visible');
  assert(html.indexOf('change_request:901b') !== -1, 'parked: second CR visible');
  assert(html.indexOf('branch_issue_reference') !== -1, 'parked: branch issue method visible');
  assert(html.indexOf('No sessions linked') === -1, 'parked: sessions exist');
})();

console.log('\u25B6 issue #577 \u2014 relationship: unresolved fixture (GitLab)');

(function () {
  window.renderAfkRunDetail(issue577GitlabUnresolvedDetail.data);
  var html = afkDetailBodyEl.innerHTML;
  assert(html.indexOf('badge-failed') !== -1, 'unresolved: run status failed');
  assert(html.indexOf('badge-abandoned') !== -1, 'unresolved: outcome abandoned');
  assert(html.indexOf('issue:801') !== -1, 'unresolved: noise issue visible');
  assert(html.indexOf('provisional') !== -1, 'unresolved: provisional links visible');
  assert(html.indexOf('temporal_inference') !== -1, 'unresolved: temporal method visible');
  assert(html.indexOf('No sessions linked') !== -1, 'unresolved: empty sessions state');
  assert(html.indexOf('issue:801') !== -1, 'unresolved: entity id confirms fixture');
})();

console.log('\u25B6 issue #577 \u2014 relationship: confidence/method/evidence/resolver_version');

(function () {
  // Verify all four provenance fields are rendered on each entity link
  window.renderAfkRunDetail(issue577GithubRunDetail.data);
  var html = afkDetailBodyEl.innerHTML;
  assert(html.indexOf('100%') !== -1, 'confidence: percentage visible on resolved link');
  assert(html.indexOf('issue_reference') !== -1, 'method: issue_reference visible');
  assert(html.indexOf('explicit_run_id') !== -1, 'method: explicit_run_id visible');
  assert(html.indexOf('branch_issue_reference') !== -1, 'method: branch_issue_reference visible');
  assert(html.indexOf('resolver v2') !== -1, 'resolver_version: v2 visible');
  assert(html.indexOf('evidence') !== -1, 'evidence: evidence block visible');
})();

// ── 6. Loading / empty / stale / partial / retry tests ──────────────────

console.log('\u25B6 issue #577 \u2014 loading state');

(function () {
  // Loading: openAfkRunDetail sets "Loading detail..." before fetch completes
  pendingAsyncBlocks++;
  var resolveFetch = null;
  appJsSandbox.fetch = function () {
    return new Promise(function (resolve) { resolveFetch = resolve; });
  };
  window.openAfkRunDetail('loading-test');
  // While loading: the overlay is visible and loading text is shown
  assert(afkDetailOverlayEl.classList.contains('visible'), 'loading: overlay visible');
  assert(afkDetailBodyEl.innerHTML.indexOf('Loading detail') !== -1, 'loading: loading text displayed');
  // Resolve the fetch
  resolveFetch({
    ok: true,
    json: function () { return Promise.resolve({ status: 'ok', data: { run: { afk_run_id: 'loading-test', status: 'completed', outcome_status: 'merged' } } }); }
  });
  pendingAsyncBlocks--;
})();

console.log('\u25B6 issue #577 \u2014 empty detail state');

(function () {
  window.renderAfkRunDetail(null);
  assert(afkDetailBodyEl.innerHTML.indexOf('No AFK outcome data available') !== -1,
    'empty detail: null detail shows no-data message');
  window.renderAfkRunDetail({});
  assert(afkDetailBodyEl.innerHTML.indexOf('No AFK outcome data available') !== -1,
    'empty detail: missing run shows no-data message');
})();

console.log('\u25B6 issue #577 \u2014 stale-on-error: panel status map');

(function () {
  // Stale-on-error: the panel status map correctly propagates stale state
  // through shouldRenderPanel + computePanelFreshness
  var states = { 'afk-outcomes': { status: 'ok', updatedAt: 999000 } };
  states['afk-outcomes'] = {
    status: window.resolvePanelStatuses({ afkRuns: 'boom' })['afk-outcomes'],
    updatedAt: states['afk-outcomes'].updatedAt
  };
  assert(states['afk-outcomes'].status === 'stale', 'stale-on-error: panel status is stale');
  assert(window.shouldRenderPanel(states, 'afk-outcomes') === false,
    'stale-on-error: shouldRenderPanel returns false');
  var fresh = window.computePanelFreshness(states, 'afk-outcomes', 1000000);
  assert(fresh !== null && fresh.label === 'Showing previous data',
    'stale-on-error: freshness label shows previous data');
})();

console.log('\u25B6 issue #577 \u2014 partial data: runs list with mixed field completeness');

(function () {
  window.renderAfkOutcomesTable({
    items: [
      { afk_run_id: 'pd-1', provider: 'github', status: 'completed', title: 'Full run',
        outcome_status: 'merged', started_at: '2026-08-13T09:00:00Z', last_seen_at: '2026-08-13T10:00:00Z' },
      { afk_run_id: 'pd-2', provider: null, status: null, title: null,
        outcome_status: null, started_at: null, last_seen_at: null },
      { afk_run_id: 'pd-3', provider: 'gitlab', status: 'running', title: '',
        outcome_status: 'open', started_at: '2026-08-14T08:00:00Z', last_seen_at: null }
    ],
    total: 3
  });
  var html = afkRunsTbodyEl.innerHTML;
  assert(html.indexOf('data-id="pd-1"') !== -1, 'partial: full row rendered');
  assert(html.indexOf('data-id="pd-2"') !== -1, 'partial: null-fields row rendered');
  assert(html.indexOf('data-id="pd-3"') !== -1, 'partial: empty-title row rendered');
  assert(html.indexOf('Full run') !== -1, 'partial: full title visible');
  assert(html.indexOf('badge-merged') !== -1, 'partial: merged badge for full row');
  // pd-2 has null status/title/outcome - should render fallbacks
  assert(html.indexOf('>--<') !== -1, 'partial: dash fallbacks for null provider/status');
})();

console.log('\u25B6 issue #577 \u2014 retry/error: 404 handling');

(function () {
  pendingAsyncBlocks++;
  appJsSandbox.fetch = function () {
    return Promise.resolve({ ok: false, status: 404 });
  };
  // Suppress the EXPECTED console.error from openAfkRunDetail's catch block
  var savedConsoleError = appJsSandbox.console.error;
  appJsSandbox.console.error = function () {};
  window.openAfkRunDetail('not-found-run').then(function () {
    // Restore console.error BEFORE assertions so failures are visible
    appJsSandbox.console.error = savedConsoleError;
    assert(afkDetailBodyEl.innerHTML.indexOf('AFK run not found') !== -1,
      'retry/error: 404 renders not-found message');
    pendingAsyncBlocks--;
  });
})();

console.log('\u25B6 issue #577 \u2014 retry/error: 500 handling');

(function () {
  pendingAsyncBlocks++;
  appJsSandbox.fetch = function () {
    return Promise.resolve({ ok: false, status: 500 });
  };
  // Suppress the EXPECTED console.error from openAfkRunDetail's catch block
  var savedConsoleError = appJsSandbox.console.error;
  appJsSandbox.console.error = function () {};
  window.openAfkRunDetail('error-run').then(function () {
    // Restore console.error BEFORE assertions so failures are visible
    appJsSandbox.console.error = savedConsoleError;
    assert(afkDetailBodyEl.innerHTML.indexOf('Failed to load') !== -1,
      'retry/error: 500 renders failure message');
    assert(afkDetailBodyEl.innerHTML.indexOf('AFK run not found') === -1,
      'retry/error: 500 does NOT show not-found message');
    appJsSandbox.fetch = function () { return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } }); };
    pendingAsyncBlocks--;
  });
})();

console.log('\u25B6 issue #577 \u2014 retry/error: network failure handling');

(function () {
  pendingAsyncBlocks++;
  appJsSandbox.fetch = function () {
    return Promise.reject(new Error('network down'));
  };
  // Suppress the EXPECTED console.error from openAfkRunDetail's catch block
  var savedConsoleError = appJsSandbox.console.error;
  appJsSandbox.console.error = function () {};
  window.openAfkRunDetail('network-error').then(function () {
    // Restore console.error BEFORE assertions so failures are visible
    appJsSandbox.console.error = savedConsoleError;
    assert(afkDetailBodyEl.innerHTML.indexOf('Failed to load') !== -1,
      'retry/error: network failure renders failure message');
    assert(afkDetailBodyEl.innerHTML.indexOf('network down') !== -1,
      'retry/error: network error message included');
    appJsSandbox.fetch = function () { return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } }); };
    pendingAsyncBlocks--;
  });
})();

// ── 7. Cross-provider parity (GitHub vs GitLab) ─────────────────────────

console.log('\u25B6 issue #577 \u2014 cross-provider parity: GitHub vs GitLab fixture flow');

(function () {
  // Both fixtures render the same chain structure: the same canonical step
  // order, the same badge types, the same provenance fields.
  window.renderAfkRunDetail(issue577GithubRunDetail.data);
  var ghHtml = afkDetailBodyEl.innerHTML;
  window.renderAfkRunDetail(issue577GitlabRunDetail.data);
  var glHtml = afkDetailBodyEl.innerHTML;

  // Same chain step keys present in both
  var stepKeys = ['issues', 'run', 'sessions', 'agents', 'usage', 'change_requests', 'commits', 'reviews', 'outcome'];
  stepKeys.forEach(function (key) {
    assert(ghHtml.indexOf('data-step="' + key + '"') !== -1, 'parity: GitHub has step ' + key);
    assert(glHtml.indexOf('data-step="' + key + '"') !== -1, 'parity: GitLab has step ' + key);
  });

  // Same badge classes
  assert(ghHtml.indexOf('badge-completed') !== -1, 'parity: GitHub run badge-completed');
  assert(glHtml.indexOf('badge-completed') !== -1, 'parity: GitLab run badge-completed');
  assert(ghHtml.indexOf('badge-merged') !== -1, 'parity: GitHub outcome badge-merged');
  assert(glHtml.indexOf('badge-merged') !== -1, 'parity: GitLab outcome badge-merged');

  // Both carry confidence percentages and resolver versions
  assert(ghHtml.indexOf('100%') !== -1, 'parity: GitHub confidence visible');
  assert(glHtml.indexOf('100%') !== -1, 'parity: GitLab confidence visible');
  assert(ghHtml.indexOf('resolver v2') !== -1, 'parity: GitHub resolver version');
  assert(glHtml.indexOf('resolver v2') !== -1, 'parity: GitLab resolver version');

  // Both carry Active Tokens in usage step
  assert(ghHtml.indexOf('Active Tokens') !== -1, 'parity: GitHub usage step');
  assert(glHtml.indexOf('Active Tokens') !== -1, 'parity: GitLab usage step');

  // Provider-specific content present in respective fixtures
  assert(ghHtml.indexOf('weiyentan/opencode-gateway') !== -1, 'parity: GitHub repo name');
  assert(glHtml.indexOf('cloudnative-pg/cloudnative-pg') !== -1, 'parity: GitLab repo name');
})();

console.log('\u25B6 issue #577 \u2014 cross-provider parity: runs list');

(function () {
  // Both providers render identically in the runs list with the same fields
  window.renderAfkOutcomesTable({
    items: [
      { afk_run_id: 'par-gh', provider: 'github', status: 'completed', title: 'GH run',
        outcome_status: 'merged', started_at: '2026-08-13T09:00:00Z', last_seen_at: '2026-08-13T10:00:00Z' },
      { afk_run_id: 'par-gl', provider: 'gitlab', status: 'completed', title: 'GL run',
        outcome_status: 'merged', started_at: '2026-08-14T08:00:00Z', last_seen_at: '2026-08-14T09:00:00Z' }
    ],
    total: 2
  });
  var html = afkRunsTbodyEl.innerHTML;
  // Same badge types for same status/outcome
  var ghRow = html.slice(html.indexOf('data-id="par-gh"'), html.indexOf('</tr>', html.indexOf('data-id="par-gh"')));
  var glRow = html.slice(html.indexOf('data-id="par-gl"'), html.indexOf('</tr>', html.indexOf('data-id="par-gl"')));
  assert(ghRow.indexOf('badge-completed') !== -1, 'parity runs: GitHub has completed badge');
  assert(glRow.indexOf('badge-completed') !== -1, 'parity runs: GitLab has completed badge');
  assert(ghRow.indexOf('badge-merged') !== -1, 'parity runs: GitHub has merged badge');
  assert(glRow.indexOf('badge-merged') !== -1, 'parity runs: GitLab has merged badge');
  // Provider label in each row
  assert(ghRow.indexOf('>github<') !== -1, 'parity runs: GitHub provider in row');
  assert(glRow.indexOf('>gitlab<') !== -1, 'parity runs: GitLab provider in row');
})();

// ── 8. Escaping regression (preserved from existing tests) ──────────────

console.log('\u25B6 issue #577 \u2014 escaping: entity ids with HTML metacharacters');

(function () {
  window.renderAfkRunDetail({
    run: { afk_run_id: 'esc-1', status: 'completed', outcome_status: 'open' },
    issues: [{ entity_id: '<img src=x onerror=alert(1)>', entity_type: 'issue', role: 'resolved',
               correlation_method: 'issue_reference', correlation_confidence: 1.0, evidence: [],
               resolver_version: '1', provisional: false }]
  });
  assert(afkDetailBodyEl.innerHTML.indexOf('<img src=x') === -1,
    'escaping: entity id with <img> tag is HTML-escaped');
  assert(afkDetailBodyEl.innerHTML.indexOf('&lt;img') !== -1,
    'escaping: entity id is escaped as &lt;img');
})();

console.log('\u25B6 issue #577 \u2014 escaping: run list with HTML in title');

(function () {
  window.renderAfkOutcomesTable({
    items: [
      { afk_run_id: 'esc-2', provider: 'github', status: 'completed',
        title: '<script>alert("xss")</script>', outcome_status: 'merged',
        started_at: null, last_seen_at: null }
    ],
    total: 1
  });
  assert(afkRunsTbodyEl.innerHTML.indexOf('<script>alert') === -1,
    'escaping: script tag in title is HTML-escaped');
  assert(afkRunsTbodyEl.innerHTML.indexOf('&lt;script&gt;') !== -1,
    'escaping: title is escaped as &lt;script&gt;');
})();

// ── 9. Overlay interaction (preserved from existing tests) ──────────────

console.log('\u25B6 issue #577 \u2014 overlay: open/close lifecycle');

(function () {
  pendingAsyncBlocks++;
  // Wire the close handler (normally done by setupAfkOutcomesEventHandlers)
  afkDetailCloseEl.addEventListener('click', function () {
    afkDetailOverlayEl.classList.remove('visible');
  });
  appJsSandbox.fetch = function () {
    return Promise.resolve({
      ok: true,
      json: function () {
        return Promise.resolve({
          status: 'ok',
          data: { run: { afk_run_id: 'ov-1', status: 'completed', outcome_status: 'merged' } }
        });
      }
    });
  };
  window.openAfkRunDetail('ov-1').then(function () {
    assert(afkDetailOverlayEl.classList.contains('visible'), 'overlay: visible after open');
    assert(afkDetailTitleEl.textContent.length > 0, 'overlay: title set');
    assert(afkDetailBodyEl.innerHTML.indexOf('afk-chain') !== -1, 'overlay: chain rendered');
    // Close the overlay via the wired handler
    afkDetailCloseEl._handlers.click();
    assert(afkDetailOverlayEl.classList.contains('visible') === false, 'overlay: hidden after close');
    pendingAsyncBlocks--;
  });
})();

// ── Transcript view (issue #469) ──────────────────────────────────────────
// Pure helpers for UUID validation, depth formatting, part-type classification,
// and the header/timeline/message/part renderers.

console.log('\u25B6 isValidSessionId');

assert(window.isValidSessionId(null) === false, 'null \u2192 false');
assert(window.isValidSessionId('') === false, 'empty \u2192 false');
assert(window.isValidSessionId('not-a-uuid') === false, 'plain text \u2192 false');
assert(window.isValidSessionId('  550e8400-e29b-41d4-a716-446655440000  ') === true, 'UUID with whitespace \u2192 true (trimmed)');
assert(window.isValidSessionId('550e8400-e29b-41d4-a716-446655440000') === true, 'lowercase UUID \u2192 true');
assert(window.isValidSessionId('550E8400-E29B-41D4-A716-446655440000') === true, 'uppercase UUID \u2192 true');
assert(window.isValidSessionId('550e8400e29b41d4a716446655440000') === false, 'UUID without hyphens \u2192 false');
assert(window.isValidSessionId('550e8400-e29b-41d4-a716') === false, 'truncated UUID \u2192 false');
assert(window.isValidSessionId(123) === false, 'number \u2192 false');

console.log('\u25B6 fmtTranscriptDepth');

assert(window.fmtTranscriptDepth(null) === 'Root session', 'null \u2192 Root session');
assert(window.fmtTranscriptDepth(undefined) === 'Root session', 'undefined \u2192 Root session');
assert(window.fmtTranscriptDepth(0) === 'Root session', '0 \u2192 Root session');
assert(window.fmtTranscriptDepth(1) === 'Subagent (L1)', '1 \u2192 Subagent (L1)');
assert(window.fmtTranscriptDepth(2) === 'Subagent (L2)', '2 \u2192 Subagent (L2)');
assert(window.fmtTranscriptDepth(5) === 'Subagent (L5)', '5 \u2192 Subagent (L5)');

console.log('\u25B6 depthClass');

assert(window.depthClass(null) === 'tr-depth-root', 'null \u2192 tr-depth-root');
assert(window.depthClass(0) === 'tr-depth-root', '0 \u2192 tr-depth-root');
assert(window.depthClass(1) === 'tr-depth-1', '1 \u2192 tr-depth-1');
assert(window.depthClass(2) === 'tr-depth-2', '2 \u2192 tr-depth-2');
assert(window.depthClass(3) === 'tr-depth-3', '3 \u2192 tr-depth-3');
assert(window.depthClass(4) === 'tr-depth-4', '4 \u2192 tr-depth-4');
assert(window.depthClass(5) === 'tr-depth-5', '5 \u2192 tr-depth-5');
assert(window.depthClass(6) === 'tr-depth-deep', '6 \u2192 tr-depth-deep');
assert(window.depthClass(10) === 'tr-depth-deep', '10 \u2192 tr-depth-deep');

console.log('\u25B6 transcriptPartTypeClass');

assert(window.transcriptPartTypeClass('tool') === 'tr-part-tool', 'tool \u2192 tr-part-tool');
assert(window.transcriptPartTypeClass('text') === 'tr-part-text', 'text \u2192 tr-part-text');
assert(window.transcriptPartTypeClass('reasoning') === 'tr-part-reasoning', 'reasoning \u2192 tr-part-reasoning');
assert(window.transcriptPartTypeClass('step-start') === 'tr-part-step-start', 'step-start \u2192 tr-part-step-start');
assert(window.transcriptPartTypeClass('step-finish') === 'tr-part-step-finish', 'step-finish \u2192 tr-part-step-finish');
assert(window.transcriptPartTypeClass('unknown-type') === 'tr-part-unknown', 'unknown type \u2192 tr-part-unknown');
assert(window.transcriptPartTypeClass(null) === 'tr-part-unknown', 'null \u2192 tr-part-unknown');

console.log('\u25B6 renderTranscriptHeader');

assert(window.renderTranscriptHeader(null) === '', 'null header \u2192 empty string');
assert(window.renderTranscriptHeader({}) !== '', 'empty header object \u2192 non-empty HTML');

var hdrHtml = window.renderTranscriptHeader({
  id: '550e8400-e29b-41d4-a716-446655440000',
  external_session_id: 'ses_abc123',
  agent: 'test-agent',
  message_count: 15,
  part_count: 42,
  tool_call_count: 8,
  first_part_at: '2026-01-01T00:00:00Z',
  last_part_at: '2026-01-01T01:00:00Z',
  parent_session_id: null,
  child_session_ids: ['child-1', 'child-2']
});
assert(hdrHtml.indexOf('tr-header-card') !== -1, 'header contains tr-header-card');
assert(hdrHtml.indexOf('550e8400') !== -1, 'header contains short UUID');
assert(hdrHtml.indexOf('ses_abc123') !== -1, 'header contains external session id');
assert(hdrHtml.indexOf('test-agent') !== -1, 'header contains agent name');
assert(hdrHtml.indexOf('15') !== -1, 'header contains message count');
assert(hdrHtml.indexOf('42') !== -1, 'header contains part count');
assert(hdrHtml.indexOf('8') !== -1, 'header contains tool call count');
assert(hdrHtml.indexOf('None (root)') !== -1, 'header shows None (root) for no parent');
assert(hdrHtml.indexOf('2') !== -1, 'header shows 2 children');

console.log('\u25B6 renderTimelineEvent');

var tlEvent = window.renderTimelineEvent({
  depth: 0,
  agent: 'coordinator',
  part_type: 'tool',
  source_created_at: '2026-01-01T00:05:00Z',
  source_created_at_tz: '2026-01-01T00:05:00Z',
  data: { tool: 'read_file', status: 'completed', input: { path: '/tmp/test.py' }, output: 'file contents' }
});
assert(tlEvent.indexOf('tr-depth-root') !== -1, 'timeline event depth=0 has tr-depth-root class');
assert(tlEvent.indexOf('coordinator') !== -1, 'timeline event shows agent');
assert(tlEvent.indexOf('tr-part-tool') !== -1, 'timeline event tool type has tr-part-tool class');
assert(tlEvent.indexOf('read_file') !== -1, 'timeline event shows tool name');
assert(tlEvent.indexOf('tr-tool-io') !== -1, 'timeline event tool shows input/output blocks');

var tlText = window.renderTimelineEvent({
  depth: 1,
  agent: 'subagent',
  part_type: 'text',
  source_created_at: '2026-01-01T00:10:00Z',
  source_created_at_tz: '2026-01-01T00:10:00Z',
  data: { text: 'Hello world' }
});
assert(tlText.indexOf('tr-depth-1') !== -1, 'timeline event depth=1 has tr-depth-1 class');
assert(tlText.indexOf('subagent') !== -1, 'timeline event shows subagent');
assert(tlText.indexOf('tr-part-text') !== -1, 'timeline event text type has tr-part-text class');
assert(tlText.indexOf('Hello world') !== -1, 'timeline event shows text content');

console.log('\u25B6 renderTranscriptMessage');

var msgHtml = window.renderTranscriptMessage({
  role: 'assistant',
  agent: 'coder',
  mode: 'chat',
  source_created_at: '2026-01-01T00:05:00Z',
  source_created_at_tz: '2026-01-01T00:05:00Z',
  input_tokens: 1000,
  output_tokens: 500,
  data: { text: 'I will fix the bug' }
});
assert(msgHtml.indexOf('tr-msg') !== -1, 'message has tr-msg class');
assert(msgHtml.indexOf('tr-msg-assistant') !== -1, 'assistant message has role class');
assert(msgHtml.indexOf('coder') !== -1, 'message shows agent');
assert(msgHtml.indexOf('I will fix the bug') !== -1, 'message shows text content');

console.log('\u25B6 renderTranscriptPart');

var partHtml = window.renderTranscriptPart({
  part_type: 'tool',
  source_created_at: '2026-01-01T00:05:00Z',
  source_created_at_tz: '2026-01-01T00:05:00Z',
  data: { tool: 'write_file', status: 'completed', input: { path: '/tmp/out.py' }, output: 'done' }
});
assert(partHtml.indexOf('tr-part') !== -1, 'part has tr-part class');
assert(partHtml.indexOf('tr-part-tool') !== -1, 'tool part has tr-part-tool class');
assert(partHtml.indexOf('write_file') !== -1, 'part shows tool name');
assert(partHtml.indexOf('tr-tool-io') !== -1, 'part shows tool input/output blocks');

console.log('\u25B6 renderTranscriptList');

assert(window.renderTranscriptList([], function () { return ''; }) === '<p class="empty-state">No items to display</p>',
  'empty list \u2192 empty state');
assert(window.renderTranscriptList(null, function () { return ''; }) === '<p class="empty-state">No items to display</p>',
  'null list \u2192 empty state');
var listHtml = window.renderTranscriptList(['a', 'b'], function (x) { return '<div>' + x + '</div>'; });
assert(listHtml.indexOf('<div>a</div>') !== -1, 'list renders first item');
assert(listHtml.indexOf('<div>b</div>') !== -1, 'list renders second item');

// ── Transcript markup smoke check (frontend/index.html) ─────────────────
// Static verification of the transcript tab in the real index.html.

console.log('\u25B6 index.html — transcript tab markup (smoke check)');

(function () {
  var html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

  // Transcript tab exists in the nav
  assert(html.indexOf('data-tab="transcript"') !== -1, 'top nav: transcript tab exists');
  assert(html.indexOf('id="tab-transcript"') !== -1, 'top nav: transcript tab-content panel exists');
  assert(html.indexOf('data-tab="transcript" tabindex="0"') !== -1, 'transcript tab is keyboard-focusable');

  // Five tabs total now (overview, agent-runs, clients-projects, afk-outcomes, transcript)
  var navItemCount = (html.match(/class="top-nav-item/g) || []).length;
  assert(navItemCount === 5, 'top nav: exactly five top-nav-item elements (' + navItemCount + ' found)');

  // Transcript panel elements exist
  assert(html.indexOf('id="tr-session-input"') !== -1, 'transcript: session input exists');
  assert(html.indexOf('id="tr-load-btn"') !== -1, 'transcript: load button exists');
  assert(html.indexOf('id="tr-session-header"') !== -1, 'transcript: session header container exists');
  assert(html.indexOf('id="tr-view-toggle"') !== -1, 'transcript: view toggle exists');
  assert(html.indexOf('id="tr-timeline-wrap"') !== -1, 'transcript: timeline container exists');
  assert(html.indexOf('id="tr-messages-wrap"') !== -1, 'transcript: messages container exists');
  assert(html.indexOf('id="tr-parts-wrap"') !== -1, 'transcript: parts container exists');
  assert(html.indexOf('id="tr-next-page-btn"') !== -1, 'transcript: next page button exists');
  assert(html.indexOf('id="tr-status"') !== -1, 'transcript: status text exists');

  // View toggle buttons with data-view attributes
  assert(html.indexOf('data-view="timeline"') !== -1, 'transcript: timeline view button exists');
  assert(html.indexOf('data-view="messages"') !== -1, 'transcript: messages view button exists');
  assert(html.indexOf('data-view="parts"') !== -1, 'transcript: parts view button exists');
})();

// ── Transcript CSS smoke check (frontend/style.css) ─────────────────────

console.log('\u25B6 style.css — transcript styles (smoke check)');

(function () {
  var css = fs.readFileSync(path.join(__dirname, '..', 'style.css'), 'utf8');
  var live = css.replace(/\/\*[\s\S]*?\*\//g, '');

  assert(live.indexOf('.tr-controls') !== -1, 'style.css: .tr-controls rule exists');
  assert(live.indexOf('.tr-view-toggle') !== -1, 'style.css: .tr-view-toggle rule exists');
  assert(live.indexOf('.tr-view-btn') !== -1, 'style.css: .tr-view-btn rule exists');
  assert(live.indexOf('.tr-depth-root') !== -1, 'style.css: .tr-depth-root rule exists');
  assert(live.indexOf('.tr-depth-1') !== -1, 'style.css: .tr-depth-1 rule exists');
  assert(live.indexOf('.tr-tool-name') !== -1, 'style.css: .tr-tool-name rule exists');
  assert(live.indexOf('.tr-tool-io') !== -1, 'style.css: .tr-tool-io rule exists');
  assert(live.indexOf('.tr-msg') !== -1, 'style.css: .tr-msg rule exists');
  assert(live.indexOf('.tr-part') !== -1, 'style.css: .tr-part rule exists');
  assert(live.indexOf('.tr-pagination') !== -1, 'style.css: .tr-pagination rule exists');
  assert(live.indexOf('.panel-transcript') !== -1, 'style.css: .panel-transcript rule exists');
  assert(live.indexOf('.tr-text-content') !== -1, 'style.css: .tr-text-content rule exists');
  assert(live.indexOf('.tr-view-wrap') !== -1, 'style.css: .tr-view-wrap rule exists');
})();

// ── Issue #557: provider + token-breakdown helpers (pure functions) ─────
// The v1.2 raw-token/provider presentation: provider badges with missing
// labels, cache hit ratio math, the Token Breakdown detail section, the
// merged-table header additions, and the neutral provider-badge / numeric
// column styles — all verified against the production app.js/index.html/
// style.css (no copy-paste duplicates).

console.log('\u25B6 issue #557 — provider + token breakdown');

(function () {
  // fmtProvider: badge for present values, missing labels otherwise.
  var badgeHtml = window.fmtProvider('openai');
  assert(badgeHtml.indexOf('badge-provider') !== -1 && badgeHtml.indexOf('openai') !== -1,
    'fmtProvider: present provider renders a badge-provider badge with the text label');
  assert(window.fmtProvider(null) === '\u2014', 'fmtProvider: null renders em dash by default');
  assert(window.fmtProvider(undefined) === '\u2014', 'fmtProvider: undefined renders em dash by default');
  assert(window.fmtProvider('') === '\u2014', 'fmtProvider: empty string renders em dash by default');
  assert(window.fmtProvider(null, 'unknown') === 'unknown',
    'fmtProvider: detail overlay missing label is "unknown"');
  assert(window.fmtProvider('anthropic', 'unknown').indexOf('anthropic') !== -1,
    'fmtProvider: present provider ignores the missing label');

  // fmtCacheHitRatio: read / (input + read), '--' on zero denominator.
  assert(window.fmtCacheHitRatio(25, 100) === '20.0%', 'fmtCacheHitRatio: 25/(100+25) = 20.0%');
  assert(window.fmtCacheHitRatio(0, 0) === '--', 'fmtCacheHitRatio: zero denominator renders --');
  assert(window.fmtCacheHitRatio(null, 100) === '0.0%', 'fmtCacheHitRatio: null read treated as 0');
  assert(window.fmtCacheHitRatio(10, null) === '100.0%', 'fmtCacheHitRatio: null input treated as 0');

  // fmtTokenBreakdownSection: read/write/reasoning totals + cache hit ratio
  // + provider with overlay missing semantics.
  var section = window.fmtTokenBreakdownSection({
    total_input_tokens: 100,
    total_output_tokens: 50,
    total_cache_read_tokens: 25,
    total_cache_write_tokens: 5,
    total_reasoning_tokens: 7,
    primary_provider: 'openai'
  });
  assert(section.indexOf('Token Breakdown') !== -1, 'token breakdown: section title present');
  assert(section.indexOf('Cache Read Tokens') !== -1 && section.indexOf('25') !== -1,
    'token breakdown: cache read total present');
  assert(section.indexOf('Cache Write Tokens') !== -1 && section.indexOf('>5<') !== -1,
    'token breakdown: cache write total present');
  assert(section.indexOf('Reasoning Tokens') !== -1 && section.indexOf('>7<') !== -1,
    'token breakdown: reasoning total present');
  assert(section.indexOf('Cache Hit Ratio') !== -1 && section.indexOf('20.0%') !== -1,
    'token breakdown: cache hit ratio present');
  assert(section.indexOf('badge-provider') !== -1 && section.indexOf('openai') !== -1,
    'token breakdown: provider badge present');

  var missingSection = window.fmtTokenBreakdownSection({
    total_input_tokens: 0,
    total_output_tokens: 0,
    total_cache_read_tokens: null,
    total_cache_write_tokens: null,
    total_reasoning_tokens: null,
    primary_provider: null
  });
  assert(missingSection.indexOf('unknown') !== -1, 'token breakdown: missing provider renders "unknown"');
  assert(missingSection.indexOf('>0<') !== -1, 'token breakdown: null token fields render 0');
  assert(missingSection.indexOf('Cache Hit Ratio') !== -1 && missingSection.indexOf('--') !== -1,
    'token breakdown: zero-denominator ratio renders --');
})();

// ── Issue #557: merged-table header + style smoke checks ────────────────

console.log('\u25B6 issue #557 — index.html + style.css (provider + token columns)');

(function () {
  var html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  var css = fs.readFileSync(path.join(__dirname, '..', 'style.css'), 'utf8');
  var live = css.replace(/\/\*[\s\S]*?\*\//g, ''); // comment-stripped: guards assert on real rules only

  // New headers: Provider and Cache Read are retained at tablet width;
  // Cache Write and Reasoning carry ar-col-low (hidden at 761–1024px).
  assert(html.indexOf('<th>Provider</th>') !== -1, 'merged header: Provider column exists');
  assert(html.indexOf('<th>Cache Read</th>') !== -1, 'merged header: Cache Read column exists');
  assert(html.indexOf('<th class="ar-col-low">Cache Write</th>') !== -1,
    'merged header: Cache Write marked ar-col-low (tablet-hidden)');
  assert(html.indexOf('<th class="ar-col-low">Reasoning</th>') !== -1,
    'merged header: Reasoning marked ar-col-low (tablet-hidden)');
  assert(html.indexOf('<th class="ar-col-low">Provider</th>') === -1 &&
    html.indexOf('<th class="ar-col-low">Cache Read</th>') === -1,
    'merged header: Provider and Cache Read retained at tablet width');

  // Neutral outlined provider badge: hairline border, transparent fill,
  // no status color, no animation.
  assert(live.indexOf('.badge-provider') !== -1, 'style.css: .badge-provider rule exists');
  var badgeBlock = live.slice(live.indexOf('.badge-provider'), live.indexOf('}', live.indexOf('.badge-provider')) + 1);
  assert(badgeBlock.indexOf('border: 1px solid') !== -1, 'style.css: provider badge is outlined');
  assert(badgeBlock.indexOf('background: transparent') !== -1, 'style.css: provider badge fill is transparent');
  assert(badgeBlock.indexOf('animation') === -1, 'style.css: provider badge has no animation/pulse');

  // Numeric columns: right-aligned with tabular numerals.
  assert(live.indexOf('#agent-runs-table td.ar-num') !== -1, 'style.css: td.ar-num rule exists');
  var numBlock = live.slice(live.indexOf('#agent-runs-table td.ar-num'),
    live.indexOf('}', live.indexOf('#agent-runs-table td.ar-num')) + 1);
  assert(numBlock.indexOf('text-align: right') !== -1, 'style.css: numeric cells right-aligned');
  assert(numBlock.indexOf('tabular-nums') !== -1, 'style.css: numeric cells use tabular numerals');

  // No green active-row wash / cyan stripe / glow / pulse introduced.
  assert(live.indexOf('[data-active') === -1,
    'issue #557: no status-driven [data-active] row selector added');
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
