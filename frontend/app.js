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
  const AGG_WINDOW_DAYS = 30;        // window for aggregates (KPIs + model mix)
  const SESSION_WINDOW_DAYS = 7;     // window for sessions
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

  // ── Active tab state ──────────────────────────────────────────────────
  let activeTab = 'overview';

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

    // Clients / Projects
    cpTbody:        $('clients-projects-tbody'),

    // Sidebar
    sidebarLinks:   document.querySelectorAll('.sidebar-link'),

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
  };

  // ── State ──────────────────────────────────────────────────────────────

  let clientMap = {};      // client_id → name
  let refreshTimer = null;
  let fetchErrors = {};    // endpoint_key → error_message, per-fetch-cycle tracking
  let agentRunsData = null;       // latest agent runs response
  let agentRunFilters = {};       // current filter values
  let agentRunDetail = null;      // current detail view data
  let agentRunsFetchError = null; // per-cycle fetch error for agent runs

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
    const aggStart = daysAgo(AGG_WINDOW_DAYS);
    const aggEnd = nowISO();
    const sessStart = daysAgo(SESSION_WINDOW_DAYS);
    const sessEnd = nowISO();

    const results = {};
    fetchErrors = {};  // Clear previous errors

    try {
      // Build agent runs URL with current filters
      var arUrl = buildAgentRunsUrl();

      // Client-project aggregates URL
      var cpUrl = '/api/v1/usage/aggregates?start_date=' + aggStart + '&end_date=' + aggEnd + '&group_by=client,project';

      // Parallel fetches
      const [health, aggTotal, aggByModel, sessions, records, clients, agentRuns, aggByClientProject] =
        await Promise.allSettled([
          apiFetch('/health'),
          apiFetch('/api/v1/usage/aggregates?start_date=' + aggStart + '&end_date=' + aggEnd),
          apiFetch('/api/v1/usage/aggregates?start_date=' + aggStart + '&end_date=' + aggEnd + '&group_by=model'),
          apiFetch('/api/v1/usage/sessions?start_date=' + sessStart + '&end_date=' + sessEnd + '&limit=' + SESSION_LIMIT),
          apiFetch('/api/v1/usage/records?start_date=' + aggStart + '&end_date=' + aggEnd + '&limit=' + RECORD_LIMIT + '&sort_by=ingested_at&sort_dir=desc'),
          apiFetch('/admin/clients?limit=' + CLIENT_LIMIT),
          apiFetch(arUrl),
          apiFetch(cpUrl),
        ]);

      results.health        = health.status        === 'fulfilled' ? health.value        : null;
      results.aggTotal      = aggTotal.status      === 'fulfilled' ? aggTotal.value      : null;
      results.aggByModel    = aggByModel.status    === 'fulfilled' ? aggByModel.value    : null;
      results.sessions      = sessions.status      === 'fulfilled' ? sessions.value      : null;
      results.records       = records.status       === 'fulfilled' ? records.value       : null;
      results.clients       = clients.status       === 'fulfilled' ? clients.value       : null;
      results.agentRuns     = agentRuns.status     === 'fulfilled' ? agentRuns.value     : null;
      results.aggByClientProject = aggByClientProject.status === 'fulfilled' ? aggByClientProject.value : null;

      // Track per-endpoint errors
      fetchErrors = {};
      if (health.status    !== 'fulfilled') fetchErrors.health    = health.reason?.message    || 'Health check failed';
      if (aggTotal.status  !== 'fulfilled') fetchErrors.aggTotal  = aggTotal.reason?.message  || 'Aggregates (total) failed';
      if (aggByModel.status!== 'fulfilled') fetchErrors.aggByModel= aggByModel.reason?.message|| 'Aggregates (by model) failed';
      if (sessions.status  !== 'fulfilled') fetchErrors.sessions  = sessions.reason?.message  || 'Sessions query failed';
      if (records.status   !== 'fulfilled') fetchErrors.records   = records.reason?.message   || 'Usage records failed';
      if (clients.status   !== 'fulfilled') fetchErrors.clients   = clients.reason?.message   || 'Clients query failed';
      if (aggByClientProject.status !== 'fulfilled') fetchErrors.aggByClientProject = aggByClientProject.reason?.message || 'Aggregates (by client/project) failed';
      agentRunsFetchError = agentRuns.status !== 'fulfilled' ? (agentRuns.reason?.message || 'Agent runs query failed') : null;

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
    // Total tokens from aggregates total row
    if (data.aggTotal && data.aggTotal.length > 0) {
      var t = data.aggTotal[0];
      var totalTokens = (t.total_input_tokens || 0) + (t.total_output_tokens || 0);
      els.kpiTokens.textContent = fmtNum(totalTokens);
      els.kpiTokensDetail.textContent = 'input ' + fmtNum(t.total_input_tokens) + ' / output ' + fmtNum(t.total_output_tokens);
      els.kpiCost.textContent = fmtCost(t.total_estimated_cost_usd);
      els.kpiCostDetail.textContent = t.record_count + ' records';
    }

    // Sessions from sessions API
    if (data.sessions) {
      els.kpiSessions.textContent = fmtNum(data.sessions.total || 0);
      els.kpiSessionsDetail.textContent = 'last ' + SESSION_WINDOW_DAYS + ' days';
    }

    // Collectors & source DBs from health
    if (data.health) {
      var collectors = data.health.collectors || [];
      var srcDbs = data.health.source_databases || [];
      var healthyCol = collectors.filter(function (c) { return c.health === 'healthy'; }).length;
      els.kpiCollectors.textContent = healthyCol + ' / ' + collectors.length;
      els.kpiCollectorsDetail.textContent = 'total ' + collectors.length + ' registered';
      els.kpiSourceDbs.textContent = fmtNum(srcDbs.length);
      els.kpiSourceDbsDetail.textContent = srcDbs.filter(function (d) { return d.health === 'healthy'; }).length + ' healthy';
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
      els.sessionsTbody.innerHTML = '<tr><td colspan="8" class="empty-state">No sessions in the last ' + SESSION_WINDOW_DAYS + ' days' + errorIndicator('sessions') + '</td></tr>';
      return;
    }

    var html = '';
    data.sessions.items.forEach(function (s) {
      var clientName = clientMap[s.client_id] || (typeof s.client_id === 'string' ? s.client_id.substring(0, 8) : '--');
      var tokens = (s.total_input_tokens || 0) + (s.total_output_tokens || 0);
      var cost = s.total_estimated_cost_usd;
      var duration = fmtDuration(s.first_message_at, s.last_message_at);
      var isActive = s.last_message_at && (Date.now() - new Date(s.last_message_at).getTime()) < SESSION_ACTIVE_WINDOW_MS;

      html += '<tr>' +
        '<td>' + escHtml(clientName) + '</td>' +
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
      var projectStr = r.project_id || r.workspace_id || '--';
      if (r.project_id && r.workspace_id && r.workspace_id !== r.project_id) {
        projectStr = r.project_id + ' / ' + r.workspace_id;
      }
      var statusCls = statusBadgeClass(r.status);

      html += '<tr class="ar-row" data-id="' + r.id + '">' +
        '<td class="clickable ar-title">' + escHtml(r.title || '(untitled)') + '</td>' +
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

  /** Fetch and display agent run detail */
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
    var projectStr = d.project_id || d.workspace_id || '--';
    if (d.project_id && d.workspace_id && d.workspace_id !== d.project_id) {
      projectStr = d.project_id + ' / ' + d.workspace_id;
    }
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
        html += '<div class="detail-todo-item">' +
          '<span class="detail-todo-icon ' + iconCls + '">' + icon + '</span>' +
          '<span>' + escHtml(t.description) + '</span>' +
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

    // ── Session Context placeholder ──
    html += '<div class="detail-section">' +
      '<div class="detail-section-title">Session Context</div>' +
      '<div class="detail-field-value" style="color:var(--text-muted)">' +
        (d.session_context ? 'Session context available' : 'No Session Context recorded (placeholder)') +
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

  // ── Tab management ────────────────────────────────────────────────────

  /**
   * Return the tab name from the URL hash (e.g. "#clients-projects").
   * Defaults to "overview" when hash is empty or unrecognised.
   */
  function getTabFromHash() {
    var hash = window.location.hash.replace('#', '');
    if (hash === 'clients-projects') return 'clients-projects';
    return 'overview';
  }

  /**
   * Switch the active tab, updating the sidebar and content visibility.
   */
  function switchTab(tabName) {
    if (tabName !== 'overview' && tabName !== 'clients-projects') {
      tabName = 'overview';
    }
    activeTab = tabName;

    // Update sidebar links
    els.sidebarLinks.forEach(function (link) {
      var tab = link.getAttribute('data-tab');
      if (tab === tabName) {
        link.classList.add('active');
      } else {
        link.classList.remove('active');
      }
    });

    // Update content visibility
    document.querySelectorAll('[data-tab-content]').forEach(function (el) {
      if (el.getAttribute('data-tab-content') === tabName || tabName === 'overview' && el.getAttribute('data-tab-content') === 'overview') {
        el.classList.add('active');
      } else {
        el.classList.remove('active');
      }
    });

    // Update hash
    if (tabName === 'overview') {
      history.replaceState(null, '', window.location.pathname);
    } else {
      history.replaceState(null, '', '#' + tabName);
    }
  }

  /**
   * Initialise tab click handlers and set the initial tab from hash.
   */
  function initTabs() {
    // Set up click handlers
    els.sidebarLinks.forEach(function (link) {
      link.addEventListener('click', function (e) {
        e.preventDefault();
        var tab = link.getAttribute('data-tab');
        switchTab(tab);
      });
    });

    // Listen for hash changes (back/forward navigation)
    window.addEventListener('hashchange', function () {
      var tab = getTabFromHash();
      switchTab(tab);
    });

    // Set initial tab from hash
    var initialTab = getTabFromHash();
    switchTab(initialTab);
  }

  // ── Clients / Projects Table ──────────────────────────────────────────

  /** Render the two-level Clients/Projects expandable table. */
  function renderClientsProjectsTable(data) {
    var rows = data && data.aggByClientProject;
    if (!rows || rows.length === 0) {
      var errSuffix = fetchErrors.aggByClientProject
        ? ' <span class="fetch-error" title="' + escHtml(fetchErrors.aggByClientProject) + '">\u26A0 Fetch error</span>'
        : '';
      els.cpTbody.innerHTML = '<tr><td colspan="6" class="empty-state">No client/project data in the ' + AGG_WINDOW_DAYS + '-day window' + errSuffix + '</td></tr>';
      return;
    }

    // Parse pipe-delimited group_value: "ClientName|ProjectName"
    // Build nested structure: { clientName: { projects: { projectName: row }, totals: {...} } }
    var clientMap = {};

    rows.forEach(function (r) {
      var gv = r.group_value || '|';
      var parts = gv.split('|');
      var clientName = parts[0] || '(unknown)';
      var projectName = parts.length > 1 ? parts.slice(1).join('|') : '(unknown)';

      if (!clientMap[clientName]) {
        clientMap[clientName] = {
          projects: {},
          totalTokens: 0,
          totalCost: 0,
          totalSessions: 0,
          totalModels: 0,
          totalRecords: 0,
        };
      }

      var client = clientMap[clientName];
      client.projects[projectName] = r;
      client.totalTokens += (r.total_input_tokens || 0) + (r.total_output_tokens || 0);
      client.totalCost += Number(r.total_estimated_cost_usd || 0);
      client.totalSessions += r.session_count || 0;
      client.totalModels += r.model_count || 0;
      client.totalRecords += r.record_count || 0;
    });

    // Build HTML
    var html = '';
    var clientNames = Object.keys(clientMap).sort();

    clientNames.forEach(function (cn) {
      var client = clientMap[cn];
      var projectNames = Object.keys(client.projects).sort();
      var clientId = 'cp-' + escHtml(cn).replace(/\s+/g, '-');

      // Client-level row
      html += '<tr class="client-row" data-client-id="' + clientId + '" data-expanded="false">' +
        '<td><span class="expand-toggle" data-client-id="' + clientId + '">\u25B6</span></td>' +
        '<td class="client-name">' + escHtml(cn) + '</td>' +
        '<td>' + fmtNum(client.totalTokens) + '</td>' +
        '<td>' + fmtCost(client.totalCost) + '</td>' +
        '<td>' + fmtNum(client.totalSessions) + '</td>' +
        '<td>' + fmtNum(client.totalModels) + '</td>' +
        '</tr>';

      // Project-level rows (hidden initially)
      projectNames.forEach(function (pn) {
        var pr = client.projects[pn];
        var projTokens = (pr.total_input_tokens || 0) + (pr.total_output_tokens || 0);
        html += '<tr class="project-row" data-client-id="' + clientId + '">' +
          '<td></td>' +
          '<td class="project-name">' + escHtml(pn) + '</td>' +
          '<td>' + fmtNum(projTokens) + '</td>' +
          '<td>' + fmtCost(pr.total_estimated_cost_usd) + '</td>' +
          '<td>' + fmtNum(pr.session_count || 0) + '</td>' +
          '<td>' + fmtNum(pr.model_count || 0) + '</td>' +
          '</tr>';
      });
    });

    els.cpTbody.innerHTML = html;

    // Attach expand/collapse handlers using event delegation
    els.cpTbody.addEventListener('click', function (e) {
      // Find the closest clickable target
      var toggle = e.target.closest('.expand-toggle, .client-row');
      if (!toggle) return;

      var clientId;
      if (toggle.classList.contains('expand-toggle')) {
        clientId = toggle.getAttribute('data-client-id');
      } else if (toggle.classList.contains('client-row')) {
        clientId = toggle.getAttribute('data-client-id');
      }

      if (!clientId) return;

      var clientRow = els.cpTbody.querySelector('.client-row[data-client-id="' + clientId + '"]');
      var projectRows = els.cpTbody.querySelectorAll('.project-row[data-client-id="' + clientId + '"]');
      var toggleEl = els.cpTbody.querySelector('.expand-toggle[data-client-id="' + clientId + '"]');
      var isExpanded = clientRow && clientRow.getAttribute('data-expanded') === 'true';

      if (isExpanded) {
        // Collapse
        projectRows.forEach(function (pr) { pr.classList.remove('visible'); });
        if (clientRow) clientRow.setAttribute('data-expanded', 'false');
        if (toggleEl) { toggleEl.textContent = '\u25B6'; toggleEl.classList.remove('expanded'); }
      } else {
        // Expand
        projectRows.forEach(function (pr) { pr.classList.add('visible'); });
        if (clientRow) clientRow.setAttribute('data-expanded', 'true');
        if (toggleEl) { toggleEl.textContent = '\u25BC'; toggleEl.classList.add('expanded'); }
      }
    });
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
      renderClientsProjectsTable(data);
      renderAgentRunsTable(data.agentRuns);
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

    // Detail close button
    if (els.arDetailClose) {
      els.arDetailClose.addEventListener('click', function () {
        els.arDetailOverlay.classList.remove('visible');
        agentRunDetail = null;
      });
    }

    // Overlay click to close
    if (els.arDetailOverlay) {
      els.arDetailOverlay.addEventListener('click', function (e) {
        if (e.target === els.arDetailOverlay) {
          els.arDetailOverlay.classList.remove('visible');
          agentRunDetail = null;
        }
      });
    }

    // Enter key on agent filter triggers apply
    if (els.arFilterAgent) {
      els.arFilterAgent.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') applyFilters();
      });
    }
  }

  function startAutoRefresh() {
    initTabs();
    setupAgentRunEventHandlers();
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

})();
