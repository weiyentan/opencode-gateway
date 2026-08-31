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
  const AFK_RUN_LIMIT = 50;
  // Change-request summary list (issue #613): page size for the primary
  // change-request-per-row view (GET /api/v1/afk-outcomes/change-requests).
  const AFK_CR_LIMIT = 100;

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
    agentUsageTbody: $('agent-usage-tbody'), // Agent Usage panel (issue #438)

    // Agent Runs — merged Sessions + Agent Runs table (issue #402)
    arTbody:        $('agent-runs-tbody'),
    arFilterFrom:   $('ar-filter-from'),
    arFilterTo:     $('ar-filter-to'),
    arFilterAgent:  $('ar-filter-agent'),
    arFilterStatus: $('ar-filter-status'),
    arFilterApply:  $('ar-filter-apply'),
    arFilterClear:  $('ar-filter-clear'),
    arPageSize:     $('ar-page-size'),
    arDetailOverlay: $('ar-detail-overlay'),
    arDetailTitle:  $('ar-detail-title'),
    arDetailBody:   $('ar-detail-body'),
    arDetailClose:  $('ar-detail-close'),
    arPagination:   $('agent-runs-pagination'), // control block below the panel (issue #427)

    // Client/Project
    cpTbody:         $('cp-tbody'),
    cpPanelSubtitle: $('cp-panel-subtitle'),

    // AFK Outcomes (issue #453)
    afkRunsTbody:    $('afk-runs-tbody'),
    afkDetailOverlay: $('afk-detail-overlay'),
    afkDetailTitle:  $('afk-detail-title'),
    afkDetailBody:   $('afk-detail-body'),
    afkDetailClose:  $('afk-detail-close'),

    // Unresolved Relationships (issue #576)
    unresolvedTbody: $('unresolved-relationships-tbody'),

    // Change Request List (issue #613): the primary change-request-per-row
    // AFK Outcomes view (summary contract + filters) and its detail overlay.
    afkCrListTbody:  $('afk-cr-list-tbody'),
    afkCrPagination: $('afk-cr-pagination'), // control block below the panel
    afkCrFilterProvider: $('afk-cr-filter-provider'),
    afkCrFilterRepository: $('afk-cr-filter-repository'),
    afkCrFilterProviderState: $('afk-cr-filter-provider-state'),
    afkCrFilterAutomationState: $('afk-cr-filter-automation-state'),
    afkCrFilterApply: $('afk-cr-filter-apply'),
    afkCrFilterClear: $('afk-cr-filter-clear'),
    crListDetailOverlay: $('cr-list-detail-overlay'),
    crListDetailTitle:   $('cr-list-detail-title'),
    crListDetailBody:    $('cr-list-detail-body'),
    crListDetailClose:   $('cr-list-detail-close'),

    // Date range bar
    drPreset:       $('dr-preset'),
    drCustomInputs: $('dr-custom-inputs'),
    drStartDate:    $('dr-start-date'),
    drEndDate:      $('dr-end-date'),

    // Change Request Provenance Timeline (issue #574)
    crProvOverlay:     $('cr-prov-overlay'),
    crProvTitle:       $('cr-prov-title'),
    crProvBody:        $('cr-prov-body'),
    crProvClose:       $('cr-prov-close'),

    // Transcript view (issue #469)
    trSessionInput:    $('tr-session-input'),
    trLoadBtn:         $('tr-load-btn'),
    trSessionHeader:   $('tr-session-header'),
    trViewToggle:      $('tr-view-toggle'),
    trTimelineWrap:    $('tr-timeline-wrap'),
    trMessagesWrap:    $('tr-messages-wrap'),
    trPartsWrap:       $('tr-parts-wrap'),
    trNextPageBtn:     $('tr-next-page-btn'),
    trStatus:          $('tr-status'),
  };

  // ── State ──────────────────────────────────────────────────────────────

  let refreshTimer = null;
  let fetchErrors = {};    // endpoint_key → error_message, per-fetch-cycle tracking
  let agentRunsData = null;       // latest agent runs response
  let agentRunFilters = {};       // current filter values
  let agentRunDetail = null;      // current detail view data
  let agentRunsFetchError = null; // per-cycle fetch error for agent runs
  let afkRunsData = null;         // latest AFK runs list response
  let afkRunsFetchError = null;   // per-cycle fetch error for AFK runs
  let unresolvedRelationshipsData = null; // latest unresolved relationships data
  let selectedRepo = null;
  let afkOnlyFilter = false;
  // Change-request summary list state (issue #613): the latest summary
  // response, the per-cycle fetch error, the active filter set (served
  // through the summary contract — never client-side re-filtering), and the
  // change-request identity of the currently opened detail row.  Selection
  // is keyed by (provider, repository, external_id) — never an internal
  // AFK Run ID (PRD story 14).
  let afkCrData = null;
  let afkCrFetchError = null;
  let afkCrFilters = { provider: '', repository: '', providerState: '', automationState: '' };
  let selectedChangeRequest = null;
  // Change-request list pagination state: the current page (1-indexed) and
  // the page size (AFK_CR_LIMIT).  The page is read from the URL
  // (?limit / ?offset) on dashboard load and translated to the summary
  // contract's limit/offset at request time — offset = (page - 1) * limit.
  let afkCrPage = 1;
  let afkCrPageSize = AFK_CR_LIMIT;

  // The #612 adapter/formatter module loads before app.js in index.html and
  // exposes itself on window.ChangeRequestAdapters.  Captured once at load
  // time; absent in environments that don't load the module (guarded at
  // every call site so nothing crashes at IIFE evaluation).
  var ChangeRequestAdapters = (typeof window !== 'undefined' && window.ChangeRequestAdapters) || null;
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

  // ── Transcript view state (issue #469) ────────────────────────────────
  let trSessionId = null;        // current loaded session UUID
  let trActiveView = 'timeline'; // 'timeline' | 'messages' | 'parts'
  let trNextCursor = null;       // keyset cursor for the active view
  let trHasMore = false;         // whether more pages exist
  let trItems = [];              // accumulated items for the active view

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
    'agent-usage':   ['aggByAgent'],
    'agent-runs':    ['agentRuns'],
    'client-project': ['aggClientProject'],
    'afk-outcomes':  ['afkRuns'],
    'afk-repos':     ['afkRuns'],
    'afk-change-requests': ['afkRuns'],
    'unresolved-relationships': ['afkRuns'],
    'afk-cr-list':   ['afkChangeRequests'], // primary change-request view (issue #613)
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

  /** Format todo progress as a visual bar + count, e.g. "███░░ 3/5".
   *  A null/zero total renders the "no todos" empty state "0/0" (not "--"). */
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

  /** Get a CSS status badge class for agent run status */
  function statusBadgeClass(status) {
    if (status === 'running') return 'badge-running';
    if (status === 'stale') return 'badge-stale';
    if (status === 'completed') return 'badge-completed';
    if (status === 'blocked') return 'badge-blocked';
    return 'badge-unknown';
  }

  /** Format code change additions/deletions as a compact diff.
   *  Renders `+{additions}/-{deletions}` (e.g. `+15/-3`), suppressing the
   *  zero side (`-42` for pure deletions, `+120` for pure additions), and
   *  `--` when there is no code change data (both additions and deletions
   *  null/zero). */
  function fmtCodeChangesDiff(additions, deletions) {
    if (additions == null || deletions == null) return '--';
    var add = Number(additions);
    var del = Number(deletions);
    if (add === 0 && del === 0) return '--';
    if (add === 0) return '-' + fmtNum(del);
    if (del === 0) return '+' + fmtNum(add);
    return '+' + fmtNum(add) + '/-' + fmtNum(del);
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

  /** Format a provider value for display (issue #557).
   *  Present values render as a compact outlined badge (neutral palette);
   *  null/empty/missing values render the caller's missing label:
   *  '—' (em dash) in tables, 'unknown' in the detail overlay. */
  function fmtProvider(value, missingLabel) {
    var missing = missingLabel === undefined ? '\u2014' : missingLabel;
    if (value == null || value === '') return missing;
    return badge(String(value), 'badge-provider').outerHTML;
  }

  /** Format a cache hit ratio percentage (issue #557):
   *  cacheRead / (input + cacheRead).  Returns '--' when the denominator
   *  is zero (no input activity to measure against). */
  function fmtCacheHitRatio(cacheReadTokens, inputTokens) {
    var cr = Number(cacheReadTokens) || 0;
    var input = Number(inputTokens) || 0;
    var denominator = input + cr;
    if (!(denominator > 0)) return '--';
    return (cr / denominator * 100).toFixed(1) + '%';
  }

  /** Build the Token Breakdown detail-section HTML (issue #557).
   *  Per-run totals for input/output/cache read/cache write/reasoning plus
   *  the cache hit ratio and primary provider.  Null token fields render
   *  as 0 (numeric consistency — JSON stays null); a missing provider
   *  renders as 'unknown' in the overlay. */
  function fmtTokenBreakdownSection(d) {
    var input = (d && d.total_input_tokens) || 0;
    var output = (d && d.total_output_tokens) || 0;
    var read = (d && d.total_cache_read_tokens) || 0;
    var write = (d && d.total_cache_write_tokens) || 0;
    var reasoning = (d && d.total_reasoning_tokens) || 0;
    return '<div class="detail-section">' +
      '<div class="detail-section-title">Token Breakdown</div>' +
      '<div class="detail-grid">' +
        fieldHtml('Input Tokens', fmtNum(input)) +
        fieldHtml('Output Tokens', fmtNum(output)) +
        fieldHtml('Cache Read Tokens', fmtNum(read)) +
        fieldHtml('Cache Write Tokens', fmtNum(write)) +
        fieldHtml('Reasoning Tokens', fmtNum(reasoning)) +
        fieldHtml('Cache Hit Ratio', fmtCacheHitRatio(read, input)) +
        fieldHtml('Provider', fmtProvider(d && d.primary_provider, 'unknown')) +
      '</div></div>';
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

  // ── AFK Repository Summary helpers (issue #572) ───────────────────────
  // Aggregates AFK runs into a repository-first summary list: one row per
  // unique (provider, repository) pair, showing AFK activity counts,
  // provider identity, and last-activity timestamps.  Pure helpers — no DOM
  // access — so the Node test harness exercises them through the window
  // test seam.

  /** Derive a repository label from an AFK run's dedicated repository field,
   *  or its title and metadata. Falls back to provider name when no
   *  repository can be resolved. Pure — no DOM or fetch access.
   *  @param {Object} run - AFK run item from the API
   *  @returns {string} a normalized repository label */
  function deriveRepositoryLabel(run) {
    if (!run) return 'unknown';
    // Prefer a dedicated repository field from the API response
    if (run.repository) return run.repository;
    // Fallback: parse repository from title (e.g. "owner/repo: Fix bug")
    var title = run.title || '';
    var match = title.match(/^([a-zA-Z0-9_.\-]+\/[a-zA-Z0-9_.\-]+)/);
    if (match) return match[1];
    // Fall back to provider name
    return run.provider || 'unknown';
  }

  /** Build repository summary rows from an AFK runs list.
   *  Pure: aggregates runs by (provider, repository) without DOM access.
   *  @param {Array|null} runs - AFK runs items from the API
   *  @returns {Array<{provider: string, repository: string, runCount: number,
   *           lastActivity: string|null}>} sorted by runCount desc,
   *           then provider asc, then repository asc */
  function buildRepositorySummaries(runs) {
    if (!runs || !Array.isArray(runs) || runs.length === 0) return [];
    var groups = {};
    runs.forEach(function (r) {
      var provider = (r && r.provider) || 'unknown';
      var repository = deriveRepositoryLabel(r);
      var key = provider + '|' + repository;
      if (!groups[key]) {
        groups[key] = {
          provider: provider,
          repository: repository,
          runCount: 0,
          lastActivity: null
        };
      }
      groups[key].runCount++;
      var seen = r.last_seen_at || r.started_at;
      if (seen && (!groups[key].lastActivity || seen > groups[key].lastActivity)) {
        groups[key].lastActivity = seen;
      }
    });
    return Object.keys(groups).map(function (k) { return groups[k]; })
      .sort(function (a, b) {
        if (b.runCount !== a.runCount) return b.runCount - a.runCount;
        if (a.provider !== b.provider) return a.provider < b.provider ? -1 : 1;
        return a.repository < b.repository ? -1 : (a.repository > b.repository ? 1 : 0);
      });
  }

  /** Make the repository summary table rows clickable (issue #573).
   *  Each row fires selectRepository(provider, repository) on click,
   *  transitioning to the change-request list view for that repository. */
  function renderRepositorySummaryTable(data) {
    applyPanelFreshness('afk-repos');
    if (!shouldRenderPanel(panelStates, 'afk-repos')) return;

    var reposEl = $('afk-repos-tbody');
    if (!reposEl) return;

    var runs = data && data.items;
    var summaries = buildRepositorySummaries(runs);

    if (summaries.length === 0) {
      var errSuffix = afkRunsFetchError
        ? ' <span class="fetch-error" title="' + escHtml(afkRunsFetchError) + '">\u26A0 Fetch error</span>'
        : '';
      reposEl.innerHTML = '<tr><td colspan="4" class="empty-state">No repository activity' + errSuffix + '</td></tr>';
      return;
    }

    var html = '';
    summaries.forEach(function (s) {
      var providerBadge = badge(s.provider, 'badge-provider').outerHTML;
      html += '<tr class="repo-row clickable" data-provider="' + escHtml(s.provider) + '" data-repository="' + escHtml(s.repository) + '" tabindex="0">' +
        '<td>' + providerBadge + '</td>' +
        '<td>' + escHtml(s.repository) + '</td>' +
        '<td>' + fmtNum(s.runCount) + '</td>' +
        '<td>' + fmtDT(s.lastActivity) + '</td>' +
        '</tr>';
    });

    reposEl.innerHTML = html;

    // Wire click + keyboard handlers for repository selection
    var rows = reposEl.querySelectorAll('.repo-row');
    rows.forEach(function (row) {
      row.addEventListener('click', function () {
        var provider = row.getAttribute('data-provider');
        var repository = row.getAttribute('data-repository');
        if (provider && repository) selectRepository(provider, repository);
      });
      row.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          var provider = row.getAttribute('data-provider');
          var repository = row.getAttribute('data-repository');
          if (provider && repository) selectRepository(provider, repository);
        }
      });
    });
  }

  // ── AFK Change Request List helpers (issue #573) ──────────────────────
  // When a repository is selected in the Repository Summary, show all AFK
  // runs for that repository as change requests (GitHub PRs / GitLab MRs).
  // Uses canonical change_request vocabulary with provider-specific labels
  // (PR for GitHub, MR for GitLab).  Pure helpers — no DOM access.

  /** Derive the provider-specific term for a change request.
   *  GitHub -> "PR", GitLab -> "MR", anything else -> "CR".
   *  Pure — no DOM or fetch access.
   *  @param {string} provider
   *  @returns {string} e.g. "PR", "MR", "CR" */
  function providerCrTerm(provider) {
    var p = (provider || '').toLowerCase();
    if (p === 'github') return 'PR';
    if (p === 'gitlab') return 'MR';
    return 'CR';
  }

  /** Build change-request rows for a selected repository from AFK runs.
   *  Pure: filters runs by (provider, repository) and extracts change-request
   *  details from each run's outcome.  Returns rows sorted by last_seen_at
   *  descending (most recent first).
   *  @param {Array|null} runs   - AFK runs items from the API
   *  @param {string} provider   - the repository's provider
   *  @param {string} repository - the repository label
   *  @returns {Array<{afkRunId, title, status, outcomeStatus, changeRequestIds,
   *           provider, repository, lastSeenAt, startedAt, afkLinked}>}
   */
  function buildChangeRequestList(runs, provider, repository) {
    if (!runs || !Array.isArray(runs) || !provider || !repository) return [];
    return runs
      .filter(function (r) {
        if (!r) return false;
        var runProvider = (r.provider || '').toLowerCase();
        var runRepo = deriveRepositoryLabel(r);
        return runProvider === provider.toLowerCase() && runRepo === repository;
      })
      .map(function (r) {
        var outcome = r.outcome || null;
        return {
          afkRunId: r.afk_run_id || null,
          title: r.title || r.afk_run_id || '--',
          status: r.status || '--',
          outcomeStatus: outcome ? (outcome.status || '--') : '--',
          changeRequestIds: outcome && outcome.change_request_ids ? outcome.change_request_ids : [],
          provider: r.provider || 'unknown',
          repository: repository,
          lastSeenAt: r.last_seen_at || null,
          startedAt: r.started_at || null,
          afkLinked: !!(outcome && outcome.change_request_ids && outcome.change_request_ids.length > 0)
        };
      })
      .sort(function (a, b) {
        // Most recent last_seen_at first; nulls last
        if (a.lastSeenAt && b.lastSeenAt) {
          return a.lastSeenAt < b.lastSeenAt ? 1 : (a.lastSeenAt > b.lastSeenAt ? -1 : 0);
        }
        if (a.lastSeenAt) return -1;
        if (b.lastSeenAt) return 1;
        return 0;
      });
  }

  /** Apply the AFK-only filter to a change-request list.
   *  Pure — filters rows by afkLinked flag.
   *  @param {Array} rows - change-request rows from buildChangeRequestList
   *  @param {boolean} afkOnly - true -> only AFK-linked rows
   *  @returns {Array} filtered rows */
  function filterChangeRequests(rows, afkOnly) {
    if (!afkOnly) return rows;
    return rows.filter(function (r) { return r.afkLinked; });
  }

  /** Handle a repository click from the Repository Summary table.
   *  Sets the selected repository and re-renders the change-request list. */
  function selectRepository(provider, repository) {
    selectedRepo = { provider: provider, repository: repository };
    afkOnlyFilter = false;
    renderChangeRequestList(afkRunsData);
  }

  /** Return to the repository summary view (clear selection). */
  function clearSelectedRepo() {
    selectedRepo = null;
    afkOnlyFilter = false;
    var panelEl = $('afk-change-requests-panel');
    if (panelEl) panelEl.style.display = 'none';
  }

  /** Render the change-request list table for the selected repository.
   *  Shows the selected repository header, AFK-only toggle, and a table of
   *  change requests with provider-specific labels (PR/MR) and AFK-linked
   *  highlighting.  Follows the agent-runs panel conventions. */
  function renderChangeRequestList(data) {
    applyPanelFreshness('afk-change-requests');
    if (!shouldRenderPanel(panelStates, 'afk-change-requests')) return;

    var panelEl = $('afk-change-requests-panel');
    var tbodyEl = $('afk-cr-tbody');
    var headerEl = $('afk-cr-header');
    var toggleEl = $('afk-cr-toggle');
    if (!panelEl || !tbodyEl) return;

    // No repository selected -> hide the panel
    if (!selectedRepo) {
      panelEl.style.display = 'none';
      return;
    }
    panelEl.style.display = '';

    // Update header with selected repository
    if (headerEl) {
      var crTerm = providerCrTerm(selectedRepo.provider);
      headerEl.textContent = selectedRepo.repository + ' ' + crTerm + 's (' + selectedRepo.provider + ')';
    }

    // Sync the AFK-only toggle checkbox
    if (toggleEl) {
      toggleEl.checked = afkOnlyFilter;
    }

    var runs = data && data.items;
    var rows = buildChangeRequestList(runs, selectedRepo.provider, selectedRepo.repository);
    var filtered = filterChangeRequests(rows, afkOnlyFilter);

    if (filtered.length === 0) {
      var emptyMsg = rows.length === 0
        ? 'No change requests found for ' + escHtml(selectedRepo.repository)
        : 'No AFK-linked change requests for ' + escHtml(selectedRepo.repository);
      tbodyEl.innerHTML = '<tr><td colspan="5" class="empty-state">' + emptyMsg + '</td></tr>';
      return;
    }

    var crTerm = providerCrTerm(selectedRepo.provider);
    var html = '';
    filtered.forEach(function (r) {
      var statusCls = afkRunStatusBadgeClass(r.status);
      var outcomeCls = outcomeStatusBadgeClass(r.outcomeStatus);
      var crIds = r.changeRequestIds.length > 0 ? r.changeRequestIds.join(', ') : '--';
      html += '<tr class="afk-cr-row' + (r.afkLinked ? ' afk-cr-linked' : '') + '">' +
        '<td data-label="' + crTerm + ' ID">' + escHtml(crIds) + '</td>' +
        '<td data-label="Status">' + badge(r.status || '--', statusCls).outerHTML + '</td>' +
        '<td data-label="Outcome">' + badge(outcomeStatusLabel(r.outcomeStatus), outcomeCls).outerHTML + '</td>' +
        '<td data-label="AFK">' + (r.afkLinked ? '<span class="afk-cr-badge">AFK</span>' : '--') + '</td>' +
        '<td data-label="Last Seen">' + fmtDT(r.lastSeenAt) + '</td>' +
        '</tr>';
    });

    tbodyEl.innerHTML = html;
  }

  // ── AFK outcome chain helpers (issue #453) ─────────────────────────────
  // The locked EngineeringOutcomeStatus vocabulary (afk_outcomes.models) is
  // merged / closed / abandoned / open.  "still open" is the human rendering
  // of EngineeringOutcomeStatus.open (the issue's "still_open"); "failed" is a
  // RunStatus (afkRunStatusBadgeClass), never an outcome status — the two are
  // rendered from their own enums and never conflated.

  /** Map an EngineeringOutcomeStatus value to a status-badge CSS class.
   *  merged/closed/abandoned/open → dedicated classes; anything else → unknown.
   *  @param {string|null} status
   *  @returns {string} badge class */
  function outcomeStatusBadgeClass(status) {
    if (status === 'merged') return 'badge-merged';
    if (status === 'closed') return 'badge-closed';
    if (status === 'abandoned') return 'badge-abandoned';
    if (status === 'open') return 'badge-open';
    return 'badge-unknown';
  }

  /** Human label for an EngineeringOutcomeStatus value.  The "open" state
   *  renders as "still open" (issue #453's "still_open"); all other values
   *  pass through verbatim; null/absent → '--'. */
  function outcomeStatusLabel(status) {
    if (status === 'open') return 'still open';
    return status || '--';
  }

  /** Map a RunStatus value (afk_outcomes.models.RunStatus) to a badge class.
   *  Extends the Agent Runs statusBadgeClass with the AFK-only terminal
   *  states failed / cancelled / timed_out. */
  function afkRunStatusBadgeClass(status) {
    if (status === 'running') return 'badge-running';
    if (status === 'stale') return 'badge-stale';
    if (status === 'completed') return 'badge-completed';
    if (status === 'blocked') return 'badge-blocked';
    if (status === 'failed') return 'badge-failed';
    if (status === 'cancelled') return 'badge-cancelled';
    if (status === 'timed_out') return 'badge-stale';
    return 'badge-unknown';
  }

  /** Format a correlation confidence (0.0–1.0) as a compact percentage label.
   *  Null/non-numeric → '--'.  e.g. 1.0 → '100%', 0.1 → '10%'. */
  function fmtConfidence(value) {
    if (value == null || isNaN(Number(value))) return '--';
    return Math.round(Number(value) * 100) + '%';
  }

  /** Format a correlation evidence list into a compact plain-text string.
   *  Each item renders as `kind ← source_entity_id (detail)` joined by '; '.
   *  Empty/missing → ''.  The render layer escapes the result (escHtml). */
  function fmtEvidence(evidence) {
    if (!Array.isArray(evidence) || evidence.length === 0) return '';
    return evidence.map(function (e) {
      var kind = (e && e.kind) ? e.kind : 'evidence';
      var src = (e && e.source_entity_id) ? ' \u2190 ' + e.source_entity_id : '';
      var detail = (e && e.detail) ? ' (' + e.detail + ')' : '';
      return kind + src + detail;
    }).join('; ');
  }

  /** Whether a link is provisional/inferred: an entity link is provisional
   *  when its `provisional` flag is set (role != 'resolved'); a session link
   *  is inferred when its `inferred` flag is set.  These low-confidence links
   *  are marked distinctly in the UI — never rendered like explicit links. */
  function isProvisionalLink(link) {
    return !!(link && (link.provisional === true || link.inferred === true));
  }

  /** Resolve the internal session id an AFK-linked session opens in the Agent
   *  Run detail overlay, or null when the session has no resolvable internal
   *  id (such a link stays non-clickable but remains visibly inferred).
   *  Pure — no DOM access (issue #473). */
  function resolveAfkSessionDrilldown(session) {
    return (session && session.session_id) ? String(session.session_id) : null;
  }

  /** Compose the canonical AFK outcome chain from a RunDetail response.
   *  Pure — returns the ordered steps (issue → run → sessions → agents →
   *  tokens/cost → change_request → commits → review cycles → outcome) with
   *  the data each step renders, preserving every link's correlation
   *  provenance and provisional/inferred markers for the render layer. */
  function buildAfkChain(detail) {
    var d = detail || {};
    return [
      { key: 'issues', label: 'Issue', items: d.issues || [] },
      { key: 'run', label: 'Run', run: d.run || null },
      { key: 'sessions', label: 'Sessions', items: d.sessions || [] },
      { key: 'agents', label: 'Agents', items: d.agents || [] },
      { key: 'usage', label: 'Tokens / Cost', usage: d.usage || null },
      { key: 'change_requests', label: 'Change Request', items: d.change_requests || [] },
      { key: 'commits', label: 'Commits', items: d.commits || [] },
      { key: 'reviews', label: 'Review Cycles', items: d.reviews || [] },
      { key: 'outcome', label: 'Outcome', outcome: d.outcome || null, mergeEvents: d.merge_events || [] }
    ];
  }

  // ── Relationship state helpers (issue #576) ───────────────────────────
  // Make relationship certainty visible throughout AFK Outcomes: resolved,
  // provisional, ambiguous, unmatched, parked, unresolved, noise, and
  // referenced states each map to a distinct badge class and label.  No
  // uncertain state is ever silently omitted or rendered as a definitive link.

  /** Map a relationship state to a badge CSS class and human label.
   *  Resolved links get badge-completed (green); provisional/inferred get
   *  badge-provisional (amber); ambiguous/unmatched/parked/unresolved each
   *  get their own distinct class; noise and referenced fall back to existing
   *  classes.  Null/unknown falls back to badge-unknown.
   *  @param {string|null} state
   *  @returns {{label: string, cssClass: string}} */
  function fmtRelationshipState(state) {
    if (state === 'resolved')   return { label: 'resolved',   cssClass: 'badge-completed' };
    if (state === 'provisional') return { label: 'provisional', cssClass: 'badge-provisional' };
    if (state === 'inferred')   return { label: 'inferred',   cssClass: 'badge-provisional' };
    if (state === 'ambiguous')  return { label: 'ambiguous',  cssClass: 'badge-ambiguous' };
    if (state === 'unmatched')  return { label: 'unmatched',  cssClass: 'badge-unmatched' };
    if (state === 'parked')     return { label: 'parked',     cssClass: 'badge-parked' };
    if (state === 'unresolved') return { label: 'unresolved', cssClass: 'badge-unresolved' };
    if (state === 'noise')      return { label: 'noise',      cssClass: 'badge-unknown' };
    if (state === 'referenced') return { label: 'referenced', cssClass: 'badge-stale' };
    return { label: state || '--', cssClass: 'badge-unknown' };
  }

  /** Render a relationship-state badge with optional provenance line.
   *  Confidence is shown as a percentage; method, evidence, and resolver
   *  version are shown when non-null.  Pure — returns HTML string.
   *  @param {string|null} state
   *  @param {{confidence, method, evidence, resolver_version}|null} provenance
   *  @returns {string} HTML */
  function renderRelationshipBadge(state, provenance) {
    var info = fmtRelationshipState(state);
    var html = badge(info.label, info.cssClass).outerHTML;
    if (provenance) {
      var parts = [];
      parts.push(fmtConfidence(provenance.confidence));
      if (provenance.method) parts.push(escHtml(provenance.method));
      if (provenance.resolver_version) parts.push('resolver v' + escHtml(provenance.resolver_version));
      if (parts.length) {
        html += ' <span class="afk-provenance">' + parts.join(' \u00B7 ') + '</span>';
      }
      var evidence = renderAfkEvidence(provenance);
      if (evidence) {
        html += ' <span class="afk-evidence">' + evidence + '</span>';
      }
    }
    return html;
  }

  /** Build the list of unresolved/provisional/parked relationships from an
   *  AFK run detail response.  Extracts items from the `unresolved` and
   *  `parked` arrays on the detail, mapping each to a uniform shape with a
   *  `state` field.  Pure — no DOM or fetch access.
   *  @param {Object|null} detail - the AFK run detail response
   *  @returns {Array} list of unresolved relationship items */
  function buildUnresolvedRelationships(detail) {
    if (!detail) return [];
    var items = [];
    var unresolved = detail.unresolved || [];
    var parked = detail.parked || [];
    unresolved.forEach(function (u) {
      items.push({
        entity_id: u.entity_id || '--',
        entity_type: u.entity_type || '',
        state: u.reason || u.state || 'unresolved',
        confidence: u.correlation_confidence,
        method: u.correlation_method,
        evidence: u.evidence || [],
        resolver_version: u.resolver_version
      });
    });
    parked.forEach(function (p) {
      items.push({
        entity_id: p.entity_id || '--',
        entity_type: p.entity_type || '',
        state: 'parked',
        confidence: p.correlation_confidence,
        method: p.correlation_method,
        evidence: p.evidence || [],
        resolver_version: p.resolver_version
      });
    });
    return items;
  }

  /** Render one unresolved-relationship row as HTML.  Pure — returns HTML.
   *  @param {Object} item - {entity_id, entity_type, state, confidence, method, evidence, resolver_version}
   *  @returns {string} HTML */
  function renderUnresolvedRelationshipsRow(item) {
    var html = '<tr>';
    html += '<td>' + escHtml(item.entity_id) + '</td>';
    html += '<td>' + escHtml(item.entity_type) + '</td>';
    html += '<td>' + renderRelationshipBadge(item.state, {
      confidence: item.confidence,
      method: item.method,
      evidence: item.evidence,
      resolver_version: item.resolver_version
    }) + '</td>';
    html += '</tr>';
    return html;
  }

  /** Render the unresolved-relationships view.  Returns HTML for the panel
   *  body: a table of items or the empty state.  Pure — no DOM access.
   *  @param {{items: Array}|null} data
   *  @returns {string} HTML */
  function renderUnresolvedRelationships(data) {
    var items = (data && data.items) || [];
    if (items.length === 0) {
      return '<p class="empty-state">No unresolved relationships</p>';
    }
    var html = '<div class="table-scroll"><table>' +
      '<thead><tr>' +
        '<th>Entity</th>' +
        '<th>Type</th>' +
        '<th>State / Provenance</th>' +
      '</tr></thead>' +
      '<tbody>';
    items.forEach(function (item) {
      html += renderUnresolvedRelationshipsRow(item);
    });
    html += '</tbody></table></div>';
    return html;
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

  /** Validate an Agent Runs page size against the page-size selector's
   *  choices (issue #428): exactly 25, 50, or 100 rows per page, with
   *  AGENT_RUN_LIMIT (50) as the default.  Unsupported values — including
   *  malformed input — fall back to the default.  Pure — no DOM, location,
   *  or fetch access. */
  function parseAgentRunPageSize(rawValue) {
    var n = Number(rawValue);
    if (n === 25 || n === 50 || n === 100) {
      return n;
    }
    return AGENT_RUN_LIMIT;
  }

  /** Parse Agent Runs pagination from a URL query string (issue #426).
   *  Reads `page` and `page_size`; missing or malformed (non-integer,
   *  negative, or zero) page values fall back to page 1.  Issue #428: the
   *  page size is restricted to the selector's choices (25/50/100) — any
   *  other value falls back to the default page size (AGENT_RUN_LIMIT =
   *  50).  Pure — no DOM, location, or fetch access. */
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
    pageSize = parseAgentRunPageSize(rawPageSize);
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
    // PR #431 review (finding 1): a deep link (e.g. ?page_size=100) must
    // also sync the visible #ar-page-size selector, so the control never
    // shows a stale page size that disagrees with the fetched limit.
    if (els.arPageSize) els.arPageSize.value = String(pagination.pageSize);
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

  /** Set the Agent Runs page size and reset to page 1 (issue #428).
   *  Validates the requested size against the page-size selector's choices
   *  (25/50/100); unsupported values fall back to the default
   *  (AGENT_RUN_LIMIT = 50).  A page-size change re-scopes the current
   *  view, so the URL state is REPLACED (history.replaceState) rather than
   *  pushed — unlike explicit page navigation (setAgentRunPage), which
   *  keeps using pushState.  The rows themselves only change through the
   *  normal fetch path (buildAgentRunsUrl → fetchAll / applyFilters). */
  function setAgentRunPageSize(pageSize) {
    agentRunPageSize = parseAgentRunPageSize(pageSize);
    agentRunPage = 1;
    var url = agentRunsUrlWithPagination(agentRunPage, agentRunPageSize);
    if (typeof history !== 'undefined' && typeof history.replaceState === 'function') {
      history.replaceState({}, '', url);
    }
  }

  /** Compute the nearest valid Agent Runs page for a fetched result total
   *  (issue #429).  When the result set shrinks — runs deleted, or the
   *  list narrowed elsewhere — the currently selected page may exceed the
   *  new page count; the UI must land on the nearest valid page instead of
   *  rendering an empty offset.  An empty result (total=0) resolves to
   *  page 1: from page 1 the function returns 1 unchanged (no navigation),
   *  and from a higher page it lands on page 1 — never page 0.  Pure — no
   *  DOM, location, or fetch access.
   *  @param {number} total       fetched result total (>= 0)
   *  @param {number} currentPage currently selected page (>= 1)
   *  @param {number} pageSize    rows per page (25/50/100)
   *  @returns {number} the nearest valid page (>= 1) */
  function nearestValidAgentRunPage(total, currentPage, pageSize) {
    var pageCount = Math.ceil(total / pageSize);
    var current = Math.max(currentPage, 1);
    return Math.min(current, Math.max(1, pageCount));
  }

  /** Correct the Agent Runs page state after a fetch when the result total
   *  no longer covers the current page (issue #429).  When the fetched
   *  total implies fewer pages than the current page (the result set
   *  shrank), the closure page state moves to the nearest valid page
   *  (nearestValidAgentRunPage) and the URL is REPLACED
   *  (history.replaceState, per the #428 precedent) so the fallback does
   *  not add a browser-history entry.  The caller then refetches the
   *  corrected page through the normal fetch path — this hook only fixes
   *  state + URL.  Returns true when a fallback was applied, false
   *  otherwise (no data, a still-valid page, or an empty result already on
   *  page 1 — so a refetch can never loop). */
  function applyAgentRunPageFallback(data) {
    if (!data) return false;
    var total = (typeof data.total === 'number')
      ? data.total
      : (data.items ? data.items.length : 0);
    var nearest = nearestValidAgentRunPage(total, agentRunPage, agentRunPageSize);
    if (nearest === agentRunPage) return false;
    agentRunPage = nearest;
    if (typeof history !== 'undefined' && typeof history.replaceState === 'function') {
      history.replaceState({}, '', agentRunsUrlWithPagination(agentRunPage, agentRunPageSize));
    }
    return true;
  }

  /** Re-sync Agent Runs pagination state from the URL after a browser
   *  Back/Forward navigation (PR #431 review finding 4).  Back/Forward
   *  changes location.search without re-running the load-time URL read,
   *  so without this listener agentRunPage/agentRunPageSize stay stale and
   *  the next refresh fetches the wrong offset while the control
   *  highlights a page that no longer matches the URL.  This handler
   *  re-reads the URL (which also syncs the page-size selector) and, when
   *  the effective page or page size changed, refetches through the shared
   *  path so the address bar, visible rows, and in-memory state stay
   *  consistent.  The URL read itself never pushes history, so this cannot
   *  add entries or loop. */
  function handleAgentRunPopstate() {
    var prevPage = agentRunPage;
    var prevSize = agentRunPageSize;
    readAgentRunPaginationFromUrl();
    if (agentRunPage !== prevPage || agentRunPageSize !== prevSize) {
      fetchAgentRunsAndRender();
    }
  }

  /** Re-sync the change-request list page after a Back/Forward navigation
   *  (popstate).  Reads ?limit / ?offset from the URL and, when the page
   *  changed, re-fetches through the shared fetch path so the address bar,
   *  visible rows, and in-memory state stay consistent.  The URL read
   *  itself never pushes history, so this cannot add entries or loop. */
  function handleChangeRequestPopstate() {
    var prevPage = afkCrPage;
    var prevSize = afkCrPageSize;
    readChangeRequestPaginationFromUrl();
    if (afkCrPage !== prevPage || afkCrPageSize !== prevSize) {
      fetchChangeRequestsAndRender();
    }
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
      const [health, aggTotal, aggByModel, records, clients, agentRuns, aggClientProjectResult, aggByAgent, afkRuns, afkChangeRequests] =
        await Promise.allSettled([
          apiFetch('/health'),
          apiFetch('/api/v1/usage/aggregates?start_date=' + aggStart + '&end_date=' + aggEnd),
          apiFetch('/api/v1/usage/aggregates?start_date=' + aggStart + '&end_date=' + aggEnd + '&group_by=model'),
          apiFetch('/api/v1/usage/records?start_date=' + aggStart + '&end_date=' + aggEnd + '&limit=' + RECORD_LIMIT + '&sort_by=source_created_at&sort_dir=desc'),
          clientsPromise,
          apiFetch(arUrl),
          apiFetch('/api/v1/usage/aggregates?start_date=' + aggStart + '&end_date=' + aggEnd + '&group_by=client,project'),
          // Agent Usage panel (issue #438): per-agent aggregate rows from the
          // group_by=agent query, sharing the dashboard date range and the
          // parallel-cycle fetchErrors/panelStates handling of the panels above.
          apiFetch('/api/v1/usage/aggregates?start_date=' + aggStart + '&end_date=' + aggEnd + '&group_by=agent'),
          // AFK Outcomes view (issue #453): the runs list driving the AFK
          // Outcomes tab.  List-only; the full chain is fetched on demand by
          // openAfkRunDetail (GET /api/v1/afk-outcomes/runs/{afk_run_id}).
          apiFetch('/api/v1/afk-outcomes/runs?limit=' + AFK_RUN_LIMIT),
          // Change-request summary list (issue #613): the primary AFK
          // Outcomes view — one row per provider/repository/change-request
          // identity from GET /api/v1/afk-outcomes/change-requests, scoped
          // by the active filters and the shared dashboard date range.
          // The current page offset is carried through so auto-refresh never
          // silently resets the list to page 1 (issue #617 review finding).
          apiFetch(buildChangeRequestListUrl(afkCrFilters, dateRangeState, AFK_CR_LIMIT,
            (afkCrPage - 1) * afkCrPageSize)),
        ]);

      results.health    = health.status    === 'fulfilled' ? health.value    : null;
      results.aggTotal  = aggTotal.status  === 'fulfilled' ? aggTotal.value  : null;
      results.aggByModel= aggByModel.status=== 'fulfilled' ? aggByModel.value: null;
      results.records   = records.status   === 'fulfilled' ? records.value   : null;
      results.clients   = clients.status   === 'fulfilled' ? clients.value   : null;
      results.agentRuns = agentRuns.status === 'fulfilled' ? agentRuns.value : null;
      results.aggClientProject = aggClientProjectResult.status === 'fulfilled' ? aggClientProjectResult.value : null;
      results.aggByAgent = aggByAgent.status === 'fulfilled' ? aggByAgent.value : null;
      results.afkRuns   = afkRuns.status   === 'fulfilled' ? afkRuns.value   : null;
      results.afkChangeRequests = afkChangeRequests.status === 'fulfilled' ? afkChangeRequests.value : null;
      afkCrData = results.afkChangeRequests; // latest change-request summary response (issue #613)

      // Track per-endpoint errors
      fetchErrors = {};
      if (health.status    !== 'fulfilled') fetchErrors.health    = health.reason?.message    || 'Health check failed';
      if (aggTotal.status  !== 'fulfilled') fetchErrors.aggTotal  = aggTotal.reason?.message  || 'Aggregates (total) failed';
      if (aggByModel.status!== 'fulfilled') fetchErrors.aggByModel= aggByModel.reason?.message|| 'Aggregates (by model) failed';
      if (records.status   !== 'fulfilled') fetchErrors.records   = records.reason?.message   || 'Usage records failed';
      if (clients.status   !== 'fulfilled') fetchErrors.clients   = clients.reason?.message   || 'Clients query failed';
      agentRunsFetchError = agentRuns.status !== 'fulfilled' ? (agentRuns.reason?.message || 'Agent runs query failed') : null;
      fetchErrors.aggClientProject = aggClientProjectResult.status !== 'fulfilled' ? (aggClientProjectResult.reason?.message || 'Client/project query failed') : null;
      if (aggByAgent.status!== 'fulfilled') fetchErrors.aggByAgent= aggByAgent.reason?.message || 'Aggregates (by agent) failed';
      afkRunsFetchError = afkRuns.status !== 'fulfilled' ? (afkRuns.reason?.message || 'AFK runs query failed') : null;
      afkCrFetchError = afkChangeRequests.status !== 'fulfilled' ? (afkChangeRequests.reason?.message || 'Change-request query failed') : null;

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

  /** Agent Usage panel (issues #438/#439) — row derivation.
   *  Pure: maps the group_by=agent aggregates rows to display rows carrying
   *  the full Token Breakdown contract — the four independent counters
   *  (input/output/cacheRead/cacheWrite), the full total
   *  (total = input + output + cache read + cache write per CONTEXT.md),
   *  the estimated cost (total_estimated_cost_usd) and the request count
   *  (record_count — each usage_events row is one request).  Rows without
   *  a recorded agent identity display as 'unknown' (the backend COALESCEs
   *  the same way; the frontend falls back defensively).  Ordered by total
   *  token usage descending, agent name ascending as the tie-breaker — the
   *  Agent Usage contract from CONTEXT.md. */
  function buildAgentUsageRows(aggRows) {
    if (!aggRows) return [];
    return aggRows
      .map(function (r) {
        var input = (r && r.total_input_tokens) || 0;
        var output = (r && r.total_output_tokens) || 0;
        var cacheRead = (r && r.total_cache_read_tokens) || 0;
        var cacheWrite = (r && r.total_cache_write_tokens) || 0;
        return {
          agent: (r && r.agent) || 'unknown',
          input: input,
          output: output,
          cacheRead: cacheRead,
          cacheWrite: cacheWrite,
          tokens: input + output + cacheRead + cacheWrite,
          cost: (r && r.total_estimated_cost_usd),
          requests: (r && r.record_count) || 0
        };
      })
      .sort(function (a, b) {
        if (b.tokens !== a.tokens) return b.tokens - a.tokens;
        return a.agent < b.agent ? -1 : (a.agent > b.agent ? 1 : 0);
      });
  }

  /** Agent Usage panel (issues #438/#439) — dynamic per-agent aggregate rows.
   *  Reads the group_by=agent aggregates (results.aggByAgent) fetched in the
   *  same parallel refresh cycle as the other panels; renders one row per
   *  observed agent with the full PRD #436 row contract: agent identity,
   *  compact Token Breakdown (delegating to the shared
   *  fmtTokenBreakdownCompact used by Sessions and Agent Run rows),
   *  estimated cost (fmtCost) and request count (record_count).  Follows
   *  the same freshness / failure-retention discipline as
   *  renderAgentsTable. */
  function renderAgentUsageTable(data) {
    applyPanelFreshness('agent-usage');
    if (!shouldRenderPanel(panelStates, 'agent-usage')) return; // failed fetch → keep previous rows

    var rows = data && data.aggByAgent || [];
    if (rows.length === 0) {
      els.agentUsageTbody.innerHTML = '<tr><td colspan="4" class="empty-state">No agent usage available' + errorIndicator('aggByAgent') + '</td></tr>';
      return;
    }

    var html = '';
    buildAgentUsageRows(rows).forEach(function (row) {
      html += '<tr>' +
        '<td>' + escHtml(row.agent) + '</td>' +
        '<td>' + fmtTokenBreakdownCompact(row.input, row.output, row.cacheRead, row.cacheWrite) + '</td>' +
        '<td>' + fmtCost(row.cost) + '</td>' +
        '<td>' + fmtNum(row.requests) + '</td>' +
        '</tr>';
    });

    els.agentUsageTbody.innerHTML = html;
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
      els.arTbody.innerHTML = '<tr><td colspan="15" class="empty-state">No agent runs' + errSuffix + '</td></tr>';
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
        '<td data-label="Provider">' + fmtProvider(r.primary_provider) + '</td>' +
        '<td data-label="Project / Worktree">' + escHtml(projectStr) + '</td>' +
        '<td class="ar-col-low" data-label="Todo">' + todoProgress + '</td>' +
        '<td class="ar-col-low" data-label="Files">' + fmtCodeChangesDiff(r.code_change_additions, r.code_change_deletions) + '</td>' +
        '<td data-label="Cost">' + fmtCost(r.total_estimated_cost_usd) + '</td>' +
        '<td data-label="Tokens">' + fmtAgentRunTokens(r.total_input_tokens, r.total_output_tokens, r.total_cache_read_tokens, r.total_cache_write_tokens) + '</td>' +
        '<td class="ar-num" data-label="Cache Read">' + fmtNum(r.total_cache_read_tokens || 0) + '</td>' +
        '<td class="ar-num ar-col-low" data-label="Cache Write">' + fmtNum(r.total_cache_write_tokens || 0) + '</td>' +
        '<td class="ar-num ar-col-low" data-label="Reasoning">' + fmtNum(r.total_reasoning_tokens || 0) + '</td>' +
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
  // ── Change Request Provenance Timeline (issue #574) ───────────────────
  // When a change request is selected from the AFK Outcomes detail, a
  // provenance timeline overlay renders the complete lifecycle: linked
  // issues (with independent change-request and issue repository identity),
  // every develop/review execution in chronological order (including
  // repeated cycles), timestamps, phase, status, outcome, duration, AWX
  // job IDs, OpenCode session identifiers, usage/cost, merge state, issue
  // closure state, and the final EngineeringOutcome.  RunStatus and
  // EngineeringOutcomeStatus are rendered as distinct concepts.

  /** Deterministic GitHub fixture: a completed change request with linked
   *  issues (same-repo and cross-repo), develop + review executions,
   *  merge state, issue closure state, and a final merged outcome. */
  function githubCompleteFixture() {
    return {
      change_request: {
        provider: 'github',
        repository: 'acme/web-app',
        resource_type: 'change_request',
        external_id: '142',
        title: 'Implement user authentication module',
        opened_at: '2026-08-01T09:00:00Z',
        merged_at: '2026-08-05T14:30:00Z',
        state: 'merged'
      },
      linked_issues: [
        {
          issue_number: '503',
          issue_repository: 'acme/web-app',
          relationship_kind: 'declares_closure',
          closure_status: 'inferred'
        },
        {
          issue_number: '25',
          issue_repository: 'acme/platform-tracking',
          relationship_kind: 'references',
          closure_status: null
        }
      ],
      executions: [
        {
          phase: 'develop',
          status: 'completed',
          outcome: 'completed',
          started_at: '2026-08-01T09:05:00Z',
          finished_at: '2026-08-01T09:45:00Z',
          duration_minutes: 40,
          awx_job_id: 'awx-job-1001',
          session_id: 'ses-auth-dev-001',
          input_tokens: 12000,
          output_tokens: 3500,
          cache_read_tokens: 8000,
          cache_write_tokens: 200,
          estimated_cost_usd: 0.08
        },
        {
          phase: 'review',
          status: 'completed',
          outcome: 'changes_requested',
          started_at: '2026-08-01T10:00:00Z',
          finished_at: '2026-08-01T10:15:00Z',
          duration_minutes: 15,
          awx_job_id: 'awx-job-1002',
          session_id: 'ses-auth-review-001',
          input_tokens: 5000,
          output_tokens: 800,
          cache_read_tokens: 2000,
          cache_write_tokens: 0,
          estimated_cost_usd: 0.02
        },
        {
          phase: 'develop',
          status: 'completed',
          outcome: 'completed',
          started_at: '2026-08-02T08:00:00Z',
          finished_at: '2026-08-02T09:30:00Z',
          duration_minutes: 90,
          awx_job_id: 'awx-job-1003',
          session_id: 'ses-auth-dev-002',
          input_tokens: 18000,
          output_tokens: 5200,
          cache_read_tokens: 12000,
          cache_write_tokens: 500,
          estimated_cost_usd: 0.12
        },
        {
          phase: 'review',
          status: 'completed',
          outcome: 'approved',
          started_at: '2026-08-02T10:00:00Z',
          finished_at: '2026-08-02T10:10:00Z',
          duration_minutes: 10,
          awx_job_id: 'awx-job-1004',
          session_id: 'ses-auth-review-002',
          input_tokens: 4500,
          output_tokens: 600,
          cache_read_tokens: 1800,
          cache_write_tokens: 0,
          estimated_cost_usd: 0.01
        }
      ],
      run_status: 'completed',
      engineering_outcome: {
        status: 'merged',
        merged_at: '2026-08-05T14:30:00Z',
        change_request_ids: ['acme/web-app#142'],
        resolved_issue_ids: ['acme/web-app#503']
      }
    };
  }

  /** Deterministic GitLab fixture: an incomplete change request with a
   *  repeated review cycle and no merge. */
  function gitlabIncompleteFixture() {
    return {
      change_request: {
        provider: 'gitlab',
        repository: 'cloudnative-pg/cloudnative-pg',
        resource_type: 'change_request',
        external_id: '6',
        title: 'Add connection pool max-size config',
        opened_at: '2026-07-20T11:00:00Z',
        merged_at: null,
        state: 'opened'
      },
      linked_issues: [
        {
          issue_number: '1',
          issue_repository: 'cloudnative-pg/cloudnative-pg',
          relationship_kind: 'declares_closure',
          closure_status: 'pending'
        }
      ],
      executions: [
        {
          phase: 'develop',
          status: 'completed',
          outcome: 'completed',
          started_at: '2026-07-20T11:05:00Z',
          finished_at: '2026-07-20T12:00:00Z',
          duration_minutes: 55,
          awx_job_id: 'awx-job-2001',
          session_id: 'ses-pool-dev-001',
          input_tokens: 9000,
          output_tokens: 2800,
          cache_read_tokens: 5000,
          cache_write_tokens: 100,
          estimated_cost_usd: 0.06
        },
        {
          phase: 'review',
          status: 'completed',
          outcome: 'changes_requested',
          started_at: '2026-07-20T12:30:00Z',
          finished_at: '2026-07-20T12:45:00Z',
          duration_minutes: 15,
          awx_job_id: 'awx-job-2002',
          session_id: 'ses-pool-review-001',
          input_tokens: 4000,
          output_tokens: 700,
          cache_read_tokens: 1500,
          cache_write_tokens: 0,
          estimated_cost_usd: 0.02
        },
        {
          phase: 'develop',
          status: 'completed',
          outcome: 'completed',
          started_at: '2026-07-21T09:00:00Z',
          finished_at: '2026-07-21T10:30:00Z',
          duration_minutes: 90,
          awx_job_id: 'awx-job-2003',
          session_id: 'ses-pool-dev-002',
          input_tokens: 15000,
          output_tokens: 4500,
          cache_read_tokens: 10000,
          cache_write_tokens: 300,
          estimated_cost_usd: 0.10
        },
        {
          phase: 'review',
          status: 'completed',
          outcome: 'changes_requested',
          started_at: '2026-07-21T11:00:00Z',
          finished_at: '2026-07-21T11:20:00Z',
          duration_minutes: 20,
          awx_job_id: 'awx-job-2004',
          session_id: 'ses-pool-review-002',
          input_tokens: 5500,
          output_tokens: 900,
          cache_read_tokens: 2200,
          cache_write_tokens: 0,
          estimated_cost_usd: 0.03
        },
        {
          phase: 'develop',
          status: 'completed',
          outcome: 'completed',
          started_at: '2026-07-22T08:30:00Z',
          finished_at: '2026-07-22T10:00:00Z',
          duration_minutes: 90,
          awx_job_id: 'awx-job-2005',
          session_id: 'ses-pool-dev-003',
          input_tokens: 16000,
          output_tokens: 4800,
          cache_read_tokens: 11000,
          cache_write_tokens: 350,
          estimated_cost_usd: 0.11
        }
      ],
      run_status: 'running',
      engineering_outcome: {
        status: 'open',
        merged_at: null,
        change_request_ids: ['cloudnative-pg/cloudnative-pg#6'],
        resolved_issue_ids: []
      }
    };
  }

  /** Deterministic fixture: a repeated-review lifecycle with a failed run. */
  function repeatedReviewFixture() {
    return {
      change_request: {
        provider: 'github',
        repository: 'acme/data-pipeline',
        resource_type: 'change_request',
        external_id: '88',
        title: 'Refactor ETL scheduler',
        opened_at: '2026-08-10T08:00:00Z',
        merged_at: null,
        state: 'opened'
      },
      linked_issues: [],
      executions: [
        {
          phase: 'develop',
          status: 'completed',
          outcome: 'completed',
          started_at: '2026-08-10T08:05:00Z',
          finished_at: '2026-08-10T09:30:00Z',
          duration_minutes: 85,
          awx_job_id: 'awx-job-3001',
          session_id: 'ses-etl-dev-001',
          input_tokens: 20000,
          output_tokens: 6000,
          cache_read_tokens: 14000,
          cache_write_tokens: 400,
          estimated_cost_usd: 0.14
        },
        {
          phase: 'review',
          status: 'completed',
          outcome: 'changes_requested',
          started_at: '2026-08-10T10:00:00Z',
          finished_at: '2026-08-10T10:15:00Z',
          duration_minutes: 15,
          awx_job_id: 'awx-job-3002',
          session_id: 'ses-etl-review-001',
          input_tokens: 6000,
          output_tokens: 1000,
          cache_read_tokens: 3000,
          cache_write_tokens: 0,
          estimated_cost_usd: 0.03
        },
        {
          phase: 'develop',
          status: 'completed',
          outcome: 'completed',
          started_at: '2026-08-11T09:00:00Z',
          finished_at: '2026-08-11T10:45:00Z',
          duration_minutes: 105,
          awx_job_id: 'awx-job-3003',
          session_id: 'ses-etl-dev-002',
          input_tokens: 22000,
          output_tokens: 6500,
          cache_read_tokens: 15000,
          cache_write_tokens: 450,
          estimated_cost_usd: 0.15
        },
        {
          phase: 'review',
          status: 'failed',
          outcome: null,
          started_at: '2026-08-11T11:00:00Z',
          finished_at: '2026-08-11T11:05:00Z',
          duration_minutes: 5,
          awx_job_id: 'awx-job-3004',
          session_id: null,
          input_tokens: 0,
          output_tokens: 0,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
          estimated_cost_usd: 0
        },
        {
          phase: 'develop',
          status: 'running',
          outcome: null,
          started_at: '2026-08-12T08:00:00Z',
          finished_at: null,
          duration_minutes: null,
          awx_job_id: 'awx-job-3005',
          session_id: 'ses-etl-dev-003',
          input_tokens: 8000,
          output_tokens: 2000,
          cache_read_tokens: 4000,
          cache_write_tokens: 100,
          estimated_cost_usd: 0.04
        }
      ],
      run_status: 'running',
      engineering_outcome: {
        status: 'open',
        merged_at: null,
        change_request_ids: ['acme/data-pipeline#88'],
        resolved_issue_ids: []
      }
    };
  }

  /** Build the provenance timeline data from a fixture or API response.
   *  Pure — no DOM or fetch access.  Normalizes the input into the shape
   *  the render functions expect: change_request, linked_issues, executions
   *  (sorted chronologically), run_status, and engineering_outcome. */
  function buildProvenanceTimeline(data) {
    if (!data) return null;
    var executions = (data.executions || []).slice()
      .sort(function (a, b) {
        return new Date(a.started_at) - new Date(b.started_at);
      });
    return {
      change_request: data.change_request || null,
      linked_issues: data.linked_issues || [],
      executions: executions,
      run_status: data.run_status || 'unknown',
      engineering_outcome: data.engineering_outcome || null
    };
  }

  /** Render the full provenance timeline into the detail overlay body.
   *  Pure string builder — returns HTML.  Handles loading, empty, stale,
   *  partial, and error states via the `state` parameter. */
  function renderProvenanceTimeline(data, state) {
    if (state === 'loading') {
      return '<p class="empty-state">Loading provenance timeline&hellip;</p>';
    }
    if (state === 'error') {
      return '<p class="empty-state">Failed to load provenance timeline</p>';
    }
    var timeline = buildProvenanceTimeline(data);
    if (!timeline || !timeline.change_request) {
      return '<p class="empty-state">No provenance data available</p>';
    }

    var cr = timeline.change_request;
    var html = '<div class="prov-timeline">';

    // ── Change Request Header ──
    html += '<div class="prov-section">';
    html += '<div class="prov-section-title">Change Request</div>';
    html += '<div class="prov-cr-card">';
    html += '<div class="prov-cr-head">';
    html += '<span class="prov-cr-id">' + escHtml(cr.provider + '/' + cr.repository + '#' + cr.external_id) + '</span>';
    html += badge(cr.state || 'unknown', stateBadgeForCrState(cr.state)).outerHTML;
    html += '</div>';
    html += '<div class="prov-cr-meta">';
    html += escHtml(cr.title || '--');
    if (cr.opened_at) html += ' &middot; opened ' + fmtDT(cr.opened_at);
    if (cr.merged_at) html += ' &middot; merged ' + fmtDT(cr.merged_at);
    html += '</div>';
    html += '</div></div>';

    // ── Linked Issues ──
    html += '<div class="prov-section">';
    html += '<div class="prov-section-title">Linked Issues (' + timeline.linked_issues.length + ')</div>';
    if (timeline.linked_issues.length === 0) {
      html += '<div class="prov-empty">No linked issues</div>';
    } else {
      html += '<div class="prov-issues-list">';
      timeline.linked_issues.forEach(function (issue) {
        html += '<div class="prov-issue-card">';
        html += '<div class="prov-issue-head">';
        html += '<span class="prov-issue-id">' + escHtml(issue.issue_repository + '#' + issue.issue_number) + '</span>';
        html += '<span class="prov-issue-kind">' + escHtml(issue.relationship_kind) + '</span>';
        if (issue.closure_status) {
          html += badge(issue.closure_status, closureStatusBadgeClass(issue.closure_status)).outerHTML;
        }
        html += '</div>';
        // Show different repos when they differ
        if (issue.issue_repository !== cr.repository) {
          html += '<div class="prov-issue-cross-repo">';
          html += 'cross-repo: ' + escHtml(cr.repository) + ' \u2192 ' + escHtml(issue.issue_repository);
          html += '</div>';
        }
        html += '</div>';
      });
      html += '</div>';
    }
    html += '</div>';

    // ── Execution Timeline ──
    html += '<div class="prov-section">';
    html += '<div class="prov-section-title">Execution Timeline (' + timeline.executions.length + ')</div>';
    if (timeline.executions.length === 0) {
      html += '<div class="prov-empty">No executions recorded</div>';
    } else {
      html += '<div class="prov-exec-list">';
      timeline.executions.forEach(function (ex, idx) {
        html += renderProvenanceExecution(ex, idx, timeline.executions.length);
      });
      html += '</div>';
    }
    html += '</div>';

    // ── Merge State ──
    html += '<div class="prov-section">';
    html += '<div class="prov-section-title">Merge State</div>';
    html += '<div class="prov-merge-card">';
    if (cr.merged_at) {
      html += '<span class="prov-merge-badge prov-merged">merged</span>';
      html += '<span class="prov-merge-time">Merged ' + fmtDT(cr.merged_at) + '</span>';
    } else {
      html += '<span class="prov-merge-badge prov-not-merged">not merged</span>';
    }
    html += '</div></div>';

    // ── Issue Closure State ──
    html += '<div class="prov-section">';
    html += '<div class="prov-section-title">Issue Closure State</div>';
    html += '<div class="prov-closure-card">';
    var outcomes = timeline.engineering_outcome;
    if (outcomes && outcomes.resolved_issue_ids && outcomes.resolved_issue_ids.length > 0) {
      html += '<span class="prov-closure-resolved">resolved</span>';
      html += '<span class="prov-closure-issues">' + escHtml(outcomes.resolved_issue_ids.join(', ')) + '</span>';
    } else if (timeline.linked_issues.length > 0) {
      html += '<span class="prov-closure-pending">pending</span>';
    } else {
      html += '<span class="prov-empty">no linked issues</span>';
    }
    html += '</div></div>';

    // ── Engineering Outcome (distinct from RunStatus) ──
    html += '<div class="prov-section">';
    html += '<div class="prov-section-title">Engineering Outcome</div>';
    html += '<div class="prov-outcome-card">';
    if (outcomes && outcomes.status) {
      html += badge(outcomeStatusLabel(outcomes.status), outcomeStatusBadgeClass(outcomes.status)).outerHTML;
      if (outcomes.change_request_ids && outcomes.change_request_ids.length) {
        html += '<span class="prov-outcome-detail">change requests: ' + escHtml(outcomes.change_request_ids.join(', ')) + '</span>';
      }
    } else {
      html += '<span class="prov-empty">no outcome recorded</span>';
    }
    html += '</div>';

    // RunStatus badge — rendered DISTINCTLY from EngineeringOutcomeStatus
    html += '<div class="prov-run-status-row">';
    html += '<span class="prov-run-status-label">Run Status:</span>';
    html += badge(timeline.run_status || 'unknown', afkRunStatusBadgeClass(timeline.run_status)).outerHTML;
    html += '</div>';
    html += '</div>';

    html += '</div>'; // .prov-timeline
    return html;
  }

  /** Render one execution entry in the timeline.  Pure string builder. */
  function renderProvenanceExecution(ex, idx, totalExecutions) {
    var phaseLabel = ex.phase === 'develop' ? 'Develop' : ex.phase === 'review' ? 'Review' : (ex.phase || 'unknown');
    var statusCls = statusBadgeClass(ex.status);
    var outcomeLabel = ex.outcome || '--';
    var duration = fmtDuration(ex.started_at, ex.finished_at);
    var totalTokens = (ex.input_tokens || 0) + (ex.output_tokens || 0) +
                      (ex.cache_read_tokens || 0) + (ex.cache_write_tokens || 0);

    var html = '<div class="prov-exec-item">';
    html += '<div class="prov-exec-connector">';
    html += '<span class="prov-exec-dot prov-exec-dot-' + escHtml(ex.phase || 'unknown') + '"></span>';
    if (idx < totalExecutions - 1) html += '<span class="prov-exec-line"></span>';
    html += '</div>';
    html += '<div class="prov-exec-content">';
    html += '<div class="prov-exec-head">';
    html += '<span class="prov-exec-phase">' + escHtml(phaseLabel) + '</span>';
    html += badge(ex.status || '--', statusCls).outerHTML;
    if (ex.outcome) {
      html += ' <span class="prov-exec-outcome">' + escHtml(outcomeLabel) + '</span>';
    }
    html += '</div>';
    html += '<div class="prov-exec-meta">';
    html += '<span class="prov-exec-time">' + fmtDT(ex.started_at) + '</span>';
    html += ' &middot; <span class="prov-exec-duration">' + duration + '</span>';
    if (ex.awx_job_id) {
      html += ' &middot; <span class="prov-exec-awx">AWX: ' + escHtml(ex.awx_job_id) + '</span>';
    }
    html += '</div>';
    if (ex.session_id) {
      html += '<div class="prov-exec-session">';
      html += 'Session: <span class="prov-exec-session-id">' + escHtml(ex.session_id) + '</span>';
      html += '</div>';
    }
    if (totalTokens > 0 || (ex.estimated_cost_usd || 0) > 0) {
      html += '<div class="prov-exec-usage">';
      html += fmtTokenBreakdownCompact(ex.input_tokens, ex.output_tokens,
        ex.cache_read_tokens, ex.cache_write_tokens);
      html += ' &middot; Est. Cost: ' + fmtCost(ex.estimated_cost_usd);
      html += '</div>';
    }
    html += '</div>'; // .prov-exec-content
    html += '</div>'; // .prov-exec-item
    return html;
  }

  /** Map a change request state to a badge CSS class. */
  function stateBadgeForCrState(state) {
    if (state === 'merged') return 'badge-merged';
    if (state === 'opened') return 'badge-open';
    if (state === 'closed') return 'badge-closed';
    return 'badge-unknown';
  }

  /** Map a closure episode status to a badge CSS class. */
  function closureStatusBadgeClass(status) {
    if (status === 'inferred') return 'badge-completed';
    if (status === 'pending') return 'badge-stale';
    if (status === 'awaiting_closure') return 'badge-stale';
    if (status === 'unmatched') return 'badge-failed';
    if (status === 'ambiguous') return 'badge-unknown';
    if (status === 'superseded') return 'badge-unknown';
    return 'badge-unknown';
  }

  // ── AFK Outcomes view (issue #453) ─────────────────────────────────────
  // The first UI for the AFK outcomes domain: an "AFK Outcomes" tab whose runs
  // list (GET /api/v1/afk-outcomes/runs) opens a detail overlay rendering the
  // full chain (GET /api/v1/afk-outcomes/runs/{afk_run_id}) in the canonical
  // example order — issue → run → sessions → agents → tokens/cost →
  // change_request → commits → review cycles → merged.  Every derived link
  // displays its
  // correlation provenance (method / confidence / evidence / resolver_version)
  // and provisional/inferred links are visibly marked (never rendered like
  // explicit links).  Outcome uses the locked EngineeringOutcomeStatus
  // vocabulary via outcomeStatusBadgeClass/outcomeStatusLabel.

  /** Render the AFK runs list table: one row per run with Status (RunStatus),
   *  Outcome (EngineeringOutcomeStatus), provider, started, and last-seen.
   *  Rows open the /runs/{afk_run_id} detail overlay.  Follows the agent-runs
   *  panel conventions: freshness guard, empty/error states, escHtml on every
   *  interpolated value. */
  function renderAfkOutcomesTable(data) {
    applyPanelFreshness('afk-outcomes');
    if (!shouldRenderPanel(panelStates, 'afk-outcomes')) return; // failed fetch → keep previous rows

    var runs = data && data.items;
    if (!runs || runs.length === 0) {
      var errSuffix = afkRunsFetchError
        ? ' <span class="fetch-error" title="' + escHtml(afkRunsFetchError) + '">\u26A0 Fetch error</span>'
        : '';
      els.afkRunsTbody.innerHTML = '<tr><td colspan="6" class="empty-state">No AFK runs' + errSuffix + '</td></tr>';
      return;
    }

    var html = '';
    runs.forEach(function (r) {
      var runStatusCls = afkRunStatusBadgeClass(r.status);
      var outcomeCls = outcomeStatusBadgeClass(r.outcome_status);
      var displayTitle = r.title || shortUUID(r.afk_run_id);
      html += '<tr class="afk-run-row" data-id="' + escHtml(r.afk_run_id) + '" tabindex="0">' +
        '<td class="clickable afk-run-title" data-label="Run">' + escHtml(displayTitle) + '</td>' +
        '<td data-label="Status">' + badge(r.status || '--', runStatusCls).outerHTML + '</td>' +
        '<td data-label="Outcome">' + badge(outcomeStatusLabel(r.outcome_status), outcomeCls).outerHTML + '</td>' +
        '<td data-label="Provider">' + escHtml(r.provider || '--') + '</td>' +
        '<td data-label="Started">' + fmtDT(r.started_at) + '</td>' +
        '<td data-label="Last Seen">' + fmtDT(r.last_seen_at) + '</td>' +
        '</tr>';
    });

    els.afkRunsTbody.innerHTML = html;

    var rows = els.afkRunsTbody.querySelectorAll('.afk-run-row');
    rows.forEach(function (row) {
      row.addEventListener('click', function () {
        var id = row.getAttribute('data-id');
        if (id) openAfkRunDetail(id);
      });
      row.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          var id = row.getAttribute('data-id');
          if (id) openAfkRunDetail(id);
        }
      });
    });
  }

  /** Render the unresolved-relationships panel: one row per uncertain
   *  correlation (ambiguous, unmatched, parked) from the AFK runs data.
   *  Follows the established panel conventions: freshness guard, empty/error
   *  states, escHtml on every interpolated value. */
  function renderUnresolvedRelationshipsPanel(data) {
    applyPanelFreshness('unresolved-relationships');
    if (!shouldRenderPanel(panelStates, 'unresolved-relationships')) return;

    if (!els.unresolvedTbody) return;

    // Build the list of unresolved items from all AFK runs
    var allItems = [];
    var runs = data && data.items;
    if (runs && runs.length) {
      runs.forEach(function (r) {
        var runItems = r.unresolved || [];
        runItems.forEach(function (u) {
          allItems.push({
            entity_id: u.entity_id || '--',
            entity_type: u.entity_type || '',
            state: u.reason || u.state || 'unresolved',
            confidence: u.correlation_confidence,
            method: u.correlation_method,
            evidence: u.evidence || [],
            resolver_version: u.resolver_version
          });
        });
        var parkedItems = r.parked || [];
        parkedItems.forEach(function (p) {
          allItems.push({
            entity_id: p.entity_id || '--',
            entity_type: p.entity_type || '',
            state: 'parked',
            confidence: p.correlation_confidence,
            method: p.correlation_method,
            evidence: p.evidence || [],
            resolver_version: p.resolver_version
          });
        });
      });
    }

    unresolvedRelationshipsData = { items: allItems };

    if (allItems.length === 0) {
      els.unresolvedTbody.innerHTML = '<tr><td colspan="3" class="empty-state">No unresolved relationships</td></tr>';
      return;
    }

    var html = '';
    allItems.forEach(function (item) {
      html += renderUnresolvedRelationshipsRow(item);
    });
    els.unresolvedTbody.innerHTML = html;
  }

  /** Open the AFK outcome chain overlay for one run.  A 404 (unknown run)
   *  renders a distinct "not found" empty state; other failures render the
   *  generic error empty state — no unhandled exception breaks the dashboard. */
  async function openAfkRunDetail(afkRunId) {
    els.afkDetailOverlay.classList.add('visible');
    els.afkDetailBody.innerHTML = '<p class="empty-state">Loading detail&hellip;</p>';
    els.afkDetailTitle.textContent = 'AFK Outcome Chain';

    try {
      var detail = await apiFetch('/api/v1/afk-outcomes/runs/' + encodeURIComponent(afkRunId));
      renderAfkRunDetail(detail);
    } catch (e) {
      var notFound = /404/.test(e && e.message);
      els.afkDetailBody.innerHTML = '<p class="empty-state">' +
        (notFound ? 'AFK run not found' : 'Failed to load AFK outcome chain: ' + escHtml(e.message)) +
        '</p>';
      console.error('AFK outcome detail fetch error:', e);
    }
  }

  /** Render the full AFK outcome chain into the detail overlay body. */
  function renderAfkRunDetail(detail) {
    if (!detail || !detail.run) {
      els.afkDetailBody.innerHTML = '<p class="empty-state">No AFK outcome data available</p>';
      return;
    }

    var run = detail.run;
    els.afkDetailTitle.textContent = escHtml(run.title || run.afk_run_id || 'AFK Outcome Chain');

    var html = '<div class="afk-chain">';
    buildAfkChain(detail).forEach(function (step) {
      html += renderAfkChainStep(step);
    });
    html += '</div>';

    els.afkDetailBody.innerHTML = html;
    wireAfkSessionDrilldown();
  }

  /** Wire the resolved-session drill-down links (issue #473): clicking (or
   *  activating via Enter/Space) a session with a resolvable internal id opens
   *  the Agent Run detail overlay for that session; unresolved sessions render
   *  no link and stay inert.  The AFK chain overlay is closed first so the
   *  Agent Run detail is not hidden behind it (both overlays share the same
   *  z-index, and the AFK overlay paints on top when both are visible). */
  function wireAfkSessionDrilldown() {
    var links = els.afkDetailBody.querySelectorAll('.afk-session-clickable');
    links.forEach(function (item) {
      item.addEventListener('click', function () {
        var sid = item.getAttribute('data-session-id');
        if (sid) openAfkSessionDrilldown(sid);
      });
      item.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          var sid = item.getAttribute('data-session-id');
          if (sid) openAfkSessionDrilldown(sid);
        }
      });
    });
  }

  /** Close the AFK chain overlay, then open the Agent Run detail overlay for
   *  the given internal session id (issue #473).  Uses the same `visible`-class
   *  convention as the other overlay open/close paths: removing `visible` from
   *  the AFK overlay surfaces the Agent Run overlay instead of leaving it
   *  stacked behind the AFK backdrop. */
  function openAfkSessionDrilldown(sessionId) {
    els.afkDetailOverlay.classList.remove('visible');
    openAgentRunDetail(sessionId);
  }

  // ── Change Request list view (issue #613) ───────────────────────────────
  // The primary AFK Outcomes presentation: one row per change request
  // (provider + repository + PR/MR number) served by the Gateway-owned
  // summary contract (GET /api/v1/afk-outcomes/change-requests).  Data
  // composition happens in the #612 adapters; this layer only builds the
  // request URL, renders rows, and opens the identity-keyed detail flow.
  // Ordering, aggregation, and unlinked-execution exclusion belong to the
  // query layer — the browser never re-sorts or re-aggregates rows.

  /** Build the change-request summary list URL from the active filters and
   *  the shared dashboard date range (activity window).  Filter names use
   *  the #610 query contract exactly (provider / repository /
   *  provider_state / automation_state); the shared date range feeds
   *  activity_from/activity_to — no second date picker (PRD).  Pure — no
   *  DOM or fetch access.
   *  @param {Object|null} filters - {provider, repository, providerState,
   *                                 automationState}; empty values omitted
   *  @param {Object|null} dateRangeState - the shared dashboard date range
   *  @param {number} [limit] - page size (default AFK_CR_LIMIT)
   *  @param {number} [offset] - page offset (default 0)
   *  @returns {string} the API path with query string */
  function buildChangeRequestListUrl(filters, dateRangeState, limit, offset) {
    var f = filters || {};
    var params = [];
    if (f.provider) params.push('provider=' + encodeURIComponent(f.provider));
    if (f.repository) params.push('repository=' + encodeURIComponent(f.repository));
    if (f.providerState) params.push('provider_state=' + encodeURIComponent(f.providerState));
    if (f.automationState) params.push('automation_state=' + encodeURIComponent(f.automationState));
    var range = resolveDateRange(dateRangeState || { preset: 'this-month' });
    if (range && range.startDate && !isNaN(range.startDate.getTime())) {
      params.push('activity_from=' + encodeURIComponent(range.startDate.toISOString()));
    }
    if (range && range.endDate && !isNaN(range.endDate.getTime())) {
      params.push('activity_to=' + encodeURIComponent(range.endDate.toISOString()));
    }
    params.push('limit=' + (limit != null ? limit : AFK_CR_LIMIT));
    // Always emit offset when it is a valid non-negative number (0 included)
    // so the URL mirrors the server-side page (offset = (page-1) * limit),
    // matching the agent-runs builder's always-emit convention.
    if (typeof offset === 'number' && offset >= 0 && Number.isInteger(offset)) {
      params.push('offset=' + offset);
    }
    return '/api/v1/afk-outcomes/change-requests' + (params.length ? '?' + params.join('&') : '');
  }

  /** Parse change-request list pagination from a URL query string.
   *  Reads `limit` (page size) and `offset` (zero-based row offset); the
   *  current page is derived as floor(offset / limit) + 1.  Missing or
   *  malformed values fall back to the defaults: page 1 of AFK_CR_LIMIT
   *  (100) rows.  Pure — no DOM, location, or fetch access. */
  function parseChangeRequestPagination(queryString) {
    var page = 1;
    var pageSize = AFK_CR_LIMIT;
    var params = new URLSearchParams(queryString || '');
    var rawLimit = params.get('limit');
    var rawOffset = params.get('offset');
    var nLimit = Number(rawLimit);
    var nOffset = Number(rawOffset);
    if (rawLimit !== null && Number.isInteger(nLimit) && nLimit >= 1) {
      pageSize = nLimit;
    }
    if (rawOffset !== null && Number.isInteger(nOffset) && nOffset >= 0) {
      page = Math.floor(nOffset / pageSize) + 1;
    }
    return { page: page, pageSize: pageSize };
  }

  /** Read `limit`/`offset` from the current URL into the change-request
   *  pagination closure state.  Called on dashboard load so a deep link
   *  such as ?limit=100&offset=100 loads the corresponding change-request
   *  page; the translation happens in fetchChangeRequestsAndRender on the
   *  next fetch. */
  function readChangeRequestPaginationFromUrl() {
    var query = (typeof location !== 'undefined' && location.search) || '';
    var pagination = parseChangeRequestPagination(query);
    afkCrPage = pagination.page;
    afkCrPageSize = pagination.pageSize;
  }

  /** Build the dashboard URL carrying the given change-request pagination
   *  state, keeping any other query parameters already present in the URL. */
  function changeRequestsUrlWithPagination(page, pageSize) {
    var params = new URLSearchParams(
      (typeof location !== 'undefined' && location.search) || '');
    params.set('limit', String(pageSize));
    params.set('offset', String((page - 1) * pageSize));
    var path = (typeof location !== 'undefined' && location.pathname) || '';
    return path + '?' + params.toString();
  }

  /** Set the change-request list page and persist it in the URL via browser
   *  history.  Invalid page values fall back to page 1.  The URL update
   *  itself never changes rows — rows only change through the normal fetch
   *  path (buildChangeRequestListUrl → fetchChangeRequestsAndRender). */
  function setChangeRequestPage(page) {
    var parsed = parseChangeRequestPagination(
      'limit=' + afkCrPageSize + '&offset=' + ((page - 1) * afkCrPageSize));
    afkCrPage = parsed.page;
    var url = changeRequestsUrlWithPagination(afkCrPage, afkCrPageSize);
    if (typeof history !== 'undefined' && typeof history.pushState === 'function') {
      history.pushState({}, '', url);
    }
  }

  /** Compute the nearest valid change-request page for a fetched result
   *  total.  When the result set shrinks — change requests deleted, or the
   *  list narrowed elsewhere — the currently selected page may exceed the
   *  new page count; the UI must land on the nearest valid page instead of
   *  rendering an empty offset.  An empty result (total=0) resolves to
   *  page 1.  Pure — no DOM, location, or fetch access. */
  function nearestValidChangeRequestPage(total, currentPage, pageSize) {
    var pageCount = Math.ceil(total / pageSize);
    var current = Math.max(currentPage, 1);
    return Math.min(current, Math.max(1, pageCount));
  }

  /** Correct the change-request page state after a fetch when the result
   *  total no longer covers the current page.  When the fetched total
   *  implies fewer pages than the current page, the closure page state
   *  moves to the nearest valid page and the URL is REPLACED
   *  (history.replaceState) so the fallback does not add a browser-history
   *  entry.  The caller then refetches the corrected page through the
   *  normal fetch path — this hook only fixes state + URL.  Returns true
   *  when a fallback was applied, false otherwise (no data, a still-valid
   *  page, or an empty result already on page 1 — so a refetch can never
   *  loop). */
  function applyChangeRequestPageFallback(data) {
    if (!data) return false;
    var total = (typeof data.total === 'number')
      ? data.total
      : (data.items ? data.items.length : 0);
    var nearest = nearestValidChangeRequestPage(total, afkCrPage, afkCrPageSize);
    if (nearest === afkCrPage) return false;
    afkCrPage = nearest;
    var url = changeRequestsUrlWithPagination(afkCrPage, afkCrPageSize);
    if (typeof history !== 'undefined' && typeof history.replaceState === 'function') {
      history.replaceState({}, '', url);
    }
    return true;
  }

  /** Build the provider-scoped change-request detail path (planned #611
   *  contract): the identity tuple is the navigation key — never an
   *  internal AFK Run ID (PRD story 14).  Pure — no DOM or fetch access.
   *  @param {string|null} provider
   *  @param {string|null} repository
   *  @param {*} externalId
   *  @returns {string} the API path */
  function buildChangeRequestDetailPath(provider, repository, externalId) {
    return '/api/v1/afk-outcomes/change-requests/' +
      encodeURIComponent(provider || '') + '/' +
      encodeURIComponent(repository || '') + '/' +
      encodeURIComponent(externalId != null ? String(externalId) : '');
  }

  /** Compose the flat identity key of one change request (used for row
   *  data-attributes and selection identity).  Pure. */
  function changeRequestKey(provider, repository, externalId) {
    return [provider || '', repository || '', externalId != null ? String(externalId) : ''].join('/');
  }

  /** Adapt a summary list response into stable view models through the #612
   *  adapter module.  Returns [] when the module is absent (defensive — the
   *  render layer then shows the empty state rather than crashing).
   *  A field-vocabulary bridge normalizes the #610 contract's freshness
   *  column name (`latest_linked_activity`) onto the adapter's expected
   *  name (`latest_activity_at`), filling only when absent (non-erasing:
   *  a present value always wins) — a pure alias, never a browser-side
   *  join.
   *  @param {Object|Array|null} data
   *  @returns {Array} stable change-request view models */
  function adaptChangeRequestSummaries(data) {
    if (!(ChangeRequestAdapters && typeof ChangeRequestAdapters.adaptChangeRequestSummaryList === 'function')) {
      return [];
    }
    var items = Array.isArray(data) ? data : ((data && data.items) || []);
    var bridged = items.map(function (item) {
      if (!item || item.latest_activity_at != null || item.latest_linked_activity == null) {
        return item;
      }
      return Object.assign({}, item, { latest_activity_at: item.latest_linked_activity });
    });
    return ChangeRequestAdapters.adaptChangeRequestSummaryList(bridged);
  }

  /** Render one change-request summary row: provider, repository, PR/MR
   *  identity, provider state, AFK automation state (dual statuses rendered
   *  independently), total cost (USD or 'Cost unavailable'), and latest
   *  linked activity.  Pure — returns an HTML string; every interpolated
   *  value is escaped. */
  function renderChangeRequestSummaryRow(view) {
    var id = view.identity;
    var crLabel = (id.external_id)
      ? view.providerTerm + ' #' + id.external_id
      : view.providerTerm + ' ' + (view.displayId !== '--' ? view.displayId : '--');
    return '<tr class="afk-cr-list-row" tabindex="0" ' +
        'data-provider="' + escHtml(id.provider) + '" ' +
        'data-repository="' + escHtml(id.repository) + '" ' +
        'data-external-id="' + escHtml(id.external_id) + '">' +
      '<td data-label="Provider">' + badge(id.provider || '--', 'badge-provider').outerHTML + '</td>' +
      '<td data-label="Repository">' + escHtml(id.repository || '--') + '</td>' +
      '<td data-label="' + escHtml(view.providerTerm) + '">' + escHtml(crLabel) + '</td>' +
      '<td data-label="Provider State">' +
        badge(view.providerState.label, view.providerState.badgeClass).outerHTML + '</td>' +
      '<td data-label="AFK Automation">' +
        badge(view.afkAutomationState.label, view.afkAutomationState.badgeClass).outerHTML + '</td>' +
      '<td data-label="Cost" class="afk-cr-cost-cell">' + escHtml(view.cost.label) + '</td>' +
      '<td data-label="Latest Activity">' + fmtDT(view.latestActivityAt) + '</td>' +
      '</tr>';
  }

  /** Render the primary change-request list: one row per change request in
   *  the exact order the Gateway returned it (newest linked activity first —
   *  the query layer's ordering policy).  Follows the shared panel
   *  conventions: freshness guard, empty/error states, escHtml on every
   *  interpolated value.  Rows open the identity-keyed detail flow. */
  function renderChangeRequestSummaryTable(data) {
    applyPanelFreshness('afk-cr-list');
    if (!shouldRenderPanel(panelStates, 'afk-cr-list')) return; // failed fetch → keep previous rows
    if (!els.afkCrListTbody) return;

    var views = adaptChangeRequestSummaries(data);
    if (views.length === 0) {
      var errSuffix = afkCrFetchError
        ? ' <span class="fetch-error" title="' + escHtml(afkCrFetchError) + '">\u26A0 Fetch error</span>'
        : '';
      els.afkCrListTbody.innerHTML =
        '<tr><td colspan="7" class="empty-state">No change requests' + errSuffix + '</td></tr>';
      return;
    }

    var html = views.map(renderChangeRequestSummaryRow).join('');
    els.afkCrListTbody.innerHTML = html;

    var rows = els.afkCrListTbody.querySelectorAll('.afk-cr-list-row');
    rows.forEach(function (row) {
      var provider = row.getAttribute('data-provider');
      var repository = row.getAttribute('data-repository');
      var externalId = row.getAttribute('data-external-id');
      function activate() {
        if (provider && repository && externalId) {
          openChangeRequestDetail(provider, repository, externalId);
        }
      }
      row.addEventListener('click', activate);
      row.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          activate();
        }
      });
    });
  }

  /** Read the change-request filter controls into the filter state shape.
   *  Empty controls are omitted (the contract returns the unfiltered set). */
  function readChangeRequestFiltersFromUI() {
    var filters = {};
    if (els.afkCrFilterProvider && els.afkCrFilterProvider.value) {
      filters.provider = els.afkCrFilterProvider.value;
    }
    if (els.afkCrFilterRepository && els.afkCrFilterRepository.value) {
      filters.repository = els.afkCrFilterRepository.value.trim();
    }
    if (els.afkCrFilterProviderState && els.afkCrFilterProviderState.value) {
      filters.providerState = els.afkCrFilterProviderState.value;
    }
    if (els.afkCrFilterAutomationState && els.afkCrFilterAutomationState.value) {
      filters.automationState = els.afkCrFilterAutomationState.value;
    }
    return filters;
  }

  /** Sync the filter controls to a filter state (used by Clear). */
  function syncChangeRequestFilterUI(filters) {
    var f = filters || {};
    if (els.afkCrFilterProvider) els.afkCrFilterProvider.value = f.provider || '';
    if (els.afkCrFilterRepository) els.afkCrFilterRepository.value = f.repository || '';
    if (els.afkCrFilterProviderState) els.afkCrFilterProviderState.value = f.providerState || '';
    if (els.afkCrFilterAutomationState) els.afkCrFilterAutomationState.value = f.automationState || '';
  }

  /** Apply the current filter controls and re-fetch the summary list through
   *  the contract (filters ride the query — never client-side re-filtering).
   *  A filter change re-scopes the result set, so pagination resets to
   *  page 1 and the URL state is REPLACED rather than pushed. */
  function applyChangeRequestFilters() {
    afkCrFilters = readChangeRequestFiltersFromUI();
    afkCrPage = 1;
    var url = changeRequestsUrlWithPagination(afkCrPage, afkCrPageSize);
    if (typeof history !== 'undefined' && typeof history.replaceState === 'function') {
      history.replaceState({}, '', url);
    }
    return fetchChangeRequestsAndRender();
  }

  /** Clear the change-request filters (back to the full list) and re-fetch.
   *  Resets pagination to page 1 and replaces the URL state, mirroring
   *  applyChangeRequestFilters. */
  function clearChangeRequestFilters() {
    afkCrFilters = { provider: '', repository: '', providerState: '', automationState: '' };
    syncChangeRequestFilterUI(afkCrFilters);
    afkCrPage = 1;
    var url = changeRequestsUrlWithPagination(afkCrPage, afkCrPageSize);
    if (typeof history !== 'undefined' && typeof history.replaceState === 'function') {
      history.replaceState({}, '', url);
    }
    return fetchChangeRequestsAndRender();
  }

  /** Fetch the change-request summary page described by the current filter
   *  state and re-render the table (issue #613).  Mirrors
   *  fetchAgentRunsAndRender: an independent fetch keeps the panel's
   *  freshness state, preserves the previous rows during loading and
   *  failures, and never blocks the rest of the dashboard.  The fetch
   *  carries the current pagination state — offset = (page - 1) * limit —
   *  so paging, filtering, and the dashboard refresh all ride the same
   *  path. */
  function fetchChangeRequestsAndRender() {
    var prev = panelStates['afk-cr-list'];
    setPanelState('afk-cr-list', 'refreshing', prev ? prev.updatedAt : null);
    var offset = (afkCrPage - 1) * afkCrPageSize;
    var url = buildChangeRequestListUrl(afkCrFilters, dateRangeState, afkCrPageSize, offset);
    return apiFetch(url).then(function (data) {
      afkCrData = data;
      afkCrFetchError = null;
      setPanelState('afk-cr-list', 'ok', Date.now());
      // Issue #429 pattern: when the fetched total no longer covers the
      // current page (the result set shrank), correct the page state + URL
      // and re-fetch the nearest valid page through this same path.
      if (applyChangeRequestPageFallback(data)) {
        return fetchChangeRequestsAndRender();
      }
      renderChangeRequestSummaryTable(data);
      renderChangeRequestPagination(data);
    }).catch(function (e) {
      afkCrFetchError = e.message || 'Change-request query failed';
      var prevState = panelStates['afk-cr-list'];
      setPanelState('afk-cr-list', 'stale', prevState ? prevState.updatedAt : null);
      renderChangeRequestSummaryTable(null); // keeps previous rows; label shows "Showing previous data"
      renderChangeRequestPagination(afkCrData); // keeps the last-known page info
      console.error('Change-request list fetch error:', e);
    });
  }

  /** Render the Change Request list pagination control block below the
   *  panel: Previous / Next plus the numbered page items computed by
   *  computePageItems from the API response `total` and the current page
   *  size (AFK_CR_LIMIT).  Previous is disabled on page 1, Next on the
   *  final page, and the current page carries aria-current="page".
   *  Clicking a control persists the page via setChangeRequestPage and
   *  re-fetches that server-side page through fetchChangeRequestsAndRender,
   *  preserving active filters.  Change-request row content, columns,
   *  ordering, and detail interactions are untouched — this block only
   *  re-requests the same endpoint with a different offset. */
  function renderChangeRequestPagination(data) {
    if (!els.afkCrPagination) return;
    // Failed fetch → keep the previous control state (mirrors the table's
    // "keep previous rows" behavior via the same panel guard).
    if (!shouldRenderPanel(panelStates, 'afk-cr-list')) return;

    var total = (data && typeof data.total === 'number')
      ? data.total
      : (data && data.items ? data.items.length : 0);
    var pageCount = Math.ceil(total / afkCrPageSize);
    var items = computePageItems(afkCrPage, pageCount);
    if (items.length === 0) {
      els.afkCrPagination.innerHTML = '';
      return;
    }

    var html = '';
    var prevPage = afkCrPage - 1;
    var nextPage = afkCrPage + 1;

    html += '<button type="button" class="filter-clear pagination-btn" data-page="' + prevPage + '"' +
      (prevPage < 1 ? ' disabled' : '') + ' aria-label="Previous page">\u2190 Previous</button>';

    items.forEach(function (item) {
      if (item.type === 'ellipsis') {
        html += '<span class="pagination-ellipsis" aria-hidden="true">\u2026</span>';
        return;
      }
      var isCurrent = item.page === afkCrPage;
      html += '<button type="button" class="filter-clear pagination-btn' +
        (isCurrent ? ' pagination-current' : '') + '" data-page="' + item.page + '"' +
        ' aria-label="' + (isCurrent ? 'Page ' + item.page + ', current page' : 'Page ' + item.page) + '"' +
        (isCurrent ? ' aria-current="page"' : '') + '>' + item.page + '</button>';
    });

    html += '<button type="button" class="filter-clear pagination-btn" data-page="' + nextPage + '"' +
      (nextPage > pageCount ? ' disabled' : '') + ' aria-label="Next page">Next \u2192</button>';

    els.afkCrPagination.innerHTML = html;

    // Wire the page controls: selecting a page updates the pagination
    // state (setChangeRequestPage → URL history) and re-fetches that page
    // via the shared path so the active filters ride along.
    var buttons = els.afkCrPagination.querySelectorAll('button');
    buttons.forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (btn.disabled) return; // disabled buttons never fire in browsers; belt-and-braces
        var page = Number(btn.getAttribute('data-page'));
        // Clicking the already-current page is a no-op — no duplicate
        // history entry, no redundant refetch.  The current page stays
        // focusable (aria-current="page" + "current page" label unchanged).
        if (!Number.isInteger(page) || page < 1 || page === afkCrPage) return;
        setChangeRequestPage(page);
        return fetchChangeRequestsAndRender();
      });
    });
  }

  /** Open the change-request detail overlay for one row, keyed by the
   *  change-request identity tuple (never an internal AFK Run ID — PRD
   *  story 14).  A 404 (unknown change request) renders a distinct
   *  "not found" empty state; other failures render the generic error
   *  state — no unhandled exception breaks the dashboard.  Returns the
   *  fetch promise so tests can await it. */
  function openChangeRequestDetail(provider, repository, externalId) {
    selectedChangeRequest = {
      provider: provider,
      repository: repository,
      externalId: String(externalId)
    };
    if (els.crListDetailOverlay) els.crListDetailOverlay.classList.add('visible');
    if (els.crListDetailBody) els.crListDetailBody.innerHTML = '<p class="empty-state">Loading detail&hellip;</p>';
    if (els.crListDetailTitle) els.crListDetailTitle.textContent = 'Change Request';

    var path = buildChangeRequestDetailPath(provider, repository, externalId);
    return apiFetch(path).then(function (detail) {
      renderChangeRequestDetail(detail);
    }).catch(function (e) {
      var notFound = /404/.test(e && e.message);
      if (els.crListDetailBody) {
        els.crListDetailBody.innerHTML = '<p class="empty-state">' +
          (notFound ? 'Change request not found' : 'Failed to load change-request detail: ' + escHtml(e.message)) +
          '</p>';
      }
      console.error('Change-request detail fetch error:', e);
    });
  }

  /** Render the change-request detail flow (issue #613): PR/MR identity and
   *  dual statuses first, then the execution-focused summary (grouped by
   *  purpose), linked sessions, and a collapsed activity timeline.  Consumes
   *  the #612 detail adapter — the Gateway-owned composite payload, never
   *  browser-side joins. */
  function renderChangeRequestDetail(detail) {
    if (!els.crListDetailBody) return;
    var view = (ChangeRequestAdapters && typeof ChangeRequestAdapters.adaptChangeRequestDetail === 'function')
      ? ChangeRequestAdapters.adaptChangeRequestDetail(detail)
      : null;
    if (!view) {
      els.crListDetailBody.innerHTML = '<p class="empty-state">No change-request detail data available</p>';
      return;
    }
    if (els.crListDetailTitle) {
      els.crListDetailTitle.textContent = view.displayId || 'Change Request';
    }
    var html = '<div class="afk-cr-detail">' +
      renderChangeRequestDetailHeader(view) +
      renderChangeRequestExecutions(view) +
      renderChangeRequestSessions(view) +
      renderChangeRequestTimeline(view) +
      '</div>';
    els.crListDetailBody.innerHTML = html;
    wireCrDetailSessionDrilldown();
  }

  /** Render the detail header: PR/MR identity, title, provider state and
   *  AFK automation state as independent badges, and the aggregate cost.
   *  Pure — returns an HTML string. */
  function renderChangeRequestDetailHeader(view) {
    var html = '<div class="afk-cr-detail-header">' +
      '<div class="afk-cr-detail-head">' +
        '<span class="afk-cr-detail-term">' + escHtml(view.providerTerm) + '</span>' +
        '<span class="afk-cr-detail-id">' + escHtml(view.displayId) + '</span>' +
        (view.title ? ' <span class="afk-cr-detail-title">' + escHtml(view.title) + '</span>' : '') +
      '</div>' +
      '<div class="afk-cr-detail-statuses">' +
        '<span class="afk-cr-status"><span class="afk-cr-status-label">Provider</span>' +
          badge(view.providerState.label, view.providerState.badgeClass).outerHTML + '</span>' +
        '<span class="afk-cr-status"><span class="afk-cr-status-label">AFK Automation</span>' +
          badge(view.afkAutomationState.label, view.afkAutomationState.badgeClass).outerHTML + '</span>' +
        '<span class="afk-cr-status"><span class="afk-cr-status-label">Total Cost</span>' +
          '<span class="afk-cr-cost">' + escHtml(view.aggregateCost.label) + '</span></span>' +
      '</div>' +
      '</div>';
    return html;
  }

  /** Render the execution-focused summary: implementation, review, and
   *  retry executions grouped by purpose (distinct sections — PRD story
   *  17), with an explicit "no linked executions" empty state.  Any
   *  execution purpose outside the locked vocabulary is preserved under
   *  "Other" rather than hidden.  Pure — returns an HTML string. */
  function renderChangeRequestExecutions(view) {
    var html = '<div class="afk-cr-section">' +
      '<h3 class="afk-cr-section-title">Executions (' + fmtNum(view.executionCounts.total) + ')</h3>';
    var groups = [
      { key: 'implementation', label: 'Implementation' },
      { key: 'review', label: 'Review' },
      { key: 'retry', label: 'Retry' }
    ];
    var any = false;
    groups.forEach(function (g) {
      var execs = view.executions.filter(function (e) { return e.purpose.value === g.key; });
      if (!execs.length) return;
      any = true;
      html += '<div class="afk-cr-exec-group"><h4 class="afk-cr-exec-group-title">' + escHtml(g.label) + '</h4>' +
        execs.map(renderChangeRequestExecution).join('') + '</div>';
    });
    var rest = view.executions.filter(function (e) {
      return groups.every(function (g) { return e.purpose.value !== g.key; });
    });
    if (rest.length) {
      any = true;
      html += '<div class="afk-cr-exec-group"><h4 class="afk-cr-exec-group-title">Other</h4>' +
        rest.map(renderChangeRequestExecution).join('') + '</div>';
    }
    if (!any) html += '<div class="afk-empty">No linked executions</div>';
    return html + '</div>';
  }

  /** Render one execution entry: AWX job id, purpose and status/outcome
   *  badges, AWX job metadata (job template, trigger type, branch), linked
   *  session, timestamps, duration, token usage (compact Token Breakdown
   *  when telemetry exists), per-run cost, and the bounded failure summary
   *  when present.  Pure — returns an HTML string. */
  function renderChangeRequestExecution(execution) {
    var tokens = execution.tokens || {};
    var tokensAvailable = !!tokens && [tokens.inputTokens, tokens.outputTokens,
      tokens.cacheReadTokens, tokens.cacheWriteTokens].some(function (t) {
        return t != null && t !== '' && !isNaN(Number(t)) && Number(t) > 0;
      });
    var html = '<div class="afk-cr-execution">' +
      '<div class="afk-cr-execution-head">' +
        '<span class="afk-entity-id">' + escHtml(execution.awxJobId || '--') + '</span>' +
        badge(execution.purpose.label, execution.purpose.badgeClass).outerHTML +
        badge(execution.status.label, execution.status.badgeClass).outerHTML +
        (execution.outcome && execution.outcome !== execution.status.value
          ? badge(execution.outcome, afkRunStatusBadgeClass(execution.outcome)).outerHTML : '') +
      '</div>' +
      '<div class="afk-cr-execution-meta">' +
        (execution.externalSessionId ? 'session: ' + escHtml(execution.externalSessionId) : '') +
        (execution.jobTemplateId != null ? ' &middot; template: ' + escHtml(String(execution.jobTemplateId)) : '') +
        (execution.triggerType ? ' &middot; trigger: ' + escHtml(execution.triggerType) : '') +
        (execution.branch ? ' &middot; branch: ' + escHtml(execution.branch) : '') +
        (execution.startedAt ? ' &middot; started ' + fmtDT(execution.startedAt) : '') +
        (execution.finishedAt ? ' &middot; finished ' + fmtDT(execution.finishedAt) : '') +
        ' &middot; duration: ' + escHtml(execution.duration) +
        ' &middot; cost: ' + escHtml(execution.cost.label) +
      '</div>';
    if (tokensAvailable) {
      html += '<div class="afk-tokens">' +
        fmtTokenBreakdownCompact(tokens.inputTokens, tokens.outputTokens,
          tokens.cacheReadTokens, tokens.cacheWriteTokens) +
      '</div>';
    }
    if (execution.failureSummary) {
      html += '<div class="afk-cr-execution-failure">' + escHtml(execution.failureSummary) + '</div>';
    }
    return html + '</div>';
  }

  /** Render the linked sessions (reusing the AFK chain session-link
   *  renderer: compact Token Breakdown, inferred markers, and Agent Run
   *  drill-down where a resolvable internal session id exists). */
  function renderChangeRequestSessions(view) {
    var sessions = view.sessions || [];
    var html = '<div class="afk-cr-section">' +
      '<h3 class="afk-cr-section-title">Sessions (' + fmtNum(sessions.length) + ')</h3>';
    if (!sessions.length) {
      html += '<div class="afk-empty">No sessions linked</div>';
    } else {
      html += sessions.map(renderAfkSessionLink).join('');
    }
    return html + '</div>';
  }

  /** Render the provenance/activity timeline, collapsed by default
   *  (<details> without open — PRD story 24/25).  Timeline entries are
   *  structured facts; every interpolated value is escaped. */
  function renderChangeRequestTimeline(view) {
    var timeline = view.timeline;
    var html = '<div class="afk-cr-section">' +
      '<details class="afk-cr-timeline">' +
      '<summary class="afk-cr-timeline-summary">Activity timeline</summary>' +
      '<div class="afk-cr-timeline-body">';
    if (!timeline || !timeline.length) {
      html += '<div class="afk-empty">No timeline data</div>';
    } else {
      timeline.forEach(function (item) {
        html += '<div class="afk-cr-timeline-item">' +
          '<span class="afk-cr-timeline-time">' +
            fmtDT(item.occurred_at || item.timestamp || item.at) + '</span>' +
          '<span class="afk-cr-timeline-type">' +
            escHtml(item.event_type || item.type || 'event') + '</span>' +
          '<span class="afk-cr-timeline-text">' +
            escHtml(item.summary || item.description || '') + '</span>' +
          '</div>';
      });
    }
    return html + '</div></details></div>';
  }

  /** Wire the change-request detail session drill-down: a session with a
   *  resolvable internal id opens the existing Agent Run detail experience
   *  (closing the change-request overlay first, mirroring
   *  openAfkSessionDrilldown). */
  function wireCrDetailSessionDrilldown() {
    if (!els.crListDetailBody) return;
    var links = els.crListDetailBody.querySelectorAll('.afk-session-clickable');
    links.forEach(function (item) {
      item.addEventListener('click', function () {
        var sid = item.getAttribute('data-session-id');
        if (sid) openCrSessionDrilldown(sid);
      });
      item.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          var sid = item.getAttribute('data-session-id');
          if (sid) openCrSessionDrilldown(sid);
        }
      });
    });
  }

  /** Close the change-request detail overlay, then open the Agent Run
   *  detail overlay for the given internal session id. */
  function openCrSessionDrilldown(sessionId) {
    if (els.crListDetailOverlay) els.crListDetailOverlay.classList.remove('visible');
    openAgentRunDetail(sessionId);
  }

  // Build a root-to-children tree from the flat session links. Child arrays
  // intentionally retain the session-object contract used by the API/tests.
  function buildSessionTree(sessions) {
    if (!sessions || !sessions.length) return [];
    var byExtId = {};
    var roots = [];
    sessions.forEach(function (session) {
      var extId = session.external_session_id || session.session_id || '';
      byExtId[extId] = Object.assign({}, session, { children: [] });
    });
    sessions.forEach(function (session) {
      var extId = session.external_session_id || session.session_id || '';
      var parentId = session.parent_session_id || '';
      if (parentId && byExtId[parentId]) {
        byExtId[parentId].children.push(byExtId[extId]);
      } else if (parentId) {
        roots.push({ session: byExtId[extId], children: byExtId[extId].children, missing_parent: parentId });
      } else {
        roots.push({ session: byExtId[extId], children: byExtId[extId].children });
      }
    });
    return roots;
  }

  function renderNestedSessionNode(node, depth) {
    depth = depth || 0;
    var html = renderAfkSessionLink(node.session);
    if (depth > 0) {
      html = html.replace('afk-chain-item',
        'afk-chain-item afk-session-nested afk-nesting-depth-' + Math.min(depth, 5));
    } else {
      html = html.replace('afk-chain-item', 'afk-chain-item afk-session-root');
    }
    if (node.missing_parent) {
      html = html.replace('afk-chain-item', 'afk-chain-item afk-missing-parent');
      html += '<div class="afk-missing-parent-note">\u26A0 parent not in this run: ' +
        escHtml(node.missing_parent) + '</div>';
    }
    if (node.children && node.children.length) {
      html += '<div class="afk-session-children">';
      node.children.forEach(function (child) {
        html += renderNestedSessionNode({ session: child, children: child.children || [] }, depth + 1);
      });
      html += '</div>';
    }
    return html;
  }

  /** Render one chain step (from buildAfkChain) into HTML. */
  function renderAfkChainStep(step) {
    var body = '';
    if (step.key === 'run') {
      body = renderAfkRunStep(step.run);
    } else if (step.key === 'sessions') {
      var sessions = step.items || [];
      if (sessions.length) {
        var tree = buildSessionTree(sessions);
        body = tree.map(function (node) { return renderNestedSessionNode(node, 0); }).join('');
      } else {
        body = '<div class="afk-empty">No sessions linked</div>';
      }
    } else if (step.key === 'agents') {
      var agents = step.items || [];
      body = agents.length
        ? '<div class="afk-agents">' + agents.map(function (a) {
            return '<span class="afk-agent-chip">' + escHtml(a) + '</span>';
          }).join('') + '</div>'
        : '<div class="afk-empty">No agents recorded</div>';
    } else if (step.key === 'usage') {
      body = renderAfkUsageStep(step.usage);
    } else if (step.key === 'outcome') {
      body = renderAfkOutcomeStep(step.outcome, step.mergeEvents);
    } else {
      var items = step.items || [];
      body = items.length
        ? items.map(renderAfkEntityLink).join('')
        : '<div class="afk-empty">None</div>';
    }

    return '<div class="afk-chain-step" data-step="' + escHtml(step.key) + '">' +
      '<div class="afk-chain-step-label">' + escHtml(step.label) + '</div>' +
      '<div class="afk-chain-step-body">' + body + '</div>' +
      '</div>';
  }

  /** Render one entity link (issue/change_request/review/commit/merge_event)
   *  with its role, provisional marker, and correlation provenance. */
  function renderAfkEntityLink(link) {
    var provisional = isProvisionalLink(link);
    var roleCls = link.role === 'resolved' ? 'badge-completed'
      : link.role === 'noise' ? 'badge-unknown'
      : 'badge-stale'; // referenced
    var html = '<div class="afk-chain-item' + (provisional ? ' afk-provisional' : '') + '">' +
      '<div class="afk-chain-item-head">' +
        '<span class="afk-entity-id">' + escHtml(link.entity_id || '--') + '</span>' +
        '<span class="afk-entity-type">' + escHtml(link.entity_type || '') + '</span>' +
        (link.repository ? '<span class="afk-entity-repository">' + escHtml(link.repository) + '</span>' : '') +
        badge(link.role || '--', roleCls).outerHTML +
        (provisional ? ' <span class="afk-provisional-mark">\u26A0 provisional</span>' : '') +
      '</div>' +
      '<div class="afk-provenance">' + renderAfkProvenance(link) + '</div>';
    var evidence = renderAfkEvidence(link);
    if (evidence) {
      html += '<div class="afk-evidence">evidence: ' + evidence + '</div>';
    }
    html += '</div>';
    return html;
  }

  /** Render one session attachment with its inferred marker and compact Token
   *  Breakdown (delegating to fmtTokenBreakdownCompact per CONTEXT.md).
   *  Session attachments are always inferred — the marker makes that visible.
   *  A session with a resolvable internal session_id is clickable and opens the
   *  Agent Run detail overlay; one without stays non-clickable (issue #473). */
  function renderAfkSessionLink(session) {
    var agent = session.agent || '--';
    var extId = session.external_session_id || session.session_id || '--';
    var inferred = isProvisionalLink(session);
    var drillId = resolveAfkSessionDrilldown(session);
    var clickable = drillId ? ' afk-session-clickable' : '';
    var dataAttr = drillId ? ' data-session-id="' + escHtml(drillId) + '" tabindex="0"' : '';
    return '<div class="afk-chain-item' + (inferred ? ' afk-provisional' : '') + clickable + '"' + dataAttr + '>' +
      '<div class="afk-chain-item-head">' +
        '<span class="afk-entity-id">' + escHtml(extId) + '</span>' +
        '<span class="afk-entity-type">session</span>' +
        (inferred ? ' <span class="afk-provisional-mark">\u26A0 inferred</span>' : '') +
        (drillId ? ' <span class="afk-session-open">\u2197 open run</span>' : '') +
      '</div>' +
      '<div class="afk-session-meta">agent: ' + escHtml(agent) +
        ' &middot; messages: ' + fmtNum(session.message_count) +
        ' &middot; cost: ' + fmtCost(session.total_estimated_cost_usd) + '</div>' +
      '<div class="afk-tokens">' +
        fmtTokenBreakdownCompact(session.total_input_tokens, session.total_output_tokens,
          session.total_cache_read_tokens, session.total_cache_write_tokens) +
      '</div>' +
      '</div>';
  }

  /** Render the Run step: run status, provider, title, timestamps. */
  function renderAfkRunStep(run) {
    if (!run) return '<div class="afk-empty">No run data</div>';
    return '<div class="afk-chain-item">' +
      '<div class="afk-chain-item-head">' +
        '<span class="afk-entity-id">' + escHtml(shortUUID(run.afk_run_id)) + '</span>' +
        '<span class="afk-entity-type">' + escHtml(run.provider || '') + '</span>' +
        badge(run.status || '--', afkRunStatusBadgeClass(run.status)).outerHTML +
      '</div>' +
      '<div class="afk-run-meta">' + escHtml(run.title || run.afk_run_id || '--') +
        (run.started_at ? ' &middot; started ' + fmtDT(run.started_at) : '') +
        (run.finished_at ? ' &middot; finished ' + fmtDT(run.finished_at) : '') +
      '</div>' +
      '</div>';
  }

  /** Render the Tokens/Cost step from the run UsageAggregate.  Active Tokens
   *  = input + output (cache read/write are siblings, never folded in) and the
   *  cost renders via fmtCost — per the CONTEXT.md token vocabulary. */
  function renderAfkUsageStep(usage) {
    if (!usage) return '<div class="afk-empty">No usage aggregate</div>';
    return '<div class="afk-chain-item">' +
      '<div class="afk-tokens">' +
        fmtTokenBreakdownCompact(usage.input_tokens, usage.output_tokens,
          usage.cache_read_tokens, usage.cache_write_tokens) +
      '</div>' +
      '<div class="afk-usage-meta">Active Tokens: ' + fmtNum(usage.active_tokens) +
        ' &middot; Est. Cost: ' + fmtCost(usage.estimated_cost_usd) +
        ' &middot; ' + fmtNum(usage.session_count) + ' sessions &middot; ' +
        fmtNum(usage.message_count) + ' messages</div>' +
      '</div>';
  }

  /** Render the Outcome step: the terminal EngineeringOutcomeStatus badge plus
   *  the resolved change_request/issue ids and the merge event (merged_at). */
  function renderAfkOutcomeStep(outcome, mergeEvents) {
    var statusHtml = outcome
      ? badge(outcomeStatusLabel(outcome.status), outcomeStatusBadgeClass(outcome.status)).outerHTML
      : badge('unknown', 'badge-unknown').outerHTML;
    var html = '<div class="afk-chain-item">' +
      '<div class="afk-chain-item-head">' +
        '<span class="afk-entity-id">outcome</span>' + statusHtml +
      '</div>';
    if (outcome) {
      var parts = [];
      if (outcome.change_request_ids && outcome.change_request_ids.length) {
        parts.push('change request: ' + escHtml(outcome.change_request_ids.join(', ')));
      }
      if (outcome.resolved_issue_ids && outcome.resolved_issue_ids.length) {
        parts.push('resolved issues: ' + escHtml(outcome.resolved_issue_ids.join(', ')));
      }
      if (outcome.merged_at) {
        parts.push('merged ' + fmtDT(outcome.merged_at));
      }
      html += '<div class="afk-outcome-meta">' + (parts.length ? parts.join(' &middot; ') : 'no details') + '</div>';
    } else {
      html += '<div class="afk-outcome-meta">No terminal outcome recorded</div>';
    }
    if (mergeEvents && mergeEvents.length) {
      html += mergeEvents.map(renderAfkEntityLink).join('');
    }
    html += '</div>';
    return html;
  }

  /** Open the Change Request Provenance Timeline overlay (issue #574).
   *  Uses deterministic fixtures for now; will be backed by the Gateway
   *  composite read contract when the API is ready.  Shows loading, error,
   *  and empty states. */
  async function openChangeRequestProvenance(changeRequestId) {
    els.crProvOverlay.classList.add('visible');
    els.crProvBody.innerHTML = '<p class="empty-state">Loading provenance timeline&hellip;</p>';
    els.crProvTitle.textContent = 'Change Request Provenance';

    // Deterministic fixtures: select by change request id prefix
    var fixture;
    if (changeRequestId && changeRequestId.indexOf('cloudnative-pg') !== -1) {
      fixture = gitlabIncompleteFixture();
    } else if (changeRequestId && changeRequestId.indexOf('data-pipeline') !== -1) {
      fixture = repeatedReviewFixture();
    } else {
      fixture = githubCompleteFixture();
    }

    els.crProvTitle.textContent = escHtml(
      (fixture.change_request.repository || '') + '#' + (fixture.change_request.external_id || '')
    );
    els.crProvBody.innerHTML = renderProvenanceTimeline(fixture, 'ok');
  }

  /** Render an entity link's provenance line (method · confidence · resolver). */
  function renderAfkProvenance(link) {
    var parts = [];
    if (link && link.correlation_method) parts.push(escHtml(link.correlation_method));
    parts.push(fmtConfidence(link && link.correlation_confidence));
    if (link && link.resolver_version) parts.push('resolver v' + escHtml(link.resolver_version));
    return parts.join(' \u00B7 ');
  }

  /** Render an entity link's evidence list as a compact, escaped string. */
  function renderAfkEvidence(link) {
    var evidence = (link && link.evidence) || [];
    if (!evidence.length) return '';
    return escHtml(fmtEvidence(evidence));
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
          '<span>' + escHtml(t.content) + priorityMark + '</span>' +
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
        fieldHtml('Code Changes', fmtCodeChangesDiff(d.code_change_additions, d.code_change_deletions)) +
      '</div></div>';

    // ── Token Breakdown (issue #557): read/write/reasoning + cache hit
    //    ratio + primary provider, with missing-data semantics. ──
    html += fmtTokenBreakdownSection(d);

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
    var errors = Object.assign({}, fetchErrors, { agentRuns: agentRunsFetchError, afkRuns: afkRunsFetchError, afkChangeRequests: afkCrFetchError });
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
      renderAgentUsageTable(data); // Agent Usage panel (issue #438)
      // Issue #429: when the fetched total no longer covers the current
      // page (the result set shrank), correct the page state + URL and
      // re-fetch the nearest valid page through the shared agent-runs
      // path.  The stale-offset response is NOT rendered — the previously
      // displayed rows stay visible while the corrected page loads.
      if (applyAgentRunPageFallback(data.agentRuns)) {
        fetchAgentRunsAndRender();
      } else {
        renderAgentRunsTable(data.agentRuns);
        renderAgentRunPagination(data.agentRuns); // pagination control below the panel (issue #427)
      }
      renderClientProjectBreakdown(data);
      renderChangeRequestSummaryTable(data.afkChangeRequests); // Change Request list (issue #613) — primary view
      renderChangeRequestPagination(data.afkChangeRequests); // pagination control below the panel
      renderAfkOutcomesTable(data.afkRuns); // AFK Outcomes view (issue #453) — secondary run-centric view
      renderRepositorySummaryTable(data.afkRuns);
      renderChangeRequestList(data.afkRuns);
      renderUnresolvedRelationshipsPanel(data.afkRuns); // Unresolved relationships (issue #576)
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

  /** Apply the current filter-bar values and re-fetch the Agent Runs list
   *  (issue #428): applying or changing filters re-scopes the view to page 1,
   *  REPLACING the URL state (history.replaceState) so filter adjustments do
   *  not add a browser-history entry per change.  The filter values ride
   *  along in the request (buildAgentRunsUrl) and in the existing query state
   *  kept by agentRunsUrlWithPagination.  Explicit page navigation
   *  (setAgentRunPage, issue #426) keeps using pushState. */
  function applyFilters() {
    agentRunFilters = readFiltersFromUI();
    agentRunPage = 1;
    if (typeof history !== 'undefined' && typeof history.replaceState === 'function') {
      history.replaceState({}, '', agentRunsUrlWithPagination(agentRunPage, agentRunPageSize));
    }
    return fetchAgentRunsAndRender();
  }

  /** Fetch the Agent Runs page described by the current filter + pagination
   *  state and re-render the table and pagination controls (issues #427 and
   *  #428).  Shared by the Apply/Clear filter path, the pagination control
   *  clicks, and the page-size change path, so paging and page-size changes
   *  always preserve the active filters: buildAgentRunsUrl carries
   *  from_date/to_date/agent/status alongside the page-derived limit/offset
   *  (issue #426). */
  function fetchAgentRunsAndRender() {
    // Track the agent-runs panel freshness for this independent fetch
    var prev = panelStates['agent-runs'];
    setPanelState('agent-runs', 'refreshing', prev ? prev.updatedAt : null);
    // Re-fetch agent runs with current filters + page state, update table
    var url = buildAgentRunsUrl();
    return apiFetch(url).then(function (data) {
      agentRunsData = data;
      agentRunsFetchError = null;
      setPanelState('agent-runs', 'ok', Date.now());
      // Issue #429: when the fetched total no longer covers the current
      // page (the result set shrank), correct the page state + URL and
      // re-fetch the nearest valid page through this same path.  Nothing
      // renders until the corrected page resolves, so the previously
      // displayed rows stay visible while the new page loads.
      if (applyAgentRunPageFallback(data)) {
        return fetchAgentRunsAndRender();
      }
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
        // PR #431 review (finding 3): clicking the already-current page is a
        // no-op — no duplicate history entry, no redundant refetch.  The
        // current page stays focusable (aria-current="page" + "current page"
        // label unchanged).
        if (!Number.isInteger(page) || page < 1 || page === agentRunPage) return;
        setAgentRunPage(page);
        return fetchAgentRunsAndRender();
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
    return applyFilters();
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

    // Page-size selector (issue #428): changing the rows-per-page choice
    // re-scopes the list to page 1 at the new size — setAgentRunPageSize
    // updates the closure state and REPLACES the URL state, then the
    // shared filter path re-fetches immediately so the table reflects the
    // new limit without waiting for the next auto-refresh.  The current
    // filter values ride along unchanged.
    if (els.arPageSize) {
      els.arPageSize.addEventListener('change', function () {
        setAgentRunPageSize(els.arPageSize.value);
        fetchAgentRunsAndRender();
      });
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

  /** Wire the Change Request Provenance Timeline overlay DOM events (issue #574):
   *  close button, backdrop click, and ESC key. */
  function setupCrProvEventHandlers() {
    if (els.crProvClose) {
      els.crProvClose.addEventListener('click', function () {
        els.crProvOverlay.classList.remove('visible');
      });
    }
    if (els.crProvOverlay) {
      els.crProvOverlay.addEventListener('click', function (e) {
        if (e.target === els.crProvOverlay) {
          els.crProvOverlay.classList.remove('visible');
        }
      });
    }
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' &&
          els.crProvOverlay && els.crProvOverlay.classList.contains('visible')) {
        els.crProvOverlay.classList.remove('visible');
      }
    });
  }

  /** Wire the AFK Outcomes detail-overlay DOM events (issue #453): the close
   *  button, backdrop click, and ESC key — mirroring the agent-runs overlay
   *  wiring (setupAgentRunEventHandlers). */
  function setupAfkOutcomesEventHandlers() {
    if (els.afkDetailClose) {
      els.afkDetailClose.addEventListener('click', function () {
        els.afkDetailOverlay.classList.remove('visible');
      });
    }
    if (els.afkDetailOverlay) {
      els.afkDetailOverlay.addEventListener('click', function (e) {
        if (e.target === els.afkDetailOverlay) {
          els.afkDetailOverlay.classList.remove('visible');
        }
      });
    }
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' &&
          els.afkDetailOverlay && els.afkDetailOverlay.classList.contains('visible')) {
        els.afkDetailOverlay.classList.remove('visible');
      }
    });

    // Change-request list panel (issue #573): back button + AFK-only toggle
    var backBtn = $('afk-cr-back');
    if (backBtn) {
      backBtn.addEventListener('click', function () {
        clearSelectedRepo();
      });
    }
    var toggleEl = $('afk-cr-toggle');
    if (toggleEl) {
      toggleEl.addEventListener('change', function () {
        afkOnlyFilter = toggleEl.checked;
        renderChangeRequestList(afkRunsData);
      });
    }

    // Change Request list (issue #613): filter bar (selects apply on change,
    // repository text input on Enter, plus explicit Apply/Clear buttons) and
    // the identity-keyed detail overlay (close button, backdrop, ESC) —
    // mirroring the existing overlay wiring.
    var crFilterApply = $('afk-cr-filter-apply');
    if (crFilterApply) {
      crFilterApply.addEventListener('click', function () { applyChangeRequestFilters(); });
    }
    var crFilterClear = $('afk-cr-filter-clear');
    if (crFilterClear) {
      crFilterClear.addEventListener('click', function () { clearChangeRequestFilters(); });
    }
    var crFilterProvider = $('afk-cr-filter-provider');
    if (crFilterProvider) {
      crFilterProvider.addEventListener('change', function () { applyChangeRequestFilters(); });
    }
    var crFilterProviderState = $('afk-cr-filter-provider-state');
    if (crFilterProviderState) {
      crFilterProviderState.addEventListener('change', function () { applyChangeRequestFilters(); });
    }
    var crFilterAutomationState = $('afk-cr-filter-automation-state');
    if (crFilterAutomationState) {
      crFilterAutomationState.addEventListener('change', function () { applyChangeRequestFilters(); });
    }
    var crFilterRepository = $('afk-cr-filter-repository');
    if (crFilterRepository) {
      crFilterRepository.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') applyChangeRequestFilters();
      });
    }
    if (els.crListDetailClose) {
      els.crListDetailClose.addEventListener('click', function () {
        els.crListDetailOverlay.classList.remove('visible');
      });
    }
    if (els.crListDetailOverlay) {
      els.crListDetailOverlay.addEventListener('click', function (e) {
        if (e.target === els.crListDetailOverlay) {
          els.crListDetailOverlay.classList.remove('visible');
        }
      });
    }
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' &&
          els.crListDetailOverlay && els.crListDetailOverlay.classList.contains('visible')) {
        els.crListDetailOverlay.classList.remove('visible');
      }
    });
  }

  // ── Transcript view (issue #469) ───────────────────────────────────────
  // Renders a session's execution transcript: header, messages, parts, and
  // unified timeline with depth-annotated parent/child distinction.  Uses
  // keyset cursor pagination (next_cursor) for all list endpoints.

  /** Validate a session UUID string (simple check: non-empty, matches UUID
   *  format).  Pure -- no DOM or fetch access. */
  function isValidSessionId(raw) {
    if (!raw || typeof raw !== 'string') return false;
    var trimmed = raw.trim();
    if (trimmed.length === 0) return false;
    return /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(trimmed);
  }

  /** Format a Transcript Timeline depth as a human-readable label.
   *  depth=0 -> 'Root session', depth=1 -> 'Subagent (L1)', etc.
   *  Pure -- no DOM access. */
  function fmtTranscriptDepth(depth) {
    if (depth == null || depth === 0) return 'Root session';
    return 'Subagent (L' + depth + ')';
  }

  /** Return a CSS class for depth-based visual distinction.
   *  depth=0 -> 'tr-depth-root', depth=1 -> 'tr-depth-1', etc.
   *  Pure -- no DOM access. */
  function depthClass(depth) {
    var d = (depth != null) ? depth : 0;
    if (d === 0) return 'tr-depth-root';
    if (d <= 5) return 'tr-depth-' + d;
    return 'tr-depth-deep';
  }

  /** Return a CSS class for a transcript event type.
   *  Pure -- no DOM access. */
  function transcriptPartTypeClass(partType) {
    if (partType === 'tool') return 'tr-part-tool';
    if (partType === 'text') return 'tr-part-text';
    if (partType === 'reasoning') return 'tr-part-reasoning';
    if (partType === 'step-start') return 'tr-part-step-start';
    if (partType === 'step-finish') return 'tr-part-step-finish';
    return 'tr-part-unknown';
  }

  /** Render the transcript session header card.
   *  Pure -- returns HTML string. */
  function renderTranscriptHeader(header) {
    if (!header) return '';
    var html = '<div class="tr-header-card">';
    html += '<div class="tr-header-grid">';
    html += fieldHtml('Session', shortUUID(header.id));
    html += fieldHtml('External ID', escHtml(header.external_session_id || '--'));
    html += fieldHtml('Agent', escHtml(header.agent || '--'));
    html += fieldHtml('Messages', fmtNum(header.message_count));
    html += fieldHtml('Parts', fmtNum(header.part_count));
    html += fieldHtml('Tool Calls', fmtNum(header.tool_call_count));
    if (header.first_part_at && header.last_part_at) {
      html += fieldHtml('Duration', fmtDuration(header.first_part_at, header.last_part_at));
    }
    html += fieldHtml('Parent', header.parent_session_id
      ? escHtml(header.parent_session_id)
      : '<span style="color:var(--text-muted)">None (root)</span>');
    if (header.child_session_ids && header.child_session_ids.length > 0) {
      html += fieldHtml('Children', fmtNum(header.child_session_ids.length));
    }
    html += '</div></div>';
    return html;
  }

  /** Render one timeline event row.  Pure -- returns HTML string. */
  function renderTimelineEvent(event) {
    var depth = event.depth || 0;
    var cls = depthClass(depth) + ' ' + transcriptPartTypeClass(event.part_type);
    var depthLabel = fmtTranscriptDepth(depth);
    var agent = event.agent || '';
    var time = fmtDT(event.source_created_at_tz || event.source_created_at);
    var data = event.data || {};

    var html = '<div class="tr-event ' + cls + '">';
    html += '<div class="tr-event-head">';
    html += '<span class="tr-event-depth">' + escHtml(depthLabel) + '</span>';
    if (agent) html += ' <span class="tr-event-agent">' + escHtml(agent) + '</span>';
    html += ' <span class="tr-event-type">' + escHtml(event.part_type || 'unknown') + '</span>';
    html += ' <span class="tr-event-time">' + time + '</span>';
    html += '</div>';

    if (event.part_type === 'tool') {
      var toolName = data.tool || data.name || '';
      var toolStatus = data.status || '';
      var toolInput = data.input != null ? data.input : data.tool_input;
      var toolOutput = data.output != null ? data.output : data.tool_output;
      html += '<div class="tr-event-body">';
      if (toolName) html += '<span class="tr-tool-name">' + escHtml(toolName) + '</span>';
      if (toolStatus) html += ' ' + badge(toolStatus, 'badge-' + (toolStatus === 'completed' ? 'completed' : toolStatus === 'error' ? 'failed' : 'unknown')).outerHTML;
      if (toolInput != null) {
        var inputStr = typeof toolInput === 'string' ? toolInput : JSON.stringify(toolInput, null, 2);
        html += '<pre class="tr-tool-io">' + escHtml(inputStr) + '</pre>';
      }
      if (toolOutput != null) {
        var outputStr = typeof toolOutput === 'string' ? toolOutput : JSON.stringify(toolOutput, null, 2);
        html += '<pre class="tr-tool-io">' + escHtml(outputStr) + '</pre>';
      }
      html += '</div>';
    } else if (event.part_type === 'text' || event.part_type === 'reasoning') {
      var text = data.text || data.content || '';
      if (text) {
        html += '<div class="tr-event-body tr-text-content">' + escHtml(text) + '</div>';
      }
    } else {
      var summary = data.text || data.content || '';
      if (summary) {
        html += '<div class="tr-event-body tr-text-content">' + escHtml(summary) + '</div>';
      }
    }
    html += '</div>';
    return html;
  }

  /** Render one message row.  Pure -- returns HTML string. */
  function renderTranscriptMessage(msg) {
    var role = msg.role || 'unknown';
    var agent = msg.agent || '';
    var time = fmtDT(msg.source_created_at_tz || msg.source_created_at);
    var tokens = (msg.input_tokens || 0) + (msg.output_tokens || 0);

    var html = '<div class="tr-msg tr-msg-' + escHtml(role) + '">';
    html += '<div class="tr-msg-head">';
    html += '<span class="tr-msg-role">' + badge(role, 'badge-' + (role === 'assistant' ? 'completed' : role === 'user' ? 'running' : 'unknown')).outerHTML + '</span>';
    if (agent) html += ' <span class="tr-msg-agent">' + escHtml(agent) + '</span>';
    if (msg.mode) html += ' <span class="tr-msg-mode">' + escHtml(msg.mode) + '</span>';
    html += ' <span class="tr-msg-time">' + time + '</span>';
    if (tokens > 0) {
      html += ' <span class="tr-msg-tokens">' + fmtNum(tokens) + ' tokens</span>';
    }
    html += '</div>';

    var msgData = msg.data || {};
    var text = msgData.text || msgData.content || '';
    if (text) {
      html += '<div class="tr-msg-body tr-text-content">' + escHtml(text) + '</div>';
    }
    html += '</div>';
    return html;
  }

  /** Render one part row.  Pure -- returns HTML string. */
  function renderTranscriptPart(part) {
    var cls = transcriptPartTypeClass(part.part_type);
    var time = fmtDT(part.source_created_at_tz || part.source_created_at);
    var partData = part.data || {};

    var html = '<div class="tr-part ' + cls + '">';
    html += '<div class="tr-part-head">';
    html += '<span class="tr-part-type">' + badge(part.part_type || 'unknown', 'badge-' + (part.part_type === 'tool' ? 'completed' : part.part_type === 'text' ? 'running' : 'unknown')).outerHTML + '</span>';
    html += ' <span class="tr-part-time">' + time + '</span>';
    html += '</div>';

    if (part.part_type === 'tool') {
      var toolName = partData.tool || partData.name || '';
      var toolStatus = partData.status || '';
      var toolInput = partData.input != null ? partData.input : partData.tool_input;
      var toolOutput = partData.output != null ? partData.output : partData.tool_output;
      html += '<div class="tr-part-body">';
      if (toolName) html += '<span class="tr-tool-name">' + escHtml(toolName) + '</span>';
      if (toolStatus) html += ' ' + badge(toolStatus, 'badge-' + (toolStatus === 'completed' ? 'completed' : toolStatus === 'error' ? 'failed' : 'unknown')).outerHTML;
      if (toolInput != null) {
        var inputStr = typeof toolInput === 'string' ? toolInput : JSON.stringify(toolInput, null, 2);
        html += '<pre class="tr-tool-io">' + escHtml(inputStr) + '</pre>';
      }
      if (toolOutput != null) {
        var outputStr = typeof toolOutput === 'string' ? toolOutput : JSON.stringify(toolOutput, null, 2);
        html += '<pre class="tr-tool-io">' + escHtml(outputStr) + '</pre>';
      }
      html += '</div>';
    } else {
      var text = partData.text || partData.content || '';
      if (text) {
        html += '<div class="tr-part-body tr-text-content">' + escHtml(text) + '</div>';
      }
    }
    html += '</div>';
    return html;
  }

  /** Render a list of events/parts/messages into a container. */
  function renderTranscriptList(items, renderer) {
    if (!items || items.length === 0) {
      return '<p class="empty-state">No items to display</p>';
    }
    return items.map(renderer).join('');
  }

  /** Switch the active transcript sub-view (timeline/messages/parts). */
  function switchTranscriptView(viewName) {
    trActiveView = viewName;
    trNextCursor = null;
    trHasMore = false;
    trItems = [];
    var btns = els.trViewToggle ? els.trViewToggle.querySelectorAll('.tr-view-btn') : [];
    btns.forEach(function (btn) {
      btn.classList.toggle('active', btn.getAttribute('data-view') === viewName);
    });
    if (els.trTimelineWrap) els.trTimelineWrap.style.display = (viewName === 'timeline') ? '' : 'none';
    if (els.trMessagesWrap) els.trMessagesWrap.style.display = (viewName === 'messages') ? '' : 'none';
    if (els.trPartsWrap)    els.trPartsWrap.style.display    = (viewName === 'parts')    ? '' : 'none';
    if (trSessionId) fetchTranscriptPage();
  }

  /** Update the "next page" button and status text. */
  function updateTranscriptPagination() {
    if (els.trNextPageBtn) {
      els.trNextPageBtn.disabled = !trHasMore;
      els.trNextPageBtn.textContent = trHasMore ? 'Load more \u2192' : 'End of stream';
    }
    if (els.trStatus) {
      var count = trItems.length;
      var cursorInfo = trNextCursor ? ' (paginated)' : '';
      els.trStatus.textContent = count + ' items loaded' + cursorInfo;
    }
  }

  /** Fetch a page of transcript data for the active view. */
  async function fetchTranscriptPage() {
    if (!trSessionId) return;
    var baseUrl = '/api/v1/execution/sessions/' + encodeURIComponent(trSessionId);
    var url;
    switch (trActiveView) {
      case 'timeline': url = baseUrl + '/timeline'; break;
      case 'messages': url = baseUrl + '/messages'; break;
      case 'parts':    url = baseUrl + '/parts';    break;
      default: return;
    }
    var params = ['limit=100'];
    if (trNextCursor) params.push('after=' + encodeURIComponent(trNextCursor));
    url += '?' + params.join('&');

    if (els.trStatus) els.trStatus.textContent = 'Loading\u2026';
    if (els.trNextPageBtn) els.trNextPageBtn.disabled = true;

    try {
      var data = await apiFetch(url);
      var items = data.items || [];
      var renderer;
      switch (trActiveView) {
        case 'timeline': renderer = renderTimelineEvent; break;
        case 'messages': renderer = renderTranscriptMessage; break;
        case 'parts':    renderer = renderTranscriptPart;    break;
        default: renderer = renderTimelineEvent;
      }
      trItems = trItems.concat(items);
      trNextCursor = data.next_cursor || null;
      trHasMore = !!data.has_more;

      var container;
      switch (trActiveView) {
        case 'timeline': container = els.trTimelineWrap; break;
        case 'messages': container = els.trMessagesWrap; break;
        case 'parts':    container = els.trPartsWrap;    break;
        default: container = els.trTimelineWrap;
      }
      if (container) {
        container.innerHTML = renderTranscriptList(trItems, renderer);
      }
      updateTranscriptPagination();
    } catch (e) {
      console.error('Transcript fetch error:', e);
      if (els.trStatus) els.trStatus.textContent = 'Error: ' + e.message;
    }
  }

  /** Load a transcript session: fetch header and first page of timeline. */
  async function loadTranscriptSession() {
    var raw = els.trSessionInput ? els.trSessionInput.value.trim() : '';
    if (!isValidSessionId(raw)) {
      if (els.trStatus) els.trStatus.textContent = 'Please enter a valid session UUID';
      return;
    }
    trSessionId = raw;
    trNextCursor = null;
    trHasMore = false;
    trItems = [];
    trActiveView = 'timeline';

    var btns = els.trViewToggle ? els.trViewToggle.querySelectorAll('.tr-view-btn') : [];
    btns.forEach(function (btn) {
      btn.classList.toggle('active', btn.getAttribute('data-view') === 'timeline');
    });
    if (els.trTimelineWrap) els.trTimelineWrap.style.display = '';
    if (els.trMessagesWrap) els.trMessagesWrap.style.display = 'none';
    if (els.trPartsWrap)    els.trPartsWrap.style.display    = 'none';

    if (els.trSessionHeader) {
      els.trSessionHeader.innerHTML = '<p class="empty-state">Loading session header\u2026</p>';
    }
    if (els.trStatus) els.trStatus.textContent = 'Loading session\u2026';

    try {
      var header = await apiFetch('/api/v1/execution/sessions/' + encodeURIComponent(trSessionId));
      if (els.trSessionHeader) {
        els.trSessionHeader.innerHTML = renderTranscriptHeader(header);
      }
      await fetchTranscriptPage();
    } catch (e) {
      var notFound = /404/.test(e && e.message);
      if (els.trSessionHeader) {
        els.trSessionHeader.innerHTML = '<p class="empty-state">' +
          (notFound ? 'Session not found' : 'Failed to load session: ' + escHtml(e.message)) +
          '</p>';
      }
      if (els.trStatus) els.trStatus.textContent = notFound ? 'Session not found' : 'Error: ' + e.message;
    }
  }

  /** Wire transcript view DOM events. */
  function setupTranscriptEventHandlers() {
    if (els.trLoadBtn) {
      els.trLoadBtn.addEventListener('click', loadTranscriptSession);
    }
    if (els.trSessionInput) {
      els.trSessionInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') loadTranscriptSession();
      });
    }
    if (els.trViewToggle) {
      els.trViewToggle.querySelectorAll('.tr-view-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
          var view = btn.getAttribute('data-view');
          if (view) switchTranscriptView(view);
        });
      });
    }
    if (els.trNextPageBtn) {
      els.trNextPageBtn.addEventListener('click', fetchTranscriptPage);
    }
  }
  // ── End transcript view (issue #469) ──────────────────────────────────


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
    setupAfkOutcomesEventHandlers();
    setupCrProvEventHandlers();
    setupTranscriptEventHandlers();
    setupTabNavigation();
    setupDateRangeHandlers();
    // Issue #426: read ?page / ?page_size from the URL before the initial
    // fetch so a deep link such as ?page=2&page_size=100 loads the
    // corresponding Agent Runs page on dashboard load.
    readAgentRunPaginationFromUrl();
    // Change Request list: read ?limit / ?offset from the URL before the
    // initial fetch so a deep link such as ?limit=100&offset=100 loads the
    // corresponding change-request page on dashboard load.
    readChangeRequestPaginationFromUrl();
    // PR #431 review (finding 4): keep Back/Forward navigation in sync with
    // the in-memory page state.  Guarded so non-browser environments (the
    // Node sandbox, which never calls startAutoRefresh) can't crash on a
    // missing window.addEventListener.
    if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
      window.addEventListener('popstate', handleAgentRunPopstate);
      window.addEventListener('popstate', handleChangeRequestPopstate);
    }
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
  // AFK Outcomes (issue #613/#614): the change-request detail overlay wiring
  // (close button, backdrop, ESC) as a test seam — mirrors the
  // setupAgentRunEventHandlers export used by the Node harness.
  window.setupAfkOutcomesEventHandlers = setupAfkOutcomesEventHandlers;
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
  // Agent Runs page-size validation + reset semantics (issue #428): the
  // size validator (25/50/100, fallback 50) and the size-change hook that
  // resets to page 1 and REPLACES the URL state (history.replaceState) —
  // filter applies reset through applyFilters, which shares the same
  // replace semantics instead of pushing a history entry per adjustment.
  window.parseAgentRunPageSize = parseAgentRunPageSize;
  window.setAgentRunPageSize = setAgentRunPageSize;
  // Agent Runs pagination resilience (issue #429): the pure nearest-valid-
  // page calculator (used when a fetched total no longer covers the current
  // page) and the shared agent-runs fetch path — the refresh-style refetch
  // that keeps the selected page/offset, preserves rows during loading and
  // failures, and re-fetches the corrected page after a fallback.
  window.nearestValidAgentRunPage = nearestValidAgentRunPage;
  window.fetchAgentRunsAndRender = fetchAgentRunsAndRender;
  // PR #431 review (finding 4): the Back/Forward (popstate) re-sync handler
  // joins the window test seam so the Node harness can drive it directly.
  window.handleAgentRunPopstate = handleAgentRunPopstate;
  // Agent Usage panel (issues #438/#439): the pure row-derivation helper
  // and the render function — the Node harness exercises row derivation
  // (full Token Breakdown contract fields), full-total ordering, the
  // 'unknown' fallback, and rendering through the fake tbody.
  window.buildAgentUsageRows = buildAgentUsageRows;
  window.renderAgentUsageTable = renderAgentUsageTable;
  // AFK Outcomes view (issue #453): the locked-vocabulary outcome/run badge
  // mappings, confidence/evidence formatters, provisional-link predicate,
  // canonical chain composer, and the render functions — all pure string
  // builders except renderAfkOutcomesTable/renderAfkRunDetail (which write to
  // the fake tbody/detail-body elements) and openAfkRunDetail (fetch-driven).
  window.outcomeStatusBadgeClass = outcomeStatusBadgeClass;
  window.outcomeStatusLabel = outcomeStatusLabel;
  window.afkRunStatusBadgeClass = afkRunStatusBadgeClass;
  window.fmtConfidence = fmtConfidence;
  window.fmtEvidence = fmtEvidence;
  window.isProvisionalLink = isProvisionalLink;
  window.resolveAfkSessionDrilldown = resolveAfkSessionDrilldown;
  window.buildAfkChain = buildAfkChain;
  window.renderAfkEntityLink = renderAfkEntityLink;
  window.renderAfkSessionLink = renderAfkSessionLink;
  window.renderAfkChainStep = renderAfkChainStep;
  window.renderAfkRunDetail = renderAfkRunDetail;
  window.renderAfkOutcomesTable = renderAfkOutcomesTable;
  window.openAfkRunDetail = openAfkRunDetail;
  window.buildSessionTree = buildSessionTree;
  window.renderNestedSessionNode = renderNestedSessionNode;
  window.deriveRepositoryLabel = deriveRepositoryLabel;
  window.buildRepositorySummaries = buildRepositorySummaries;
  window.renderRepositorySummaryTable = renderRepositorySummaryTable;
  window.providerCrTerm = providerCrTerm;
  window.buildChangeRequestList = buildChangeRequestList;
  window.filterChangeRequests = filterChangeRequests;
  window.renderChangeRequestList = renderChangeRequestList;
  window.selectRepository = selectRepository;
  window.clearSelectedRepo = clearSelectedRepo;
  // Change Request list (issue #613): the URL builders, filter state hooks,
  // render functions, and the identity-keyed selection/detail flow — the
  // pure helpers (URL builders, row/detail renderers) plus the fetch-driven
  // paths (apply/clear filters, open detail) exercised by the Node harness.
  window.buildChangeRequestListUrl = buildChangeRequestListUrl;
  window.buildChangeRequestDetailPath = buildChangeRequestDetailPath;
  window.changeRequestKey = changeRequestKey;
  window.renderChangeRequestSummaryRow = renderChangeRequestSummaryRow;
  window.renderChangeRequestSummaryTable = renderChangeRequestSummaryTable;
  window.readChangeRequestFiltersFromUI = readChangeRequestFiltersFromUI;
  window.syncChangeRequestFilterUI = syncChangeRequestFilterUI;
  window.applyChangeRequestFilters = applyChangeRequestFilters;
  window.clearChangeRequestFilters = clearChangeRequestFilters;
  window.fetchChangeRequestsAndRender = fetchChangeRequestsAndRender;
  // Change Request list pagination (mirrors the Agent Runs pagination test
  // seam): the pure URL-param parser, the on-load URL reader, the page-set
  // history hook, the nearest-valid-page calculator, the page-fallback
  // hook, and the control renderer.
  window.parseChangeRequestPagination = parseChangeRequestPagination;
  window.readChangeRequestPaginationFromUrl = readChangeRequestPaginationFromUrl;
  window.setChangeRequestPage = setChangeRequestPage;
  window.changeRequestsUrlWithPagination = changeRequestsUrlWithPagination;
  window.nearestValidChangeRequestPage = nearestValidChangeRequestPage;
  window.applyChangeRequestPageFallback = applyChangeRequestPageFallback;
  window.renderChangeRequestPagination = renderChangeRequestPagination;
  window.handleChangeRequestPopstate = handleChangeRequestPopstate;
  window.openChangeRequestDetail = openChangeRequestDetail;
  window.renderChangeRequestDetail = renderChangeRequestDetail;
  window.renderChangeRequestExecution = renderChangeRequestExecution;
  window.setChangeRequestFilters = function (filters) { afkCrFilters = filters || {}; };
  window.setSelectedChangeRequest = function (cr) { selectedChangeRequest = cr; };
  window.getSelectedChangeRequest = function () { return selectedChangeRequest; };
  // Issue #576: relationship state presentation + unresolved-relationships view
  window.fmtRelationshipState = fmtRelationshipState;
  window.renderRelationshipBadge = renderRelationshipBadge;
  window.buildUnresolvedRelationships = buildUnresolvedRelationships;
  window.renderUnresolvedRelationshipsRow = renderUnresolvedRelationshipsRow;
  window.renderUnresolvedRelationships = renderUnresolvedRelationships;
  // Transcript view (issue #469): pure helpers for depth formatting, part-type
  // classification, and the header/timeline/message/part renderers.  The Node
  // test harness exercises these through the vm-sandbox window seam.
  window.isValidSessionId = isValidSessionId;
  window.fmtTranscriptDepth = fmtTranscriptDepth;
  window.depthClass = depthClass;
  window.transcriptPartTypeClass = transcriptPartTypeClass;
  window.renderTranscriptHeader = renderTranscriptHeader;
  window.renderTimelineEvent = renderTimelineEvent;
  window.renderTranscriptMessage = renderTranscriptMessage;
  window.renderTranscriptPart = renderTranscriptPart;
  window.renderTranscriptList = renderTranscriptList;
  // Read-only accessor for the last COMPLETED refresh cycle time — reusable
  // by follow-up work (issue #358) without reaching into module state.
  window.getLastRefreshedAt = function () { return lastRefreshedAt; };
  // Provider + token-breakdown helpers (issue #557): provider badge/missing
  // label, cache hit ratio, and the Token Breakdown detail-section builder —
  // pure string builders exercised by the Node harness.
  window.fmtProvider = fmtProvider;
  window.fmtCacheHitRatio = fmtCacheHitRatio;
  window.fmtTokenBreakdownSection = fmtTokenBreakdownSection;

})();
