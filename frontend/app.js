/* ═══════════════════════════════════════════════════════════════════════════
   Aurora Glass — Dashboard Logic
   Vanilla JS.  No frameworks.  No build step.
   ═══════════════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  // ── Configuration ──────────────────────────────────────────────────────

  const REFRESH_INTERVAL_MS = parseInt(
    document.querySelector('meta[name="refresh-interval"]')?.getAttribute('content'),
    10
  ) || 30000; // 30s default; override via <meta name="refresh-interval" content="...">
  const RECORD_LIMIT = 100;
  const CLIENT_LIMIT = 100;
  /**
   * Client metadata cache expiry policy: the client_id → name map is fetched
   * from /admin/clients at most once per 10-minute window.  Within the TTL
   * refresh cycles reuse the cached map; after expiry the next cycle
   * refetches.  Unknown client ids additionally trigger a non-blocking
   * background refresh at any time (see ensureClientName).
   */
  const CLIENT_CACHE_TTL_MS = 600000; // 10 minutes
  const AGENT_RUN_LIMIT = 50;

  // ── Element refs ───────────────────────────────────────────────────────

  const $ = function (id) { return document.getElementById(id); };

  const els = {
    dashboard:      document.querySelector('.dashboard'),
    liveIndicator:  $('live-indicator'),
    timestamp:      $('timestamp'),
    lastRefreshed:  $('last-refreshed'),
    dbStatus:       $('db-status'),
    versionFooter:  $('footer-version'),

    // KPIs
    kpiTokens:      $('kpi-tokens'),
    kpiTokensDetail:$('kpi-tokens-detail'),
    kpiCost:        $('kpi-cost'),
    kpiCostDetail:  $('kpi-cost-detail'),
    kpiSessions:    $('kpi-sessions'),
    kpiSessionsDetail: $('kpi-sessions-detail'),
    kpiCollectors:  $('kpi-collectors'),
    kpiCollectorsDetail: $('kpi-collectors-detail'),
    kpiSourceDbs:   $('kpi-source-dbs'),
    kpiSourceDbsDetail: $('kpi-source-dbs-detail'),

    // Sections
    modelMixChart:  $('model-mix-chart'),
    eventsFeed:     $('events-feed'),
    eventBadge:     $('event-badge'),
    collectorDist:  $('collector-dist-chart'),
    collectorsTbody: $('collectors-tbody'),
    agentsTbody:    $('agents-tbody'),

    // Agent Runs — merged Sessions + Agent Runs table (issue #402)
    arTbody:        $('agent-runs-tbody'),
    arFilterFrom:   $('ar-filter-from'),
    arFilterTo:     $('ar-filter-to'),
    arFilterAgent:  $('ar-filter-agent'),
    arFilterStatus: $('ar-filter-status'),
    arFilterApply:  $('ar-filter-apply'),
    arFilterClear:  $('ar-filter-clear'),
    arDetailOverlay: $('ar-detail-overlay'),
    arDetailTitle:  $('ar-detail-title'),
    arDetailBody:   $('ar-detail-body'),
    arDetailClose:  $('ar-detail-close'),
    arPagination:   $('agent-runs-pagination'), // control block below the panel (issue #427)

    // Client/Project
    cpTbody:         $('cp-tbody'),
    cpPanelSubtitle: $('cp-panel-subtitle'),

    // Date range bar
    drPreset:       $('dr-preset'),
    drCustomInputs: $('dr-custom-inputs'),
    drStartDate:    $('dr-start-date'),
    drEndDate:      $('dr-end-date'),
  };

  // ── State ──────────────────────────────────────────────────────────────

  let refreshTimer = null;
  let fetchErrors = {};    // endpoint_key → error_message, per-fetch-cycle tracking
  let agentRunsData = null;       // latest agent runs response
  let agentRunFilters = {};       // current filter values
  let agentRunDetail = null;      // current detail view data
  let agentRunsFetchError = null; // per-cycle fetch error for agent runs
  // Agent Runs pagination state (issue #426): the current page and page
  // size, read from the URL (?page / ?page_size) on dashboard load and
  // translated to the existing agent-runs API's limit/offset at request
  // time.  Defaults match the pre-pagination behavior exactly: page 1 of
  // AGENT_RUN_LIMIT (50) rows.
  let agentRunPage = 1;
  let agentRunPageSize = AGENT_RUN_LIMIT;
  let dateRangeState = { preset: 'this-month' }; // selected date-range preset
  let expandedClientNames = {}; // drilldown: client names with expanded project rows
  let _lastDateRangeKey = null; // tracks previous render's date range context for resetting drilldown

  // ── Panel freshness state (issue #357) ────────────────────────────────
  // Per-panel freshness map: panelId → { status: 'ok'|'refreshing'|'stale',
  // updatedAt }.  Maintained by refreshDashboard() (and applyFilters() for
  // the agent-runs panel) and consumed by each panel render through the
  // pure helpers below (computePanelFreshness / shouldRenderPanel).  A panel
  // whose fetch failed resolves to 'stale' and its render function skips the
  // re-render, so the previous successful data stays on screen.
  //
  // lastRefreshedAt records the time of the last COMPLETED refresh cycle.
  // It is module-level and exposed via getLastRefreshedAt() so follow-up
  // work (issue #358 — KPI label clarification) can reuse it; the header
  // "Last refreshed HH:MM:SS" clock consumes it via updateLastRefreshed().
  let panelStates = {};
  let lastRefreshedAt = null;

  // Which fetch endpoint keys feed each panel — used to resolve a panel to
  // 'stale' when any of its endpoints failed in the current refresh cycle.
  // 'agentRuns' maps to the agentRunsFetchError channel (fetched separately).
  // The merged Sessions + Agent Runs view (issue #402) reads the Sessions KPI
  // from the aggregates total row and the events feed from the agent-runs
  // channel; the /api/v1/usage/sessions endpoint is no longer fetched.
  const PANEL_ENDPOINTS = {
    'kpi-tokens':     ['aggTotal'],
    'kpi-cost':       ['aggTotal'],
    'kpi-sessions':   ['aggTotal'],
    'kpi-collectors': ['health'],
    'kpi-source-dbs': ['health'],
    'model-mix':     ['aggByModel'],
    events:          ['health', 'agentRuns'],
    'collector-dist': ['health'],
    collectors:      ['health'],
    agents:          ['aggByModel', 'health'],
    'agent-runs':    ['agentRuns'],
    'client-project': ['aggClientProject'],
  };

  // ── Client metadata cache ─────────────────────────────────────────────
  // The client_id → name map changes rarely but costs one HTTP round trip
  // per fetch, so it is cached in memory with a 10-minute expiry
  // (CLIENT_CACHE_TTL_MS).  Within the TTL window refresh cycles reuse the
  // cached map; after expiry the next cycle refetches.  Lookup misses
  // (unknown client ids in rendered data) trigger a non-blocking background
  // refresh, and a failed refresh never clears the map — last-known names
  // stay available (stale-while-failure).

  /**
   * Create an in-memory cache for the client_id → name lookup map.
   * Pure factory — no DOM or fetch access — so the Node test harness can
   * exercise hit/miss/expiry/invalidation with an injected clock.
   *
   * Expiry policy: the cache is stale once `ttlMs` (default
   * CLIENT_CACHE_TTL_MS, 10 minutes) elapses since the last successful
   * refresh.  Entries never expire individually — get() keeps returning
   * last-known names even after expiry (stale-while-revalidate); callers
   * decide when to refetch via isExpired().
   *
   * @param {Object} [opts] - { ttlMs, now }; now() injects the clock (tests)
   * @returns {Object} cache handle: get/set/has/isExpired/refresh/invalidate/snapshot
   */
  function createClientCache(opts) {
    var ttlMs = (opts && opts.ttlMs != null) ? opts.ttlMs : CLIENT_CACHE_TTL_MS;
    var nowFn = (opts && typeof opts.now === 'function') ? opts.now : Date.now;
    var map = {};        // client_id → name (last known; survives refresh failures)
    var loadedAt = null; // ms timestamp of last successful refresh; null = never loaded

    function isExpired() {
      return loadedAt === null || (nowFn() - loadedAt) >= ttlMs;
    }

    return {
      /** Look up a client name; undefined when the id is unknown (miss). */
      get: function (id) { return map[id]; },
      /** True when the id has a cached name. */
      has: function (id) { return Object.prototype.hasOwnProperty.call(map, id); },
      /** True when never loaded or the TTL window has elapsed. */
      isExpired: isExpired,
      /** Record a single client entry (client_id → name). */
      set: function (id, name) { map[id] = name; },
      /**
       * Replace the whole map with a freshly fetched client list.  Call only
       * on success — on failure leave the map untouched so last-known names
       * remain available (stale-while-failure).  Resets the TTL window.
       */
      refresh: function (entries) {
        var next = {};
        (entries || []).forEach(function (c) {
          next[c.id] = c.name || c.id;
        });
        map = next;
        loadedAt = nowFn();
      },
      /**
       * Manual invalidation for use after client administration changes:
       * marks the cache stale so the next refresh cycle refetches, but keeps
       * last-known names so labels survive a subsequent fetch failure.
       */
      invalidate: function () {
        loadedAt = null;
      },
      /** Copy of the current map (introspection/tests). */
      snapshot: function () { return Object.assign({}, map); }
    };
  }

  /** The live client metadata cache used by the dashboard (10-min TTL). */
  var clientCache = createClientCache({ ttlMs: CLIENT_CACHE_TTL_MS });

  /** Shared in-flight promise: deduplicates concurrent background refreshes
   *  so that a single render pass (up to ~25 cache misses) fires at most one
   *  /admin/clients request.  Null while no refresh is in flight. */
  var clientsRefreshInFlight = null;

  /** Refetch client metadata and replace the cached map on success.
   *  Never clears the map: on failure last-known names are retained.
   *  Deduplicates in-flight requests through a shared promise — concurrent
   *  callers all await the same single fetch. */
  function refreshClientCache() {
    if (clientsRefreshInFlight) return clientsRefreshInFlight;
    clientsRefreshInFlight = apiFetch('/admin/clients?limit=' + CLIENT_LIMIT)
      .then(function (data) {
        if (data && data.items) {
          clientCache.refresh(data.items);
        }
        return data;
      })
      .catch(function (e) {
        // Stale-while-failure: keep the last-known client names.
        console.error('Client metadata refresh failed:', e);
      })
      .finally(function () {
        clientsRefreshInFlight = null;
      });
    return clientsRefreshInFlight;
  }

  /**
   * Resolve a client's display name from the cache.  A lookup miss (unknown
   * client id) triggers a non-blocking background refresh of the cache and
   * returns undefined, letting callers fall back to the raw id.
   */
  function ensureClientName(clientId) {
    var name = clientCache.get(clientId);
    if (name === undefined) {
      refreshClientCache(); // background refresh — fire and forget
    }
    return name;
  }

  /** Expose cache invalidation for client administration changes. */
  function invalidateClientCache() {
    clientCache.invalidate();
  }
  /**
   * Resolve the display label for a project row in the Client/Project
   * Usage Breakdown.  Single source of truth — used by both the render
   * function and tests.
   *
   * Priority: projectLabel → projectId → 'unknown'
   * @param {object|null|undefined} p - projectRow shaped object
   * @returns {string}
   */
  function resolveProjectLabel(p) {
    return (p && p.projectLabel) || (p && p.projectId) || 'unknown';
  }
  /**
   * Resolve date range from state, handling both preset and custom.
   * Delegates to computeDateRange for named presets; constructs Date
   * objects from custom date strings when preset is 'custom'.
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

  // ── Helpers ────────────────────────────────────────────────────────────

  /** ISO-8601 date string for N days ago at midnight UTC */
  function daysAgo(n) {
    const d = new Date();
    d.setDate(d.getDate() - n);
    d.setUTCHours(0, 0, 0, 0);
    return d.toISOString();
  }

  /** ISO-8601 now */
  function nowISO() {
    return new Date().toISOString();
  }

  /** Format a number with locale-aware separators */
  function fmtNum(n) {
    if (n == null || isNaN(n)) return '--';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return n.toLocaleString('en-US');
  }

  /** Format cost to 2–4 decimal places */
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

  /** Format a duration between two ISO timestamps */
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

  /** Format a relative time string */
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

  // ── Date-range engine ──────────────────────────────────────────────────

  /**
   * Compute a start/end Date range from a named preset.
   * @param {string} preset - 'this-month', 'last-month', 'last-30-days', 'last-7-days'
   * @returns {{ startDate: Date, endDate: Date }}
   */
  function computeDateRange(preset) {
    var now = new Date();
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
   * @returns {string} e.g. "Jul 1\u201327, 2026"
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
      // Same month and year: "Jul 1\u201327, 2026"
      var m = startDate.toLocaleDateString('en-US', { month: 'short' });
      return m + ' ' + startDate.getDate() + '\u2013' + endDate.getDate() + ', ' + startYear;
    } else if (startYear === endYear) {
      // Different months, same year: "Jun 28\u2013Jul 27, 2026"
      return startDate.toLocaleDateString('en-US', monthDayOpts) + '\u2013' + endDate.toLocaleDateString('en-US', monthDayOpts) + ', ' + startYear;
    } else {
      // Different years: "Dec 28, 2025\u2013Jan 27, 2026"
      return startDate.toLocaleDateString('en-US', fullOpts) + '\u2013' + endDate.toLocaleDateString('en-US', fullOpts);
    }
  }

  /** Format a short datetime */
  function fmtDT(isoStr) {
    if (!isoStr) return '--';
    const d = new Date(isoStr);
    return d.toLocaleString('en-US', {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit'
    });
  }

  /** Format an Agent Runs "Last Updated" timestamp as a year-inclusive
   *  absolute local datetime, e.g. "Aug 11, 2026, 9:41 AM" (issue #4).
   *  Missing/unparseable input → '--' (fmtDT/fmtRelative fallback style).
   *  Pure — no DOM access.  Accepts an injected clock for deterministic
   *  tests (precedent: createClientCache now-fn); when omitted it defaults
   *  to the real clock so production callers can pass just the ISO string.
   *  Future timestamps (backend clock skew) clamp to the injected now,
   *  mirroring formatUpdatedAgo's future-clamp behavior.  A non-finite
   *  injected clock (invalid Date, now-fn returning a string/NaN) falls
   *  back to Date.now() so the clamp can never render "Invalid Date".
   *  @param {*} isoStr - ISO 8601 timestamp string
   *  @param {*} [now]  - injected clock: Date, ms number, or now-fn
   *  @returns {string} e.g. "Aug 11, 2026, 9:41 AM" | "--" */
  function formatAgentRunTimestamp(isoStr, now) {
    if (!isoStr) return '--';
    let d = new Date(isoStr);
    if (isNaN(d.getTime())) return '--';
    // Duck-type the injected clock (cross-realm Dates from the Node test
    // harness fail instanceof, so check getTime — same trick as kpiSubtitle).
    let nowMs;
    if (typeof now === 'function') {
      nowMs = now();
    } else if (now != null && typeof now.getTime === 'function') {
      nowMs = now.getTime();
    } else if (typeof now === 'number') {
      nowMs = now;
    } else {
      nowMs = Date.now();
    }
    // Guard the injected clock: coerce it to a finite epoch-ms number so a
    // non-finite result (invalid Date whose getTime() is NaN, or a now-fn
    // returning a string) falls back to the real clock instead of silently
    // skipping the future-clamp or rendering "Invalid Date".
    nowMs = Number(nowMs);
    if (!isFinite(nowMs)) nowMs = Date.now();
    if (d.getTime() > nowMs) d = new Date(nowMs);
    return d.toLocaleString('en-US', {
      month: 'short', day: 'numeric', year: 'numeric',
      hour: 'numeric', minute: '2-digit'
    });
  }

  /** Derive LLM provider from model name */
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

  /** Create a badge span */
  function badge(label, cls) {
    var s = document.createElement('span');
    s.className = 'badge ' + (cls || 'badge-unknown');
    s.textContent = label;
    return s;
  }

  /** Format todo progress string — completed/total */
  function fmtTodoProgress(completed, total) {
    if (total == null || total <= 0) return '--';
    var c = completed || 0;
    return c + '/' + total;
  }

  /** Get a CSS status badge class for agent run status */
  function statusBadgeClass(status) {
    if (status === 'running') return 'badge-running';
    if (status === 'stale') return 'badge-stale';
    if (status === 'completed') return 'badge-completed';
    if (status === 'blocked') return 'badge-blocked';
    return 'badge-unknown';
  }

  /** Format code changes count */
  function fmtCodeChanges(n) {
    if (n == null || n <= 0) return '--';
    return fmtNum(n);
  }

  /** Format a short UUID for display */
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

  // ── Panel freshness (pure helpers — no DOM) ───────────────────────────
  // Freshness state model (issue #357): refreshDashboard() maintains a
  // per-panel state map (panelStates: panelId → { status, updatedAt }) and
  // each panel render consumes it through these helpers.  status is one of
  //   'refreshing' — a refresh cycle is in flight for this panel
  //   'ok'         — last fetch succeeded; updatedAt = completion time
  //   'stale'      — last fetch failed; the panel keeps showing previous data
  // These functions are pure so the Node test harness can exercise the full
  // refreshing/updated/stale lifecycle through the vm-sandbox exports.

  /** Format "Updated Xm ago" text from a panel's last successful update time.
   *  @param {number|null} updatedAtMs  epoch ms of the last successful update
   *  @param {number}      nowMs        epoch ms reference time
   *  @returns {string|null} "just now" (<60s), "Nm ago" (<60m), "Nh ago" (<24h),
   *                         "Nd ago", or null when updatedAtMs is absent
   *                         (panel never updated).  Future timestamps clamp to
   *                         "just now" instead of rendering a negative age. */
  function formatUpdatedAgo(updatedAtMs, nowMs) {
    if (updatedAtMs == null) return null;
    var diffMs = Math.max(0, nowMs - updatedAtMs);
    var mins = Math.floor(diffMs / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return mins + 'm ago';
    var hrs = Math.floor(mins / 60);
    if (hrs < 24) return hrs + 'h ago';
    return Math.floor(hrs / 24) + 'd ago';
  }

  /** Compute a panel's freshness descriptor from the per-panel state map.
   *  @param {Object}   panelStates panelId → { status, updatedAt }
   *  @param {string}   panelId     e.g. 'model-mix'
   *  @param {number}   nowMs       epoch ms reference time
   *  @returns {{status: string, label: string, cssClass: string}|null}
   *    refreshing → { label: 'Refreshing…', cssClass: 'freshness-refreshing' }
   *    stale + previously updated → { label: 'Showing previous data', cssClass: 'freshness-stale' }
   *    stale + never updated       → null (no previous data to reference)
   *    ok         → { label: 'Updated ' + formatUpdatedAgo(...) }
   *    unknown/idle panel → null (render nothing) */
  function computePanelFreshness(panelStates, panelId, nowMs) {
    var st = panelStates && panelStates[panelId];
    if (!st) return null;
    if (st.status === 'refreshing') {
      return { status: 'refreshing', label: 'Refreshing\u2026', cssClass: 'freshness-refreshing' };
    }
    if (st.status === 'stale') {
      // Never rendered: no previous data exists, so no "Showing previous
      // data" label — the panel render shows its empty/error state instead.
      if (st.updatedAt == null) return null;
      return { status: 'stale', label: 'Showing previous data', cssClass: 'freshness-stale' };
    }
    return {
      status: 'ok',
      label: 'Updated ' + (formatUpdatedAgo(st.updatedAt, nowMs) || '--'),
      cssClass: ''
    };
  }

  /** Whether a panel render should repaint its content.
   *  A 'stale' panel (failed fetch) returns false so the render function
   *  keeps the previous successful data on screen and only the freshness
   *  label ("Showing previous data") is swapped in.
   *  A stale panel with no previous render (updatedAt null) has no data to
   *  retain — let the render run so it shows its empty/error state. */
  function shouldRenderPanel(panelStates, panelId) {
    var st = panelStates && panelStates[panelId];
    // A stale panel with no previous render (updatedAt null) has no data to
    // retain — let the render run so it shows its empty/error state.
    return !(st && st.status === 'stale' && st.updatedAt != null);
  }

  /** Resolve every panel's post-fetch status from the current endpoint errors.
   *  @param {Object} endpointErrors fetch-error map keyed by endpoint key
   *                                 ('agentRuns' for the agent-runs channel)
   *  @returns {Object} panelId → 'ok' | 'stale' */
  function resolvePanelStatuses(endpointErrors) {
    var out = {};
    Object.keys(PANEL_ENDPOINTS).forEach(function (panelId) {
      var failed = PANEL_ENDPOINTS[panelId].some(function (key) {
        return !!endpointErrors[key];
      });
      out[panelId] = failed ? 'stale' : 'ok';
    });
    return out;
  }

  /** Format a Date as a compact HH:MM:SS clock for the header
   *  "Last refreshed" timestamp (matches the dashboard's 24h header style). */
  function formatClockTime(d) {
    return d.toLocaleString('en-US', {
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
    });
  }

  // ── KPI card classification (issue #358) ───────────────────────────────
  // Historical KPIs aggregate usage over the selected date range, so their
  // subtitles keep the range label.  Current-health KPIs (Healthy
  // Collectors, Source Databases) are live snapshots from /health — they
  // must not be presented as historical aggregates, so their subtitles
  // show "As of HH:MM:SS" instead of the date range.
  const KPI_TYPES = {
    'kpi-tokens':     'historical', // Active Tokens — date-range aggregate
    'kpi-cost':       'historical', // Est. Cost (USD) — date-range aggregate
    'kpi-sessions':   'historical', // Sessions — date-range aggregate
    'kpi-collectors': 'current',    // Healthy Collectors — live health snapshot
    'kpi-source-dbs': 'current',    // Source Databases — live health snapshot
  };

  /** Resolve a KPI card subtitle.
   *  Historical KPIs return the formatted date-range label unchanged.
   *  Current-health KPIs return "As of HH:MM:SS" from the last completed
   *  refresh cycle (issue #357's lastRefreshedAt, shared with the header
   *  clock), falling back to "Current" before the first refresh completes.
   *  Pure — no DOM access — so the Node test harness can exercise it
   *  through the vm-sandbox exports.
   *  @param {string} kpiId          e.g. 'kpi-tokens' | 'kpi-collectors'
   *  @param {string} dateRangeLabel formatted range label (e.g. "Jul 1–27, 2026")
   *  @param {*}      lastRefreshedAt Date of the last completed refresh
   *                                 cycle, or null before the first one
   *  @returns {string} subtitle text */
  function kpiSubtitle(kpiId, dateRangeLabel, lastRefreshedAt) {
    if (KPI_TYPES[kpiId] !== 'current') return dateRangeLabel;
    // Duck-type the timestamp instead of instanceof Date so Dates created
    // in another realm (e.g. the Node test harness) validate correctly.
    if (lastRefreshedAt == null ||
        typeof lastRefreshedAt.getTime !== 'function' ||
        isNaN(lastRefreshedAt.getTime())) {
      return 'Current';
    }
    return 'As of ' + formatClockTime(lastRefreshedAt);
  }

  // ── API Fetch (with envelope unwrapping) ──────────────────────────────

  async function apiFetch(path) {
    const res = await fetch(path);
    if (!res.ok) {
      throw new Error('API ' + path + ' returned ' + res.status);
    }
    const json = await res.json();
    // Unwrap response envelope: {status:"ok", data: ...}
    if (json && json.status === 'ok' && 'data' in json) {
      return json.data;
    }
    return json;
  }

  // ── Data Fetching ─────────────────────────────────────────────────────

  /** Parse Agent Runs pagination from a URL query string (issue #426).
   *  Reads `page` and `page_size`; missing, malformed (non-integer), or
   *  unsupported (outside the API's limit bounds of 1–1000) values fall
   *  back to page 1 and the default page size (AGENT_RUN_LIMIT = 50).
   *  Pure — no DOM, location, or fetch access. */
  function parseAgentRunPagination(queryString) {
    var page = 1;
    var pageSize = AGENT_RUN_LIMIT;
    var params = new URLSearchParams(queryString || '');
    var rawPage = params.get('page');
    var rawPageSize = params.get('page_size');
    var nPage = Number(rawPage);
    if (rawPage !== null && Number.isInteger(nPage) && nPage >= 1) {
      page = nPage;
    }
    var nPageSize = Number(rawPageSize);
    if (rawPageSize !== null && Number.isInteger(nPageSize) &&
        nPageSize >= 1 && nPageSize <= 1000) {
      pageSize = nPageSize;
    }
    return { page: page, pageSize: pageSize };
  }

  /** Read `page`/`page_size` from the current URL into the pagination
   *  closure state (issue #426).  Called on dashboard load so a URL such
   *  as ?page=2&page_size=100 fetches the corresponding Agent Runs page;
   *  the translation happens in buildAgentRunsUrl on the next fetch. */
  function readAgentRunPaginationFromUrl() {
    var query = (typeof location !== 'undefined' && location.search) || '';
    var pagination = parseAgentRunPagination(query);
    agentRunPage = pagination.page;
    agentRunPageSize = pagination.pageSize;
  }

  /** Build the dashboard URL carrying the given pagination state, keeping
   *  any other query parameters already present in the URL. */
  function agentRunsUrlWithPagination(page, pageSize) {
    var params = new URLSearchParams(
      (typeof location !== 'undefined' && location.search) || '');
    params.set('page', String(page));
    params.set('page_size', String(pageSize));
    var path = (typeof location !== 'undefined' && location.pathname) || '';
    return path + '?' + params.toString();
  }

  /** Set the Agent Runs page and persist it in the URL via browser history
   *  (issue #426).  Invalid page values fall back to page 1.  The URL
   *  update itself never changes Agent Runs row content — rows only change
   *  through the normal fetch path (buildAgentRunsUrl → fetchAll). */
  function setAgentRunPage(page) {
    var parsed = parseAgentRunPagination('page=' + page + '&page_size=' + agentRunPageSize);
    agentRunPage = parsed.page;
    var url = agentRunsUrlWithPagination(agentRunPage, agentRunPageSize);
    if (typeof history !== 'undefined' && typeof history.pushState === 'function') {
      history.pushState({}, '', url);
    }
  }

  /** Compute the compact page-item window for the pagination control
   *  (issue #427).  Small page counts (<= 7) render every page; larger
   *  counts render the first and last pages plus a window around the
   *  current page, with ellipsis separators filling the gaps.  Pure — no
   *  DOM access — so the Node test harness exercises it directly.
   *  @param {number} currentPage the active page (clamped into range)
   *  @param {number} pageCount   total number of pages (ceil(total/size))
   *  @returns {Array<{type:'page',page:number}|{type:'ellipsis'}>} */
  function computePageItems(currentPage, pageCount) {
    if (!Number.isInteger(pageCount) || pageCount < 1) return [];
    var current = Number.isInteger(currentPage)
      ? Math.min(Math.max(currentPage, 1), pageCount)
      : 1;
    if (pageCount <= 7) {
      var all = [];
      for (var i = 1; i <= pageCount; i++) {
        all.push({ type: 'page', page: i });
      }
      return all;
    }
    // First/last pages plus a window around the current page.
    var wanted = {};
    [1, pageCount, current - 1, current, current + 1].forEach(function (p) {
      if (p >= 1 && p <= pageCount) wanted[p] = true;
    });
    var sorted = Object.keys(wanted).map(Number).sort(function (a, b) { return a - b; });
    var items = [];
    sorted.forEach(function (p, i) {
      if (i > 0 && p - sorted[i - 1] > 1) {
        items.push({ type: 'ellipsis' });
      }
      items.push({ type: 'page', page: p });
    });
    return items;
  }

  /** Build the agent runs URL from current filter state.
   *  Issue #412: when the user has NOT explicitly set From/To filter dates,
   *  from_date/to_date fall back to the shared dashboard date range
   *  (dateRangeState via resolveDateRange — the same derivation the
   *  aggregates/records URLs use), so the run list shares the KPI time
   *  window.  Re-derivation is automatic: buildAgentRunsUrl is called from
   *  fetchAll() on every refresh, and date-range changes trigger
   *  refreshDashboard() → fetchAll().  Explicit filter values (set via
   *  Apply) always win — per boundary, so an unset From/To input still
   *  inherits the dashboard range on that side.
   *  Issue #426: page state translates to the existing API pagination
   *  params — limit=page_size and offset=(page - 1) * page_size — so the
   *  API contract (limit/offset/total) is unchanged. */
  function buildAgentRunsUrl() {
    var params = [];
    var filters = agentRunFilters;
    var dateRange = resolveDateRange(dateRangeState);

    if (filters.from_date) {
      params.push('from_date=' + encodeURIComponent(filters.from_date));
    } else {
      params.push('from_date=' + encodeURIComponent(dateRange.startDate.toISOString()));
    }
    if (filters.to_date) {
      params.push('to_date=' + encodeURIComponent(filters.to_date));
    } else {
      params.push('to_date=' + encodeURIComponent(dateRange.endDate.toISOString()));
    }
    if (filters.agent) {
      params.push('agent=' + encodeURIComponent(filters.agent));
    }
    if (filters.status) {
      params.push('status=' + encodeURIComponent(filters.status));
    }
    params.push('limit=' + agentRunPageSize);
    params.push('offset=' + ((agentRunPage - 1) * agentRunPageSize));

    return '/api/v1/usage/agent-runs?' + params.join('&');
  }

  async function fetchAll() {
    var _dateRange = resolveDateRange(dateRangeState);
    const aggStart = _dateRange.startDate.toISOString();
    const aggEnd = _dateRange.endDate.toISOString();

    const results = {};
    fetchErrors = {};  // Clear previous errors

    try {
      // Build agent runs URL with current filters
      var arUrl = buildAgentRunsUrl();

      // Client metadata is cached for 10 minutes (CLIENT_CACHE_TTL_MS):
      // only fetch /admin/clients when the cache is stale — within the TTL
      // window refresh cycles reuse the cached map instead of refetching.
      // Routed through refreshClientCache() so the scheduled path shares the
      // same single-flight deduplication as the background-refresh path.
      var clientsPromise = clientCache.isExpired()
        ? refreshClientCache()
        : Promise.resolve(null);

      // Parallel fetches
      // The /api/v1/usage/sessions fetch was dropped in the merged
      // Sessions + Agent Runs view (issue #402): the merged table is driven
      // by the agent-runs endpoint (a superset), and the Sessions KPI reads
      // the aggregates total row's session_count.
      const [health, aggTotal, aggByModel, records, clients, agentRuns, aggClientProjectResult] =
        await Promise.allSettled([
          apiFetch('/health'),
          apiFetch('/api/v1/usage/aggregates?start_date=' + aggStart + '&end_date=' + aggEnd),
          apiFetch('/api/v1/usage/aggregates?start_date=' + aggStart + '&end_date=' + aggEnd + '&group_by=model'),
          apiFetch('/api/v1/usage/records?start_date=' + aggStart + '&end_date=' + aggEnd + '&limit=' + RECORD_LIMIT + '&sort_by=ingested_at&sort_dir=desc'),
          clientsPromise,
          apiFetch(arUrl),
          apiFetch('/api/v1/usage/aggregates?start_date=' + aggStart + '&end_date=' + aggEnd + '&group_by=client,project'),
        ]);

      results.health    = health.status    === 'fulfilled' ? health.value    : null;
      results.aggTotal  = aggTotal.status  === 'fulfilled' ? aggTotal.value  : null;
      results.aggByModel= aggByModel.status=== 'fulfilled' ? aggByModel.value: null;
      results.records   = records.status   === 'fulfilled' ? records.value   : null;
      results.clients   = clients.status   === 'fulfilled' ? clients.value   : null;
      results.agentRuns = agentRuns.status === 'fulfilled' ? agentRuns.value : null;
      results.aggClientProject = aggClientProjectResult.status === 'fulfilled' ? aggClientProjectResult.value : null;

      // Track per-endpoint errors
      fetchErrors = {};
      if (health.status    !== 'fulfilled') fetchErrors.health    = health.reason?.message    || 'Health check failed';
      if (aggTotal.status  !== 'fulfilled') fetchErrors.aggTotal  = aggTotal.reason?.message  || 'Aggregates (total) failed';
      if (aggByModel.status!== 'fulfilled') fetchErrors.aggByModel= aggByModel.reason?.message|| 'Aggregates (by model) failed';
      if (records.status   !== 'fulfilled') fetchErrors.records   = records.reason?.message   || 'Usage records failed';
      if (clients.status   !== 'fulfilled') fetchErrors.clients   = clients.reason?.message   || 'Clients query failed';
      agentRunsFetchError = agentRuns.status !== 'fulfilled' ? (agentRuns.reason?.message || 'Agent runs query failed') : null;
      fetchErrors.aggClientProject = aggClientProjectResult.status !== 'fulfilled' ? (aggClientProjectResult.reason?.message || 'Client/project query failed') : null;

      // Attach date range for downstream render functions
      results._dateRange = _dateRange;

      // Client cache is already refreshed by refreshClientCache() above
      // (single-flight deduped); the results.clients field is retained on
      // the results object for diagnostic visibility but the cache itself
      // is already populated — no secondary refresh needed.
    } catch (e) {
      console.error('Dashboard fetch error:', e);
      showError('Failed to fetch dashboard data: ' + e.message);
    }

    return results;
  }

  // ── Error handling ────────────────────────────────────────────────────

  function showError(msg) {
    var banner = document.getElementById('error-banner');
    if (!banner) {
      banner = document.createElement('div');
      banner.id = 'error-banner';
      banner.className = 'error-banner';
      var main = document.querySelector('.dashboard');
      if (main) main.parentNode.insertBefore(banner, main);
    }
    banner.textContent = msg;
    banner.classList.add('visible');
    setTimeout(function () {
      banner.classList.remove('visible');
    }, 8000);
  }

  // ── Rendering ─────────────────────────────────────────────────────────

  function renderHeader(data) {
    var now = new Date();
    els.timestamp.textContent = now.toLocaleString('en-US', {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hour12: false
    });

    if (data.health) {
      var h = data.health;
      els.versionFooter.textContent = h.version || '--';
      els.dbStatus.textContent = 'DB: ' + (h.database || 'unknown');
      els.dbStatus.className = 'db-status ' + (h.database === 'connected' ? 'connected' : 'disconnected');

      // Live indicator based on collector health
      var collectors = h.collectors || [];
      var healthyCount = collectors.filter(function (c) { return c.health === 'healthy'; }).length;
      var totalCollectors = collectors.length;

      var live = els.liveIndicator;
      if (totalCollectors === 0) {
        live.textContent = 'NO DATA';
        live.className = 'live-indicator error';
      } else if (healthyCount === totalCollectors) {
        live.textContent = 'LIVE';
        live.className = 'live-indicator';
      } else if (healthyCount > 0) {
        live.textContent = 'DEGRADED';
        live.className = 'live-indicator stale';
      } else {
        live.textContent = 'OFFLINE';
        live.className = 'live-indicator error';
      }
    }
  }

  /** KPI Row — per-card freshness so a single failing endpoint (e.g. aggTotal)
   *  never freezes the entire row (issue N2). */
  function renderKPIs(data) {
    // Apply per-card freshness labels (replaces the old row-level label)
    applyPanelFreshness('kpi-tokens');
    applyPanelFreshness('kpi-cost');
    applyPanelFreshness('kpi-sessions');
    applyPanelFreshness('kpi-collectors');
    applyPanelFreshness('kpi-source-dbs');

    // Compute range label once for all KPI subtitles
    var rangeLabel = '--';
    if (data._dateRange) {
      rangeLabel = formatRangeLabel(data._dateRange.startDate, data._dateRange.endDate);
    }

    // Set KPI subtitles: historical KPIs (Active Tokens, Est. Cost,
    // Sessions) show the selected date range; current-health KPIs
    // (Healthy Collectors, Source Databases) show "As of HH:MM:SS" from
    // the last completed refresh (or "Current" before any refresh) — a
    // live snapshot, never a historical aggregate (issue #358).
    els.kpiTokensDetail.textContent = kpiSubtitle('kpi-tokens', rangeLabel, lastRefreshedAt);
    els.kpiCostDetail.textContent = kpiSubtitle('kpi-cost', rangeLabel, lastRefreshedAt);
    els.kpiSessionsDetail.textContent = kpiSubtitle('kpi-sessions', rangeLabel, lastRefreshedAt);
    els.kpiCollectorsDetail.textContent = kpiSubtitle('kpi-collectors', rangeLabel, lastRefreshedAt);
    els.kpiSourceDbsDetail.textContent = kpiSubtitle('kpi-source-dbs', rangeLabel, lastRefreshedAt);

    // Total tokens from aggregates total row — gated on kpi-tokens (and
    // kpi-cost, which shares the aggTotal endpoint).
    if (shouldRenderPanel(panelStates, 'kpi-tokens')) {
      if (data.aggTotal && data.aggTotal.length > 0) {
        var t = data.aggTotal[0];
        var totalTokens = (t.total_input_tokens || 0) + (t.total_output_tokens || 0);
        els.kpiTokens.textContent = fmtNum(totalTokens);
        els.kpiCost.textContent = fmtCost(t.total_estimated_cost_usd);
      }
    }

    // Sessions from the aggregates total row — gated on kpi-sessions card
    // freshness.  The merged Sessions + Agent Runs view (issue #402) no
    // longer fetches /api/v1/usage/sessions; the aggregates total row already
    // carries the range-scoped COUNT(DISTINCT session_id), so the KPI keeps
    // its date-range semantics from an already-fetched endpoint.
    if (shouldRenderPanel(panelStates, 'kpi-sessions')) {
      if (data.aggTotal && data.aggTotal.length > 0) {
        els.kpiSessions.textContent = fmtNum(data.aggTotal[0].session_count || 0);
      }
    }

    // Collectors from health — gated on kpi-collectors card freshness
    if (shouldRenderPanel(panelStates, 'kpi-collectors')) {
      if (data.health) {
        var collectors = data.health.collectors || [];
        var healthyCol = collectors.filter(function (c) { return c.health === 'healthy'; }).length;
        els.kpiCollectors.textContent = healthyCol + ' / ' + collectors.length;
      }
    }

    // Source DBs from health — gated on kpi-source-dbs card freshness
    if (shouldRenderPanel(panelStates, 'kpi-source-dbs')) {
      if (data.health) {
        var srcDbs = data.health.source_databases || [];
        els.kpiSourceDbs.textContent = fmtNum(srcDbs.length);
      }
    }
  }

  /** Model Mix — horizontal bar chart */
  function renderModelMix(data) {
    applyPanelFreshness('model-mix');
    if (!shouldRenderPanel(panelStates, 'model-mix')) return; // failed fetch → keep previous chart

    var models = data.aggByModel || [];
    if (models.length === 0) {
      els.modelMixChart.innerHTML = '<p class="empty-state">No model data available' + errorIndicator('aggByModel') + '</p>';
      return;
    }

    // Sort by total tokens descending
    models.sort(function (a, b) {
      var at = (a.total_input_tokens || 0) + (a.total_output_tokens || 0);
      var bt = (b.total_input_tokens || 0) + (b.total_output_tokens || 0);
      return bt - at;
    });

    // Compute max for bar widths
    var maxTokens = 0;
    models.forEach(function (m) {
      var t = (m.total_input_tokens || 0) + (m.total_output_tokens || 0);
      if (t > maxTokens) maxTokens = t;
    });

    var html = '';
    models.forEach(function (m, i) {
      var tokens = (m.total_input_tokens || 0) + (m.total_output_tokens || 0);
      var pct = maxTokens > 0 ? (tokens / maxTokens * 100) : 0;
      var ci = i % 8; // cycle through 8 gradient classes
      html += '<div class="chart-bar-row">' +
        '<span class="chart-bar-label" title="' + escHtml(m.group_value) + '">' + escHtml(m.group_value) + '</span>' +
        '<div class="chart-bar-track"><div class="chart-bar-fill c' + ci + '" style="width:' + pct.toFixed(1) + '%"></div></div>' +
        '<span class="chart-bar-value">' + fmtNum(tokens) + '</span>' +
        '</div>';
    });

    els.modelMixChart.innerHTML = html;
  }

  /** Operational Events Feed */
  function renderLiveEvents(data) {
    applyPanelFreshness('events');
    if (!shouldRenderPanel(panelStates, 'events')) return; // failed fetch → keep previous events

    var events = [];
    var now = new Date().toISOString();

    if (!data.health) {
      els.eventsFeed.innerHTML = '<p class="empty-state">No health data — events unavailable' + errorIndicator('health') + '</p>';
      els.eventBadge.textContent = '--';
      els.eventBadge.className = 'event-badge empty';
      return;
    }

    var collectors = data.health.collectors || [];
    var srcDbs = data.health.source_databases || [];
    var lastIngest = data.health.last_ingest_timestamp;

    // Stale collector warnings
    collectors.forEach(function (c) {
      if (c.health === 'stale') {
        events.push({
          type: 'stale',
          icon: '\u26A0',  // ⚠
          text: 'Collector <strong>' + escHtml(c.client_name) + '</strong> is <em>stale</em> — last seen ' + fmtRelative(c.last_heartbeat),
          time: c.last_heartbeat || now
        });
      } else if (c.health === 'unknown') {
        events.push({
          type: 'info',
          icon: '\u2139',  // ℹ
          text: 'Collector <strong>' + escHtml(c.client_name) + '</strong> has never reported',
          time: now
        });
      }
    });

    // Stale source DB warnings
    srcDbs.forEach(function (d) {
      if (d.health === 'stale' || d.health === 'unknown') {
        events.push({
          type: d.health === 'stale' ? 'stale' : 'info',
          icon: d.health === 'stale' ? '\u26A0' : '\u2139',
          text: 'Source DB <strong>' + escHtml(d.client_name) + '</strong> is <em>' + d.health + '</em> — last push ' + fmtRelative(d.last_push),
          time: d.last_push || now
        });
      }
    });

    // Last ingest timestamp
    if (lastIngest) {
      var ingestAge = (new Date() - new Date(lastIngest)) / 60000; // minutes
      if (ingestAge > 60) {
        events.push({
          type: 'alert',
          icon: '\u274C',  // ❌
          text: 'Last ingest was ' + fmtRelative(lastIngest) + ' — sync recovery may be needed',
          time: lastIngest
        });
      }
    } else if (collectors.length > 0) {
      events.push({
        type: 'info',
        icon: '\u2139',
        text: 'No ingest batches recorded yet',
        time: now
      });
    }

    // Also add high-usage agent runs as alerts (merged view, issue #402:
    // the agent-runs response supersedes the dropped sessions fetch)
    if (data.agentRuns && data.agentRuns.items) {
      data.agentRuns.items.slice(0, 5).forEach(function (r) {
        var tokens = (r.total_input_tokens || 0) + (r.total_output_tokens || 0);
        if (tokens > 100000) {
          var label = ensureClientName(r.client_id) || r.client_id;
          events.push({
            type: 'info',
            icon: '\uD83D\uDCCA',  // 📊
            text: 'High-usage run: <strong>' + escHtml(label) + '</strong> — ' + fmtNum(tokens) + ' tokens',
            time: r.last_updated_at || now
          });
        }
      });
    }

    // Sort events newest first
    events.sort(function (a, b) { return new Date(b.time) - new Date(a.time); });

    // Limit to 15
    events = events.slice(0, 15);

    if (events.length === 0) {
      els.eventsFeed.innerHTML = '<p class="empty-state">All systems nominal</p>';
      els.eventBadge.textContent = '0';
      els.eventBadge.className = 'event-badge empty';
      return;
    }

    // Count alerts/stale
    var alertCount = events.filter(function (e) { return e.type === 'alert' || e.type === 'stale'; }).length;
    els.eventBadge.textContent = alertCount > 0 ? alertCount : '0';
    els.eventBadge.className = alertCount > 0 ? 'event-badge' : 'event-badge empty';

    var html = '';
    events.forEach(function (e) {
      html += '<div class="event-item ' + e.type + '">' +
        '<span class="event-icon">' + e.icon + '</span>' +
        '<div><div class="event-text">' + e.text + '</div>' +
        '<div class="event-time">' + fmtDT(e.time) + '</div></div>' +
        '</div>';
    });

    els.eventsFeed.innerHTML = html;
  }

  /** Collector Distribution — health bar per collector */
  function renderCollectorDistribution(data) {
    applyPanelFreshness('collector-dist');
    if (!shouldRenderPanel(panelStates, 'collector-dist')) return; // failed fetch → keep previous bars

    if (!data.health || !data.health.collectors || data.health.collectors.length === 0) {
      els.collectorDist.innerHTML = '<p class="empty-state">No collectors registered' + errorIndicator('health') + '</p>';
      return;
    }

    var collectors = data.health.collectors;
    var maxRecords = 0;
    collectors.forEach(function (c) {
      if (c.total_records_ingested > maxRecords) maxRecords = c.total_records_ingested;
    });

    var html = '';
    collectors.forEach(function (c) {
      var pct = maxRecords > 0 ? (c.total_records_ingested / maxRecords * 100) : 0;
      var healthWidth = c.health === 'healthy' ? 100 : c.health === 'stale' ? 40 : 20;
      html += '<div class="dist-row">' +
        '<span class="dist-name" title="' + escHtml(c.client_name) + '">' + escHtml(c.client_name) + '</span>' +
        '<div class="dist-bar-track">' +
          '<div class="dist-bar-healthy" style="width:' + (c.health === 'healthy' ? Math.max(pct, 5) : 0) + '%"></div>' +
          '<div class="dist-bar-stale" style="width:' + (c.health === 'stale' ? Math.max(pct * 0.3, 3) : 0) + '%"></div>' +
          '<div class="dist-bar-unknown" style="width:' + (c.health === 'unknown' ? Math.max(pct * 0.1, 2) : 0) + '%"></div>' +
        '</div>' +
        '<span class="dist-tokens">' + fmtNum(c.total_records_ingested) + ' recs</span>' +
        '</div>';
    });

    els.collectorDist.innerHTML = html;
  }

  /** Collectors Table */
  function renderCollectorsTable(data) {
    applyPanelFreshness('collectors');
    if (!shouldRenderPanel(panelStates, 'collectors')) return; // failed fetch → keep previous rows

    if (!data.health || !data.health.collectors || data.health.collectors.length === 0) {
      els.collectorsTbody.innerHTML = '<tr><td colspan="4" class="empty-state">No collectors' + errorIndicator('health') + '</td></tr>';
      return;
    }

    var html = '';
    data.health.collectors.forEach(function (c) {
      var badgeCls = 'badge-' + c.health;
      html += '<tr>' +
        '<td>' + escHtml(c.client_name) + '</td>' +
        '<td>' + badge(c.health, badgeCls).outerHTML + '</td>' +
        '<td>' + fmtRelative(c.last_heartbeat) + '</td>' +
        '<td>' + fmtNum(c.total_records_ingested) + '</td>' +
        '</tr>';
    });

    els.collectorsTbody.innerHTML = html;
  }

  /** Agents & LLMs In Use */
  function renderAgentsTable(data) {
    applyPanelFreshness('agents');
    if (!shouldRenderPanel(panelStates, 'agents')) return; // failed fetch → keep previous rows

    if (!data.aggByModel || data.aggByModel.length === 0) {
      els.agentsTbody.innerHTML = '<tr><td colspan="6" class="empty-state">No agent data' + errorIndicator('aggByModel') + '</td></tr>';
      return;
    }

    // We use the by-model aggregates.  Each row is a model.
    // Agent name = "All" or derived from client grouping if available.
    var html = '';
    data.aggByModel.forEach(function (m) {
      var modelName = m.group_value || 'unknown';
      var provider = deriveProvider(modelName);
      var tokens = (m.total_input_tokens || 0) + (m.total_output_tokens || 0);
      var cost = m.total_estimated_cost_usd;
      var requests = m.record_count || 0;
      var status = requests > 0 ? 'active' : 'inactive';

      // Try to associate with a client/collector health status
      if (data.health && data.health.collectors) {
        var hasHealthy = data.health.collectors.some(function (c) { return c.health === 'healthy'; });
        status = hasHealthy ? 'active' : status;
      }

      html += '<tr>' +
        '<td>' + escHtml(provider) + '</td>' +
        '<td>' + escHtml(modelName) + '</td>' +
        '<td>' + fmtNum(requests) + '</td>' +
        '<td>' + fmtNum(tokens) + '</td>' +
        '<td>' + fmtCost(cost) + '</td>' +
        '<td>' + badge(status, 'badge-' + status).outerHTML + '</td>' +
        '</tr>';
    });

    els.agentsTbody.innerHTML = html;
  }

  /** Agent Runs Table — the merged Sessions + Agent Runs dashboard table
   *  (issue #402).  Driven by /api/v1/usage/agent-runs (a superset of the
   *  sessions list: session_title, model, currentStatus, token breakdown,
   *  total_estimated_cost_usd); rows open the /agent-runs/{id} detail
   *  overlay.  The separate sessions table, its /sessions fetch, and the
   *  active/idle badge heuristic are gone — status renders from the run's
   *  currentStatus semantics (falling back to status) via statusBadgeClass.
   *  Cells carry the responsive hooks: data-label on every cell (≤760px
   *  stacked rows) and ar-col-low on the low-priority columns hidden at
   *  761–1024px tablet widths. */
  function renderAgentRunsTable(data) {
    applyPanelFreshness('agent-runs');
    if (!shouldRenderPanel(panelStates, 'agent-runs')) return; // failed fetch → keep previous rows

    var runs = data && data.items;
    if (!runs || runs.length === 0) {
      var errSuffix = agentRunsFetchError
        ? ' <span class="fetch-error" title="' + escHtml(agentRunsFetchError) + '">\u26A0 Fetch error</span>'
        : '';
      els.arTbody.innerHTML = '<tr><td colspan="11" class="empty-state">No agent runs' + errSuffix + '</td></tr>';
      return;
    }

    var html = '';
    runs.forEach(function (r) {
      var todoProgress = fmtTodoProgress(r.todo_completed, r.todo_total);
      var projectStr = fmtProjectLabel(r);
      var statusCls = statusBadgeClass(r.currentStatus || r.status);
      var displayTitle = r.session_title || r.title || '(untitled)';
      // Last Updated cell (issue #5): year-inclusive absolute local
      // timestamp (issue #4 formatter) as the primary value, with the
      // relative label as muted secondary text after a middot separator
      // ('·') so the two values read as distinct.  Missing/unparseable
      // timestamps render a bare '--' (no secondary).
      var lastUpdatedAbs = formatAgentRunTimestamp(r.last_updated_at);
      var lastUpdatedCell = lastUpdatedAbs === '--'
        ? '--'
        : lastUpdatedAbs + ' · <span class="ar-rel-time">' + fmtRelative(r.last_updated_at) + '</span>';

      html += '<tr class="ar-row" data-id="' + r.id + '" tabindex="0">' +
        '<td class="clickable ar-title" data-label="Title">' + escHtml(displayTitle) + '</td>' +
        '<td data-label="Status">' + badge(r.currentStatus || r.status, statusCls).outerHTML + '</td>' +
        '<td class="ar-col-low" data-label="Agent">' + escHtml(r.agent || '--') + '</td>' +
        '<td data-label="Model">' + fmtModel(r.model) + '</td>' +
        '<td data-label="Project / Worktree">' + escHtml(projectStr) + '</td>' +
        '<td class="ar-col-low" data-label="Todo">' + todoProgress + '</td>' +
        '<td class="ar-col-low" data-label="Files">' + fmtCodeChanges(r.code_changes_total) + '</td>' +
        '<td data-label="Cost">' + fmtCost(r.total_estimated_cost_usd) + '</td>' +
        '<td data-label="Tokens">' + fmtAgentRunTokens(r.total_input_tokens, r.total_output_tokens, r.total_cache_read_tokens, r.total_cache_write_tokens) + '</td>' +
        '<td data-label="Last Updated">' + lastUpdatedCell + '</td>' +
        '<td class="ar-col-low" data-label="Children">' + (r.child_run_count || 0) + '</td>' +
        '</tr>';
    });

    els.arTbody.innerHTML = html;

    // Attach click + keyboard handlers for the detail overlay
    var rows = els.arTbody.querySelectorAll('.ar-row');
    rows.forEach(function (row) {
      row.addEventListener('click', function () {
        var id = row.getAttribute('data-id');
        if (id) openAgentRunDetail(id);
      });
      // Keyboard activation: Enter or Space on a focused row opens the detail overlay
      row.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          var id = row.getAttribute('data-id');
          if (id) openAgentRunDetail(id);
        }
      });
    });
  }

  /** Render Client/Project Usage Breakdown two-level expandable table */
  function renderClientProjectBreakdown(data) {
    applyPanelFreshness('client-project');
    if (!shouldRenderPanel(panelStates, 'client-project')) return; // failed fetch → keep previous rows

    var rows = data && data.aggClientProject || [];

    // Update panel subtitle with formatted range label
    if (els.cpPanelSubtitle && data._dateRange) {
      els.cpPanelSubtitle.textContent = formatRangeLabel(data._dateRange.startDate, data._dateRange.endDate);
    }

    if (rows.length === 0) {
      var errSuffix = errorIndicator('aggClientProject');
      els.cpTbody.innerHTML = '<tr><td colspan="5" class="empty-state">No data available' + errSuffix + '</td></tr>';
      return;
    }

    // Parse flat pipe-delimited rows into a two-level client→projects tree
    var clientMap = {}; // client_name → { rows: [project_rows], totals: {...} }

    rows.forEach(function (r) {
      var parts = (r.group_value || '').split('|');
      var clientName = parts[0] || 'Unknown';
      var projectLabel = parts[1] || 'unknown';

      if (!clientMap[clientName]) {
        clientMap[clientName] = {
          projectRows: [],
          totalTokens: 0,
          totalCost: 0,
          totalSessions: 0,
          totalModels: 0
        };
      }

      var tokens = (r.total_input_tokens || 0) + (r.total_output_tokens || 0);
      var projectRow = {
        projectId: projectLabel,
        projectLabel: r.project_label || projectLabel,
        tokens: tokens,
        cost: r.total_estimated_cost_usd,
        sessions: r.session_count || 0,
        models: r.model_count || 0
      };

      clientMap[clientName].projectRows.push(projectRow);
      clientMap[clientName].totalTokens += tokens;
      clientMap[clientName].totalCost += (r.total_estimated_cost_usd || 0);
      clientMap[clientName].totalSessions += (r.session_count || 0);
      clientMap[clientName].totalModels += (r.model_count || 0);
    });

    // Sort clients by totalTokens descending
    var clientNames = Object.keys(clientMap).sort(function (a, b) {
      return clientMap[b].totalTokens - clientMap[a].totalTokens;
    });

    var html = '';
    var rowId = 0;
    clientNames.forEach(function (clientName) {
      var c = clientMap[clientName];
      var cid = 'cp-client-' + (rowId++);

      // Sort project rows by tokens descending
      c.projectRows.sort(function (a, b) {
        return b.tokens - a.tokens;
      });

      html += '<tr class="cp-client-row" data-cp-id="' + cid + '">' +
        '<td><span class="cp-expand-icon" id="' + cid + '-icon">&#9654;</span>' + escHtml(clientName) + '</td>' +
        '<td>' + fmtNum(c.totalTokens) + '</td>' +
        '<td>' + fmtCost(c.totalCost) + '</td>' +
        '<td>' + fmtNum(c.totalSessions) + '</td>' +
        '<td>' + fmtNum(c.totalModels) + '</td>' +
        '</tr>';

      // Project sub-rows (hidden by default)
      c.projectRows.forEach(function (p) {
        html += '<tr class="cp-project-row" data-cp-parent="' + cid + '" style="display:none">' +
          '<td>' + escHtml(resolveProjectLabel(p)) + '</td>' +
          '<td>' + fmtNum(p.tokens) + '</td>' +
          '<td>' + fmtCost(p.cost) + '</td>' +
          '<td>' + fmtNum(p.sessions) + '</td>' +
          '<td>' + fmtNum(p.models) + '</td>' +
          '</tr>';
      });
    });

    // Reset drilldown state if the date range context changed
    var currentDateRangeKey = JSON.stringify(dateRangeState);
    if (currentDateRangeKey !== _lastDateRangeKey) {
      expandedClientNames = {};
      _lastDateRangeKey = currentDateRangeKey;
    }

    // Capture expanded state before re-render (keyed by client name, not row position)
    var expandedIcons = els.cpTbody.querySelectorAll('.cp-expand-icon.expanded');
    expandedClientNames = {};
    expandedIcons.forEach(function (icon) {
      var row = icon.closest('.cp-client-row');
      if (row) {
        var nameTd = row.querySelector('td');
        if (nameTd) {
          var clientName = nameTd.textContent.replace('\u25B6', '').replace('\u25BC', '').trim();
          if (clientName) expandedClientNames[clientName] = true;
        }
      }
    });

    els.cpTbody.innerHTML = html;

    // Attach expand/collapse handlers
    var clientRows = els.cpTbody.querySelectorAll('.cp-client-row');
    clientRows.forEach(function (row) {
      row.addEventListener('click', function () {
        var id = row.getAttribute('data-cp-id');
        var icon = document.getElementById(id + '-icon');
        var subRows = els.cpTbody.querySelectorAll('[data-cp-parent="' + id + '"]');

        var isExpanded = icon && icon.classList.contains('expanded');
        subRows.forEach(function (sr) {
          sr.style.display = isExpanded ? 'none' : '';
        });
        if (icon) {
          icon.classList.toggle('expanded');
          icon.innerHTML = isExpanded ? '&#9654;' : '&#9660;';
        }
      });
    });

    // Restore expanded clients after re-render
    var allClientRows = els.cpTbody.querySelectorAll('.cp-client-row');
    allClientRows.forEach(function (row) {
      var nameTd = row.querySelector('td');
      if (nameTd) {
        var clientName = nameTd.textContent.replace('\u25B6', '').replace('\u25BC', '').trim();
        if (expandedClientNames[clientName]) {
          row.click();
        }
      }
    });
  }
  async function openAgentRunDetail(sessionId) {
    // Show overlay
    els.arDetailOverlay.classList.add('visible');
    els.arDetailBody.innerHTML = '<p class="empty-state">Loading detail&hellip;</p>';
    els.arDetailTitle.textContent = 'Agent Run Detail';

    try {
      var data = await apiFetch('/api/v1/usage/agent-runs/' + encodeURIComponent(sessionId));
      agentRunDetail = data;
      renderAgentRunDetail(data);
    } catch (e) {
      els.arDetailBody.innerHTML = '<p class="empty-state">Failed to load detail: ' + escHtml(e.message) + '</p>';
      console.error('Agent run detail fetch error:', e);
    }
  }

  /** Render Agent Run Detail Panel */
  function renderAgentRunDetail(d) {
    if (!d) {
      els.arDetailBody.innerHTML = '<p class="empty-state">No detail data available</p>';
      return;
    }

    els.arDetailTitle.textContent = escHtml(d.title || 'Agent Run Detail');

    var tokens = (d.total_input_tokens || 0) + (d.total_output_tokens || 0);
    var duration = fmtDuration(d.first_message_at, d.last_message_at);
    var projectStr = fmtProjectLabel(d);
    var statusCls = statusBadgeClass(d.currentStatus || d.status);

    // ── Session Metadata ──
    var html = '<div class="detail-section">' +
      '<div class="detail-section-title">Session Metadata</div>' +
      '<div class="detail-grid">' +
        fieldHtml('Status', badge(d.currentStatus || d.status, statusCls).outerHTML) +
        fieldHtml('Title', escHtml(d.title || '--')) +
        fieldHtml('Internal ID', shortUUID(d.id)) +
        fieldHtml('External ID', escHtml(d.external_session_id || '--')) +
        fieldHtml('Client ID', shortUUID(d.client_id)) +
        fieldHtml('Source DB', shortUUID(d.source_database_id)) +
        fieldHtml('Messages', d.message_count != null ? fmtNum(d.message_count) : '--') +
        fieldHtml('Duration', duration) +
        fieldHtml('Last Updated', formatAgentRunTimestamp(d.last_updated_at)) +
      '</div></div>';

    // ── Agent & Project ──
    html += '<div class="detail-section">' +
      '<div class="detail-section-title">Agent &amp; Project</div>' +
      '<div class="detail-grid">' +
        fieldHtml('Agent', escHtml(d.agent || '--')) +
        fieldHtml('Project / Worktree', escHtml(projectStr)) +
      '</div></div>';

    // ── Parent Run ──
    html += '<div class="detail-section">' +
      '<div class="detail-section-title">Parent Run</div>';
    if (d.parent_session_id) {
      var parentStr = escHtml(d.parent_session_id);
      if (d.parent_internal_id) {
        parentStr += ' <span class="detail-field-label">(internal: ' + shortUUID(d.parent_internal_id) + ')</span>';
      }
      html += '<div class="detail-field-value">' + parentStr + '</div>';
    } else {
      html += '<div class="detail-field-value" style="color:var(--text-muted)">No parent run</div>';
    }
    html += '</div>';

    // ── Child Runs ──
    html += '<div class="detail-section">' +
      '<div class="detail-section-title">Child Runs (' + (d.child_summaries ? d.child_summaries.length : 0) + ')</div>';
    if (d.child_summaries && d.child_summaries.length > 0) {
      html += '<div class="detail-child-list">';
      d.child_summaries.forEach(function (c) {
        var cStatusCls = statusBadgeClass(c.currentStatus || c.status);
        html += '<div class="detail-child-item">' +
          '<span>' + shortUUID(c.id) + '</span>' +
          '<span>' + badge(c.currentStatus || c.status, cStatusCls).outerHTML + '</span>' +
          '<span style="color:var(--text-primary)">' + escHtml(c.agent || '--') + '</span>' +
          '<span style="color:var(--text-muted)">' + (c.message_count || 0) + ' msgs</span>' +
          '</div>';
      });
      html += '</div>';
    } else {
      html += '<div class="detail-field-value" style="color:var(--text-muted)">No child runs</div>';
    }
    html += '</div>';

    // ── Todos ──
    html += '<div class="detail-section">' +
      '<div class="detail-section-title">Todos (' + fmtTodoProgress(d.todo_completed, d.todo_total) + ')</div>';
    if (d.todo_rows && d.todo_rows.length > 0) {
      html += '<div class="detail-todo-list">';
      d.todo_rows.forEach(function (t) {
        var iconCls = t.status || 'pending';
        var iconMap = { completed: '\u2713', blocked: '\u2717', in_progress: '\u25D4', pending: '\u25CB' };
        var icon = iconMap[iconCls] || '\u25CB';
        var priorityMark = t.priority
          ? ' <span class="detail-todo-priority">[' + escHtml(t.priority) + ']</span>'
          : '';
        html += '<div class="detail-todo-item">' +
          '<span class="detail-todo-icon ' + iconCls + '">' + icon + '</span>' +
          '<span>' + escHtml(t.description) + priorityMark + '</span>' +
          '</div>';
      });
      html += '</div>';
    } else {
      html += '<div class="detail-field-value" style="color:var(--text-muted)">No todos recorded</div>';
    }
    html += '</div>';

    // ── Usage Totals ──
    html += '<div class="detail-section">' +
      '<div class="detail-section-title">Usage Totals</div>' +
      '<div class="detail-grid">' +
        fieldHtml('Input Tokens', fmtNum(d.total_input_tokens)) +
        fieldHtml('Output Tokens', fmtNum(d.total_output_tokens)) +
        fieldHtml('Cache Read Tokens', fmtNum(d.total_cache_read_tokens)) +
        fieldHtml('Cache Write Tokens', fmtNum(d.total_cache_write_tokens)) +
        fieldHtml('Active Tokens', fmtNum(tokens)) +
        fieldHtml('Est. Cost', fmtCost(d.total_estimated_cost_usd)) +
        fieldHtml('Code Changes', fmtCodeChanges(d.code_changes_total)) +
      '</div></div>';

    // ── Drill-down Link ──
    if (d.loki_search_url) {
      html += '<div class="detail-section">' +
        '<a href="' + escHtml(d.loki_search_url) + '" target="_blank" rel="noopener" class="detail-loki-link">' +
        '\u2197 Open in Grafana Explore</a></div>';
    }

    // ── Session Context ──
    var ctx = d.session_context || {};
    html += '<div class="detail-section">' +
      '<div class="detail-section-title">Session Context</div>' +
      '<div class="detail-grid">' +
        fieldHtml('Title', escHtml(ctx.title || '--')) +
        fieldHtml('Model', escHtml(ctx.session_model || '--')) +
        fieldHtml('Source Directory', escHtml(ctx.source_directory || '--')) +
        fieldHtml('Source Path', escHtml(ctx.source_path || '--')) +
        fieldHtml('Additions', ctx.code_change_additions != null ? fmtNum(ctx.code_change_additions) : '--') +
        fieldHtml('Deletions', ctx.code_change_deletions != null ? fmtNum(ctx.code_change_deletions) : '--') +
      '</div></div>';

    els.arDetailBody.innerHTML = html;
  }

  /** Helper: build a detail grid field row */
  function fieldHtml(label, value) {
    return '<div class="detail-field">' +
      '<span class="detail-field-label">' + label + '</span>' +
      '<span class="detail-field-value">' + value + '</span>' +
      '</div>';
  }

  // ── HTML-escape utility ───────────────────────────────────────────────

  function escHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /**
   * Return a small error indicator HTML snippet if the given endpoint
   * had a fetch error, so the user can distinguish "no data" from "fetch failed".
   */
  function errorIndicator(endpointKey) {
    if (fetchErrors[endpointKey]) {
      return ' <span class="fetch-error" title="' + escHtml(fetchErrors[endpointKey]) + '">\u26A0 Fetch error</span>';
    }
    return '';
  }

  // ── Panel freshness (DOM wiring) ──────────────────────────────────────
  // Each instrumented panel carries a <span class="panel-freshness"
  // id="freshness-<panelId>"> in its title row (index.html).  The label is
  // small muted text rendered in the title's existing flex row, so it never
  // changes panel layout — no spinners, no overlays, no reflow.

  /** Render a panel's freshness label into its header span. */
  function applyPanelFreshness(panelId) {
    var el = document.getElementById('freshness-' + panelId);
    if (!el) return;
    var f = computePanelFreshness(panelStates, panelId, Date.now());
    if (!f) {
      el.textContent = '';
      el.className = 'panel-freshness';
      return;
    }
    el.textContent = f.label;
    el.className = 'panel-freshness ' + f.cssClass;
  }

  /** Record a panel state and repaint its freshness label. */
  function setPanelState(panelId, status, updatedAt) {
    panelStates[panelId] = { status: status, updatedAt: updatedAt };
    applyPanelFreshness(panelId);
  }

  /** Mark every panel 'refreshing' at the start of a refresh cycle. */
  function markAllPanelsRefreshing() {
    Object.keys(PANEL_ENDPOINTS).forEach(function (panelId) {
      var prev = panelStates[panelId];
      setPanelState(panelId, 'refreshing', prev ? prev.updatedAt : null);
    });
  }

  /** Resolve every panel to 'ok'/'stale' from the cycle's fetch errors.
   *  A failed panel keeps its previous updatedAt (data on screen is the
   *  previous successful render); a successful one records the cycle time. */
  function resolvePanelStatesAfterFetch() {
    var errors = Object.assign({}, fetchErrors, { agentRuns: agentRunsFetchError });
    var statuses = resolvePanelStatuses(errors);
    var nowMs = Date.now();
    Object.keys(PANEL_ENDPOINTS).forEach(function (panelId) {
      var prev = panelStates[panelId];
      if (statuses[panelId] === 'stale') {
        setPanelState(panelId, 'stale', prev ? prev.updatedAt : null);
      } else {
        setPanelState(panelId, 'ok', nowMs);
      }
    });
  }

  /** Repaint the header "Last refreshed HH:MM:SS" clock. */
  function updateLastRefreshed() {
    if (!els.lastRefreshed) return;
    els.lastRefreshed.textContent =
      'Last refreshed ' + (lastRefreshedAt ? formatClockTime(lastRefreshedAt) : '--');
  }

  // ── Orchestration ─────────────────────────────────────────────────────

  async function refreshDashboard() {
    try {
      if (els.dashboard) els.dashboard.classList.add('refreshing');
      markAllPanelsRefreshing(); // "Refreshing…" on every panel while the cycle is in flight
      var data = await fetchAll();
      resolvePanelStatesAfterFetch(); // per-panel ok/stale from this cycle's fetch errors
      // Record the cycle timestamp BEFORE rendering so the KPI "As of"
      // subtitle and any other render-time consumers see the CURRENT
      // cycle's time, not the previous one (fixes one-cycle lag).
      lastRefreshedAt = new Date();    // data on screen is from this cycle
      renderHeader(data);
      renderKPIs(data);
      renderModelMix(data);
      renderLiveEvents(data);
      renderCollectorDistribution(data);
      renderCollectorsTable(data);
      renderAgentsTable(data);
      renderAgentRunsTable(data.agentRuns);
      renderAgentRunPagination(data.agentRuns); // pagination control below the panel (issue #427)
      renderClientProjectBreakdown(data);
    } catch (e) {
      console.error('Dashboard refresh failed:', e);
      showError('Dashboard refresh error: ' + e.message);
    } finally {
      if (els.dashboard) els.dashboard.classList.remove('refreshing');
      // Repaint the header clock — lastRefreshedAt was set before the
      // render pass above so the header displays the current cycle time.
      updateLastRefreshed();
    }
  }

  // ── Agent Run Filter Handlers ──────────────────────────────────────────

  function readFiltersFromUI() {
    var filters = {};
    if (els.arFilterFrom && els.arFilterFrom.value) {
      filters.from_date = els.arFilterFrom.value + 'T00:00:00Z';
    }
    if (els.arFilterTo && els.arFilterTo.value) {
      filters.to_date = els.arFilterTo.value + 'T23:59:59Z';
    }
    if (els.arFilterAgent && els.arFilterAgent.value) {
      filters.agent = els.arFilterAgent.value.trim();
    }
    if (els.arFilterStatus && els.arFilterStatus.value) {
      filters.status = els.arFilterStatus.value;
    }
    return filters;
  }

  function applyFilters() {
    agentRunFilters = readFiltersFromUI();
    fetchAgentRunsAndRender();
  }

  /** Fetch the Agent Runs page described by the current filter + pagination
   *  state and re-render the table and pagination controls (issue #427).
   *  Shared by the Apply/Clear filter path and the pagination control
   *  clicks, so paging always preserves the active filters: buildAgentRunsUrl
   *  carries from_date/to_date/agent/status alongside the page-derived
   *  limit/offset (issue #426). */
  function fetchAgentRunsAndRender() {
    // Track the agent-runs panel freshness for this independent fetch
    var prev = panelStates['agent-runs'];
    setPanelState('agent-runs', 'refreshing', prev ? prev.updatedAt : null);
    // Re-fetch agent runs with current filters + page state, update table
    var url = buildAgentRunsUrl();
    apiFetch(url).then(function (data) {
      agentRunsData = data;
      agentRunsFetchError = null;
      setPanelState('agent-runs', 'ok', Date.now());
      renderAgentRunsTable(data);
      renderAgentRunPagination(data);
    }).catch(function (e) {
      agentRunsFetchError = e.message || 'Agent runs query failed';
      var prevState = panelStates['agent-runs'];
      setPanelState('agent-runs', 'stale', prevState ? prevState.updatedAt : null);
      renderAgentRunsTable(null); // keeps previous rows; label shows "Showing previous data"
      renderAgentRunPagination(agentRunsData); // keeps the last-known page info
      console.error('Agent runs filter fetch error:', e);
    });
  }

  /** Render the Agent Runs pagination control block below the panel
   *  (issue #427): Previous / Next plus the numbered page items computed by
   *  computePageItems from the API response `total` and the current page
   *  size.  Previous is disabled on page 1, Next on the final page, and the
   *  current page carries aria-current="page".  Clicking a control persists
   *  the page via setAgentRunPage (issue #426) and re-fetches that server-
   *  side page through fetchAgentRunsAndRender, preserving active filters.
   *  Agent Runs row content, columns, ordering, and detail interactions are
   *  untouched — this block only re-requests the same endpoint with a
   *  different offset. */
  function renderAgentRunPagination(data) {
    if (!els.arPagination) return;
    // Failed fetch → keep the previous control state (mirrors the table's
    // "keep previous rows" behavior via the same panel guard).
    if (!shouldRenderPanel(panelStates, 'agent-runs')) return;

    var total = (data && typeof data.total === 'number')
      ? data.total
      : (data && data.items ? data.items.length : 0);
    var pageCount = Math.ceil(total / agentRunPageSize);
    var items = computePageItems(agentRunPage, pageCount);
    if (items.length === 0) {
      els.arPagination.innerHTML = '';
      return;
    }

    var html = '';
    var prevPage = agentRunPage - 1;
    var nextPage = agentRunPage + 1;

    html += '<button type="button" class="filter-clear pagination-btn" data-page="' + prevPage + '"' +
      (prevPage < 1 ? ' disabled' : '') + ' aria-label="Previous page">\u2190 Previous</button>';

    items.forEach(function (item) {
      if (item.type === 'ellipsis') {
        html += '<span class="pagination-ellipsis" aria-hidden="true">\u2026</span>';
        return;
      }
      var isCurrent = item.page === agentRunPage;
      html += '<button type="button" class="filter-clear pagination-btn' +
        (isCurrent ? ' pagination-current' : '') + '" data-page="' + item.page + '"' +
        ' aria-label="' + (isCurrent ? 'Page ' + item.page + ', current page' : 'Page ' + item.page) + '"' +
        (isCurrent ? ' aria-current="page"' : '') + '>' + item.page + '</button>';
    });

    html += '<button type="button" class="filter-clear pagination-btn" data-page="' + nextPage + '"' +
      (nextPage > pageCount ? ' disabled' : '') + ' aria-label="Next page">Next \u2192</button>';

    els.arPagination.innerHTML = html;

    // Wire the page controls: selecting a page updates the pagination
    // state (setAgentRunPage → URL history) and re-fetches that page via
    // the shared path so the active filters ride along (issue #426).
    var buttons = els.arPagination.querySelectorAll('button');
    buttons.forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (btn.disabled) return; // disabled buttons never fire in browsers; belt-and-braces
        var page = Number(btn.getAttribute('data-page'));
        if (!Number.isInteger(page) || page < 1) return;
        setAgentRunPage(page);
        fetchAgentRunsAndRender();
      });
    });
  }

  /** Pure state for the Agent Runs date-filter bar (issue #7): which of the
   *  From/To inputs is populated (drives the active styling) and whether the
   *  Clear button must be disabled (both empty).  Pure — no DOM access — so
   *  the Node test harness exercises it through the vm-sandbox exports.
   *  @param {string} fromValue raw #ar-filter-from value ('' when empty)
   *  @param {string} toValue   raw #ar-filter-to value ('' when empty)
   *  @returns {{fromActive: boolean, toActive: boolean, clearDisabled: boolean}} */
  function computeArDateFilterState(fromValue, toValue) {
    var fromActive = !!(fromValue && fromValue.length > 0);
    var toActive = !!(toValue && toValue.length > 0);
    return {
      fromActive: fromActive,
      toActive: toActive,
      clearDisabled: !fromActive && !toActive
    };
  }

  /** Sync the date-filter bar's visual state from the current From/To input
   *  values: toggles the active class on populated inputs (visible active
   *  styling) and enables the Clear button whenever either input has a
   *  value (issue #7). */
  function syncArDateFilterUI() {
    var state = computeArDateFilterState(
      els.arFilterFrom ? els.arFilterFrom.value : '',
      els.arFilterTo ? els.arFilterTo.value : ''
    );
    if (els.arFilterFrom) els.arFilterFrom.classList.toggle('active', state.fromActive);
    if (els.arFilterTo) els.arFilterTo.classList.toggle('active', state.toActive);
    if (els.arFilterClear) els.arFilterClear.disabled = state.clearDisabled;
  }

  /** Clear both date inputs and re-apply the existing filter path (issue #7):
   *  with the From/To inputs emptied they are no longer explicit filters, so
   *  buildAgentRunsUrl() falls back to the shared dashboard date range via
   *  resolveDateRange(dateRangeState) — per boundary, an unset input inherits
   *  the dashboard range on that side (issue #412).  Reuses applyFilters() →
   *  readFiltersFromUI() → buildAgentRunsUrl(); no new fetch mechanism. */
  function clearArDateFilters() {
    if (els.arFilterFrom) els.arFilterFrom.value = '';
    if (els.arFilterTo) els.arFilterTo.value = '';
    syncArDateFilterUI();
    applyFilters();
  }

  /** Wire the Agent Runs filter-bar/detail DOM events against the captured
   *  element refs: Apply, Clear (issue #7), live input styling, and the
   *  detail overlay close handlers.  Exported on the window test seam —
   *  broader than the pure-function exports — so the Node test harness can
   *  drive it against fake elements; readFiltersFromUI, clearArDateFilters,
   *  computeArDateFilterState, and syncArDateFilterUI are exported alongside
   *  it for the same reason. */
  function setupAgentRunEventHandlers() {
    // Apply button
    if (els.arFilterApply) {
      els.arFilterApply.addEventListener('click', applyFilters);
    }

    // Clear button (issue #7): empties both date inputs and re-applies the
    // existing filter path (readFiltersFromUI -> applyFilters ->
    // buildAgentRunsUrl).  With the date inputs cleared the list inherits the
    // shared dashboard date range — per-boundary fallback via
    // resolveDateRange(dateRangeState), issue #412 — not an unfiltered view.
    if (els.arFilterClear) {
      els.arFilterClear.addEventListener('click', clearArDateFilters);
    }

    // Live active styling + Clear enable state: repaint the visual state
    // whenever either date input's value changes (issue #7).
    if (els.arFilterFrom) {
      els.arFilterFrom.addEventListener('input', syncArDateFilterUI);
    }
    if (els.arFilterTo) {
      els.arFilterTo.addEventListener('input', syncArDateFilterUI);
    }

    // Initial visual state: both inputs start empty → no active classes,
    // Clear disabled.
    syncArDateFilterUI();

    // Agent run detail close button
    if (els.arDetailClose) {
      els.arDetailClose.addEventListener('click', function () {
        els.arDetailOverlay.classList.remove('visible');
        agentRunDetail = null;
      });
    }

    // Agent run detail overlay click to close
    if (els.arDetailOverlay) {
      els.arDetailOverlay.addEventListener('click', function (e) {
        if (e.target === els.arDetailOverlay) {
          els.arDetailOverlay.classList.remove('visible');
          agentRunDetail = null;
        }
      });
    }

    // ESC key closes the agent run detail overlay
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' &&
          els.arDetailOverlay && els.arDetailOverlay.classList.contains('visible')) {
        els.arDetailOverlay.classList.remove('visible');
        agentRunDetail = null;
      }
    });

    // Enter key on agent filter triggers apply
    if (els.arFilterAgent) {
      els.arFilterAgent.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') applyFilters();
      });
    }
  }

  function setupTabNavigation() {
    var navItems = document.querySelectorAll('.top-nav-item');
    var tabContents = document.querySelectorAll('.tab-content');

    function activateTab(tabName) {
      // Deactivate all
      navItems.forEach(function (item) { item.classList.remove('active'); });
      tabContents.forEach(function (tab) { tab.classList.remove('active'); });

      // Activate target
      var targetItem = document.querySelector('.top-nav-item[data-tab="' + tabName + '"]');
      var targetTab = document.getElementById('tab-' + tabName);
      if (targetItem) targetItem.classList.add('active');
      if (targetTab) targetTab.classList.add('active');
    }

    navItems.forEach(function (item) {
      item.addEventListener('click', function () {
        var tabName = item.getAttribute('data-tab');
        if (tabName) activateTab(tabName);
      });
      // Keyboard activation: Enter or Space on a focused nav item switches
      // tabs (nav items are focusable via tabindex="0" in index.html)
      item.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          var tabName = item.getAttribute('data-tab');
          if (tabName) activateTab(tabName);
        }
      });
    });
  }

  // ── Date Range Bar Handlers ─────────────────────────────────────────

  function setupDateRangeHandlers() {
    // Preset dropdown
    if (els.drPreset) {
      // Sync dropdown to current state on initial load
      els.drPreset.value = dateRangeState.preset || 'this-month';
      if (els.drPreset.value === 'custom') {
        if (els.drCustomInputs) els.drCustomInputs.style.display = 'flex';
        if (els.drStartDate && dateRangeState.customStartDate) els.drStartDate.value = dateRangeState.customStartDate;
        if (els.drEndDate && dateRangeState.customEndDate) els.drEndDate.value = dateRangeState.customEndDate;
      }

      els.drPreset.addEventListener('change', function () {
        var preset = els.drPreset.value;
        dateRangeState.preset = preset;

        if (preset === 'custom') {
          // Show custom date inputs
          if (els.drCustomInputs) els.drCustomInputs.style.display = 'flex';
          // Don't refresh yet — wait for both date inputs
        } else {
          // Hide custom date inputs and clear custom state
          if (els.drCustomInputs) els.drCustomInputs.style.display = 'none';
          delete dateRangeState.customStartDate;
          delete dateRangeState.customEndDate;
          // Refresh with the selected preset immediately
          refreshDashboard();
        }
      });
    }

    // Custom start date input
    if (els.drStartDate) {
      els.drStartDate.addEventListener('change', function () {
        dateRangeState.customStartDate = els.drStartDate.value;
        maybeApplyCustomRange();
      });
    }

    // Custom end date input
    if (els.drEndDate) {
      els.drEndDate.addEventListener('change', function () {
        dateRangeState.customEndDate = els.drEndDate.value;
        maybeApplyCustomRange();
      });
    }
  }

  /** Trigger a dashboard refresh when both custom date inputs have values */
  function maybeApplyCustomRange() {
    if (dateRangeState.customStartDate && dateRangeState.customEndDate) {
      refreshDashboard();
    }
  }

  function startAutoRefresh() {
    setupAgentRunEventHandlers();
    setupTabNavigation();
    setupDateRangeHandlers();
    // Issue #426: read ?page / ?page_size from the URL before the initial
    // fetch so a deep link such as ?page=2&page_size=100 loads the
    // corresponding Agent Runs page on dashboard load.
    readAgentRunPaginationFromUrl();
    refreshDashboard(); // initial load
    refreshTimer = setInterval(refreshDashboard, REFRESH_INTERVAL_MS);
    updateFooterInterval();
  }

  function updateFooterInterval() {
    var el = document.getElementById('footer-interval');
    if (el) el.textContent = Math.round(REFRESH_INTERVAL_MS / 1000);
  }

  // ── Bootstrap ─────────────────────────────────────────────────────────

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', startAutoRefresh);
  } else {
    startAutoRefresh();
  }

  // Expose for tests
  window.resolveProjectLabel = resolveProjectLabel;
  window.createClientCache = createClientCache;
  window.ensureClientName = ensureClientName;
  window.refreshClientCache = refreshClientCache;
  window.invalidateClientCache = invalidateClientCache;
  window.formatUpdatedAgo = formatUpdatedAgo;
  window.computePanelFreshness = computePanelFreshness;
  window.shouldRenderPanel = shouldRenderPanel;
  window.resolvePanelStatuses = resolvePanelStatuses;
  window.formatClockTime = formatClockTime;
  window.kpiSubtitle = kpiSubtitle;
  window.formatAgentRunTimestamp = formatAgentRunTimestamp;
  // Agent Runs date-filter state + Clear control (issue #7) — pure state
  // helper, DOM sync, the Clear action, the filter reader (UTC-boundary
  // conversion regression), and the wiring entry point for the test harness.
  window.readFiltersFromUI = readFiltersFromUI;
  window.computeArDateFilterState = computeArDateFilterState;
  window.syncArDateFilterUI = syncArDateFilterUI;
  window.clearArDateFilters = clearArDateFilters;
  window.setupAgentRunEventHandlers = setupAgentRunEventHandlers;
  // Agent Runs URL builder + state hooks (issue #412): buildAgentRunsUrl
  // derives from_date/to_date from the closure state (agentRunFilters,
  // dateRangeState), so the node harness gets the builder itself plus
  // setters to exercise the dashboard-range fallback and the
  // explicit-override behavior without a DOM.
  window.buildAgentRunsUrl = buildAgentRunsUrl;
  window.setAgentRunFilters = function (filters) { agentRunFilters = filters; };
  window.setDateRangeState = function (state) { dateRangeState = state; };
  // Agent Runs pagination state + URL persistence (issue #426): the pure
  // URL-param parser, the on-load URL reader, and the page-change history
  // hook — page state lives in the closure, so the node harness exercises
  // it through the builder output and the history stub.
  window.parseAgentRunPagination = parseAgentRunPagination;
  window.readAgentRunPaginationFromUrl = readAgentRunPaginationFromUrl;
  window.setAgentRunPage = setAgentRunPage;
  // Agent Runs pagination controls (issue #427): the pure page-item window
  // calculator and the control renderer (wires page-button clicks through
  // setAgentRunPage + the shared fetch path — filters preserved, row
  // content untouched).
  window.computePageItems = computePageItems;
  window.renderAgentRunPagination = renderAgentRunPagination;
  // Read-only accessor for the last COMPLETED refresh cycle time — reusable
  // by follow-up work (issue #358) without reaching into module state.
  window.getLastRefreshedAt = function () { return lastRefreshedAt; };

})();
