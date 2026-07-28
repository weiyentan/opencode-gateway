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
  const SESSION_LIMIT = 20;
  const CLIENT_LIMIT = 100;
  /** Session is considered "active" if last_message_at is within this window.
   *  This is a heuristic — long-running but infrequent sessions may be
   *  incorrectly marked as "ended", and very recent sessions that have
   *  completed may briefly show as "active". */
  const SESSION_ACTIVE_WINDOW_MS = 3600000; // 1 hour
  const AGENT_RUN_LIMIT = 50;

  // ── Element refs ───────────────────────────────────────────────────────

  const $ = function (id) { return document.getElementById(id); };

  const els = {
    dashboard:      document.querySelector('.dashboard'),
    liveIndicator:  $('live-indicator'),
    timestamp:      $('timestamp'),
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
    sessionsTbody:  $('sessions-tbody'),

    // Agent Runs
    arTbody:        $('agent-runs-tbody'),
    arFilterFrom:   $('ar-filter-from'),
    arFilterTo:     $('ar-filter-to'),
    arFilterAgent:  $('ar-filter-agent'),
    arFilterStatus: $('ar-filter-status'),
    arFilterApply:  $('ar-filter-apply'),
    arDetailOverlay: $('ar-detail-overlay'),
    arDetailTitle:  $('ar-detail-title'),
    arDetailBody:   $('ar-detail-body'),
    arDetailClose:  $('ar-detail-close'),

    // Client/Project
    cpTbody:         $('cp-tbody'),
    cpPanelSubtitle: $('cp-panel-subtitle'),

    // Session detail
    sdDetailOverlay: $('sd-detail-overlay'),
    sdDetailTitle:   $('sd-detail-title'),
    sdDetailBody:    $('sd-detail-body'),
    sdDetailClose:   $('sd-detail-close'),

    // Date range bar
    drPreset:       $('dr-preset'),
    drCustomInputs: $('dr-custom-inputs'),
    drStartDate:    $('dr-start-date'),
    drEndDate:      $('dr-end-date'),
  };

  // ── State ──────────────────────────────────────────────────────────────

  let clientMap = {};      // client_id → name
  let refreshTimer = null;
  let fetchErrors = {};    // endpoint_key → error_message, per-fetch-cycle tracking
  let agentRunsData = null;       // latest agent runs response
  let agentRunFilters = {};       // current filter values
  let agentRunDetail = null;      // current detail view data
  let agentRunsFetchError = null; // per-cycle fetch error for agent runs
  let dateRangeState = { preset: 'this-month' }; // selected date-range preset
  let expandedClientNames = {}; // drilldown: client names with expanded project rows
  let _lastDateRangeKey = null; // tracks previous render's date range context for resetting drilldown
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

  /** Truncate a string with ellipsis if longer than maxLen */
  function truncate(str, maxLen) {
    if (!str) return '--';
    if (str.length <= maxLen) return escHtml(str);
    return escHtml(str.substring(0, maxLen)) + '&hellip;';
  }

  /** Format a short UUID for display */
  function shortUUID(id) {
    if (!id) return '--';
    return String(id).substring(0, 8);
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

  /** Build the agent runs URL from current filter state */
  function buildAgentRunsUrl() {
    var params = [];
    var filters = agentRunFilters;

    if (filters.from_date) {
      params.push('from_date=' + encodeURIComponent(filters.from_date));
    }
    if (filters.to_date) {
      params.push('to_date=' + encodeURIComponent(filters.to_date));
    }
    if (filters.agent) {
      params.push('agent=' + encodeURIComponent(filters.agent));
    }
    if (filters.status) {
      params.push('status=' + encodeURIComponent(filters.status));
    }
    params.push('limit=' + AGENT_RUN_LIMIT);

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

      // Parallel fetches
      const [health, aggTotal, aggByModel, sessions, records, clients, agentRuns, aggClientProjectResult] =
        await Promise.allSettled([
          apiFetch('/health'),
          apiFetch('/api/v1/usage/aggregates?start_date=' + aggStart + '&end_date=' + aggEnd),
          apiFetch('/api/v1/usage/aggregates?start_date=' + aggStart + '&end_date=' + aggEnd + '&group_by=model'),
          apiFetch('/api/v1/usage/sessions?start_date=' + aggStart + '&end_date=' + aggEnd + '&limit=' + SESSION_LIMIT),
          apiFetch('/api/v1/usage/records?start_date=' + aggStart + '&end_date=' + aggEnd + '&limit=' + RECORD_LIMIT + '&sort_by=ingested_at&sort_dir=desc'),
          apiFetch('/admin/clients?limit=' + CLIENT_LIMIT),
          apiFetch(arUrl),
          apiFetch('/api/v1/usage/aggregates?start_date=' + aggStart + '&end_date=' + aggEnd + '&group_by=client,project'),
        ]);

      results.health    = health.status    === 'fulfilled' ? health.value    : null;
      results.aggTotal  = aggTotal.status  === 'fulfilled' ? aggTotal.value  : null;
      results.aggByModel= aggByModel.status=== 'fulfilled' ? aggByModel.value: null;
      results.sessions  = sessions.status  === 'fulfilled' ? sessions.value  : null;
      results.records   = records.status   === 'fulfilled' ? records.value   : null;
      results.clients   = clients.status   === 'fulfilled' ? clients.value   : null;
      results.agentRuns = agentRuns.status === 'fulfilled' ? agentRuns.value : null;
      results.aggClientProject = aggClientProjectResult.status === 'fulfilled' ? aggClientProjectResult.value : null;

      // Track per-endpoint errors
      fetchErrors = {};
      if (health.status    !== 'fulfilled') fetchErrors.health    = health.reason?.message    || 'Health check failed';
      if (aggTotal.status  !== 'fulfilled') fetchErrors.aggTotal  = aggTotal.reason?.message  || 'Aggregates (total) failed';
      if (aggByModel.status!== 'fulfilled') fetchErrors.aggByModel= aggByModel.reason?.message|| 'Aggregates (by model) failed';
      if (sessions.status  !== 'fulfilled') fetchErrors.sessions  = sessions.reason?.message  || 'Sessions query failed';
      if (records.status   !== 'fulfilled') fetchErrors.records   = records.reason?.message   || 'Usage records failed';
      if (clients.status   !== 'fulfilled') fetchErrors.clients   = clients.reason?.message   || 'Clients query failed';
      agentRunsFetchError = agentRuns.status !== 'fulfilled' ? (agentRuns.reason?.message || 'Agent runs query failed') : null;
      fetchErrors.aggClientProject = aggClientProjectResult.status !== 'fulfilled' ? (aggClientProjectResult.reason?.message || 'Client/project query failed') : null;

      // Attach date range for downstream render functions
      results._dateRange = _dateRange;

      // Build client lookup from admin/clients
      if (results.clients && results.clients.items) {
        results.clients.items.forEach(function (c) {
          clientMap[c.id] = c.name || c.id;
        });
      }
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

  /** KPI Row */
  function renderKPIs(data) {
    // Compute range label once for all KPI subtitles
    var rangeLabel = '--';
    if (data._dateRange) {
      rangeLabel = formatRangeLabel(data._dateRange.startDate, data._dateRange.endDate);
    }

    // Set all KPI subtitles to the formatted range label
    els.kpiTokensDetail.textContent = rangeLabel;
    els.kpiCostDetail.textContent = rangeLabel;
    els.kpiSessionsDetail.textContent = rangeLabel;
    els.kpiCollectorsDetail.textContent = rangeLabel;
    els.kpiSourceDbsDetail.textContent = rangeLabel;

    // Total tokens from aggregates total row
    if (data.aggTotal && data.aggTotal.length > 0) {
      var t = data.aggTotal[0];
      var totalTokens = (t.total_input_tokens || 0) + (t.total_output_tokens || 0);
      els.kpiTokens.textContent = fmtNum(totalTokens);
      els.kpiCost.textContent = fmtCost(t.total_estimated_cost_usd);
    }

    // Sessions from sessions API
    if (data.sessions) {
      els.kpiSessions.textContent = fmtNum(data.sessions.total || 0);
    }

    // Collectors & source DBs from health
    if (data.health) {
      var collectors = data.health.collectors || [];
      var srcDbs = data.health.source_databases || [];
      var healthyCol = collectors.filter(function (c) { return c.health === 'healthy'; }).length;
      els.kpiCollectors.textContent = healthyCol + ' / ' + collectors.length;
      els.kpiSourceDbs.textContent = fmtNum(srcDbs.length);
    }
  }

  /** Model Mix — horizontal bar chart */
  function renderModelMix(data) {
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

  /** Live Events Feed */
  function renderLiveEvents(data) {
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

    // Also add high-token sessions as alerts
    if (data.sessions && data.sessions.items) {
      data.sessions.items.slice(0, 5).forEach(function (s) {
        var tokens = (s.total_input_tokens || 0) + (s.total_output_tokens || 0);
        if (tokens > 100000) {
          var label = clientMap[s.client_id] || s.client_id;
          events.push({
            type: 'info',
            icon: '\uD83D\uDCCA',  // 📊
            text: 'High-usage session: <strong>' + escHtml(label) + '</strong> — ' + fmtNum(tokens) + ' tokens',
            time: s.last_message_at || now
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

  /** Recent Sessions */
  function renderSessionsTable(data) {
    if (!data.sessions || !data.sessions.items || data.sessions.items.length === 0) {
      els.sessionsTbody.innerHTML = '<tr><td colspan="9" class="empty-state">No sessions found' + errorIndicator('sessions') + '</td></tr>';
      return;
    }

    var html = '';
    data.sessions.items.forEach(function (s) {
      var clientName = clientMap[s.client_id] || (typeof s.client_id === 'string' ? s.client_id.substring(0, 8) : '--');
      var tokens = (s.total_input_tokens || 0) + (s.total_output_tokens || 0);
      var cost = s.total_estimated_cost_usd;
      var duration = fmtDuration(s.first_message_at, s.last_message_at);
      var title = s.session_title || '--';
      var isActive = s.last_message_at && (Date.now() - new Date(s.last_message_at).getTime()) < SESSION_ACTIVE_WINDOW_MS;

      html += '<tr class="session-row" data-id="' + s.id + '">' +
        '<td>' + escHtml(clientName) + '</td>' +
        '<td class="session-title-col" title="' + escHtml(title) + '">' + truncate(title, 40) + '</td>' +
        '<td>' + fmtDT(s.first_message_at) + '</td>' +
        '<td>' + fmtDT(s.last_message_at) + '</td>' +
        '<td>' + duration + '</td>' +
        '<td>' + (s.message_count || 0) + '</td>' +
        '<td>' + fmtNum(tokens) + '</td>' +
        '<td>' + fmtCost(cost) + '</td>' +
        '<td>' + badge(isActive ? 'active' : 'ended', isActive ? 'badge-active' : 'badge-inactive').outerHTML + '</td>' +
        '</tr>';
    });

    els.sessionsTbody.innerHTML = html;

    // Attach click handlers for session detail view
    var sessionRows = els.sessionsTbody.querySelectorAll('.session-row');
    sessionRows.forEach(function (row) {
      row.addEventListener('click', function () {
        var id = row.getAttribute('data-id');
        if (id) openSessionDetail(id);
      });
    });
  }

  /** Agent Runs Table */
  function renderAgentRunsTable(data) {
    var runs = data && data.items;
    if (!runs || runs.length === 0) {
      var errSuffix = agentRunsFetchError
        ? ' <span class="fetch-error" title="' + escHtml(agentRunsFetchError) + '">\u26A0 Fetch error</span>'
        : '';
      els.arTbody.innerHTML = '<tr><td colspan="10" class="empty-state">No agent runs' + errSuffix + '</td></tr>';
      return;
    }

    var html = '';
    runs.forEach(function (r) {
      var todoProgress = fmtTodoProgress(r.todo_completed, r.todo_total);
      var tokens = (r.total_input_tokens || 0) + (r.total_output_tokens || 0);
      var projectStr = fmtProjectLabel(r);
      var statusCls = statusBadgeClass(r.status);
      var displayTitle = r.session_title || r.title || '(untitled)';

      html += '<tr class="ar-row" data-id="' + r.id + '">' +
        '<td class="clickable ar-title">' + escHtml(displayTitle) + '</td>' +
        '<td>' + badge(r.status, statusCls).outerHTML + '</td>' +
        '<td>' + escHtml(r.agent || '--') + '</td>' +
        '<td>' + escHtml(projectStr) + '</td>' +
        '<td>' + todoProgress + '</td>' +
        '<td>' + fmtCodeChanges(r.code_changes_total) + '</td>' +
        '<td>' + fmtCost(r.total_estimated_cost_usd) + '</td>' +
        '<td>' + fmtNum(tokens) + '</td>' +
        '<td>' + fmtRelative(r.last_updated_at) + '</td>' +
        '<td>' + (r.child_run_count || 0) + '</td>' +
        '</tr>';
    });

    els.arTbody.innerHTML = html;

    // Attach click handlers for detail view
    var rows = els.arTbody.querySelectorAll('.ar-row');
    rows.forEach(function (row) {
      row.addEventListener('click', function () {
        var id = row.getAttribute('data-id');
        if (id) openAgentRunDetail(id);
      });
    });
  }

  /** Render Client/Project Usage Breakdown two-level expandable table */
  function renderClientProjectBreakdown(data) {
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
      var projectId = parts[1] || 'unknown';

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
        projectId: projectId,
        projectLabel: r.project_label || null,
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
    var statusCls = statusBadgeClass(d.status);

    // ── Session Metadata ──
    var html = '<div class="detail-section">' +
      '<div class="detail-section-title">Session Metadata</div>' +
      '<div class="detail-grid">' +
        fieldHtml('Status', badge(d.status, statusCls).outerHTML) +
        fieldHtml('Title', escHtml(d.title || '--')) +
        fieldHtml('Internal ID', shortUUID(d.id)) +
        fieldHtml('External ID', escHtml(d.external_session_id || '--')) +
        fieldHtml('Client ID', shortUUID(d.client_id)) +
        fieldHtml('Source DB', shortUUID(d.source_database_id)) +
        fieldHtml('Messages', d.message_count != null ? fmtNum(d.message_count) : '--') +
        fieldHtml('Duration', duration) +
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
        var cStatusCls = statusBadgeClass(c.status);
        html += '<div class="detail-child-item">' +
          '<span>' + shortUUID(c.id) + '</span>' +
          '<span>' + badge(c.status, cStatusCls).outerHTML + '</span>' +
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
        fieldHtml('Cached Tokens', fmtNum(d.total_cached_tokens)) +
        fieldHtml('Total Tokens', fmtNum(tokens)) +
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

  /** Fetch and display session detail */
  async function openSessionDetail(sessionId) {
    els.sdDetailOverlay.classList.add('visible');
    els.sdDetailBody.innerHTML = '<p class="empty-state">Loading detail&hellip;</p>';
    els.sdDetailTitle.textContent = 'Session Detail';

    try {
      var data = await apiFetch('/api/v1/usage/agent-runs/' + encodeURIComponent(sessionId));
      renderSessionDetail(data);
    } catch (e) {
      els.sdDetailBody.innerHTML = '<p class="empty-state">Failed to load detail: ' + escHtml(e.message) + '</p>';
      console.error('Session detail fetch error:', e);
    }
  }

  /** Close the session detail overlay */
  function closeSessionDetail() {
    els.sdDetailOverlay.classList.remove('visible');
  }

  /** Render Session Detail Panel */
  function renderSessionDetail(d) {
    if (!d) {
      els.sdDetailBody.innerHTML = '<p class="empty-state">No detail data available</p>';
      return;
    }

    els.sdDetailTitle.textContent = escHtml(d.title || 'Session Detail');

    var duration = fmtDuration(d.first_message_at, d.last_message_at);
    var projectStr = fmtProjectLabel(d);

    // Extract session context fields
    var ctx = d.session_context || {};
    var model = ctx.session_model || '--';
    var additions = ctx.code_change_additions != null ? Number(ctx.code_change_additions) : 0;
    var deletions = ctx.code_change_deletions != null ? Number(ctx.code_change_deletions) : 0;
    var netChange = additions - deletions;
    var netLabel = netChange >= 0 ? '+' + fmtNum(netChange) : fmtNum(netChange);

    var html = '';

    // ── Project ──
    html += '<div class="detail-section">' +
      '<div class="detail-section-title">Project</div>' +
      '<div class="detail-grid">' +
        fieldHtml('Project / Worktree', escHtml(projectStr)) +
        fieldHtml('Source Directory', escHtml(ctx.source_directory || '--')) +
      '</div></div>';

    // ── Code Changes ──
    html += '<div class="detail-section">' +
      '<div class="detail-section-title">Code Changes</div>' +
      '<div class="detail-grid">' +
        fieldHtml('Files Changed', fmtCodeChanges(d.code_changes_total)) +
        fieldHtml('Additions', fmtNum(additions || 0)) +
        fieldHtml('Deletions', fmtNum(deletions || 0)) +
        fieldHtml('Net Change', netLabel) +
      '</div></div>';

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

    // ── Session Metadata ──
    html += '<div class="detail-section">' +
      '<div class="detail-section-title">Session Metadata</div>' +
      '<div class="detail-grid">' +
        fieldHtml('Model', escHtml(model)) +
        fieldHtml('Agent', escHtml(d.agent || '--')) +
        fieldHtml('Duration', duration) +
        fieldHtml('Messages', d.message_count != null ? fmtNum(d.message_count) : '--') +
        fieldHtml('First Message', fmtDT(d.first_message_at)) +
        fieldHtml('Last Message', fmtDT(d.last_message_at)) +
        fieldHtml('Input Tokens', fmtNum(d.total_input_tokens)) +
        fieldHtml('Output Tokens', fmtNum(d.total_output_tokens)) +
        fieldHtml('Est. Cost', fmtCost(d.total_estimated_cost_usd)) +
      '</div></div>';

    // ── Drill-down Link ──
    if (d.loki_search_url) {
      html += '<div class="detail-section">' +
        '<a href="' + escHtml(d.loki_search_url) + '" target="_blank" rel="noopener" class="detail-loki-link">' +
        '\u2197 Open in Grafana Explore</a></div>';
    }

    els.sdDetailBody.innerHTML = html;
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

  // ── Orchestration ─────────────────────────────────────────────────────

  async function refreshDashboard() {
    try {
      if (els.dashboard) els.dashboard.classList.add('refreshing');
      var data = await fetchAll();
      renderHeader(data);
      renderKPIs(data);
      renderModelMix(data);
      renderLiveEvents(data);
      renderCollectorDistribution(data);
      renderCollectorsTable(data);
      renderAgentsTable(data);
      renderSessionsTable(data);
      renderAgentRunsTable(data.agentRuns);
      renderClientProjectBreakdown(data);
    } catch (e) {
      console.error('Dashboard refresh failed:', e);
      showError('Dashboard refresh error: ' + e.message);
    } finally {
      if (els.dashboard) els.dashboard.classList.remove('refreshing');
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
    // Re-fetch agent runs with new filters, update table
    var url = buildAgentRunsUrl();
    apiFetch(url).then(function (data) {
      agentRunsData = data;
      agentRunsFetchError = null;
      renderAgentRunsTable(data);
    }).catch(function (e) {
      agentRunsFetchError = e.message || 'Agent runs query failed';
      renderAgentRunsTable(null);
      console.error('Agent runs filter fetch error:', e);
    });
  }

  function setupAgentRunEventHandlers() {
    // Apply button
    if (els.arFilterApply) {
      els.arFilterApply.addEventListener('click', applyFilters);
    }

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

    // Session detail close button
    if (els.sdDetailClose) {
      els.sdDetailClose.addEventListener('click', closeSessionDetail);
    }

    // Session detail overlay click to close
    if (els.sdDetailOverlay) {
      els.sdDetailOverlay.addEventListener('click', function (e) {
        if (e.target === els.sdDetailOverlay) {
          closeSessionDetail();
        }
      });
    }

    // ESC key to close any open overlay
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        if (els.sdDetailOverlay && els.sdDetailOverlay.classList.contains('visible')) {
          closeSessionDetail();
        } else if (els.arDetailOverlay && els.arDetailOverlay.classList.contains('visible')) {
          els.arDetailOverlay.classList.remove('visible');
          agentRunDetail = null;
        }
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
    var sidebarItems = document.querySelectorAll('.sidebar-item');
    var tabContents = document.querySelectorAll('.tab-content');

    function activateTab(tabName) {
      // Deactivate all
      sidebarItems.forEach(function (item) { item.classList.remove('active'); });
      tabContents.forEach(function (tab) { tab.classList.remove('active'); });

      // Activate target
      var targetItem = document.querySelector('.sidebar-item[data-tab="' + tabName + '"]');
      var targetTab = document.getElementById('tab-' + tabName);
      if (targetItem) targetItem.classList.add('active');
      if (targetTab) targetTab.classList.add('active');
    }

    sidebarItems.forEach(function (item) {
      item.addEventListener('click', function () {
        var tabName = item.getAttribute('data-tab');
        if (tabName) activateTab(tabName);
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

})();
