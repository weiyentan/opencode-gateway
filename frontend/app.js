/* ═══════════════════════════════════════════════════════════════════════════
   Aurora Glass Dashboard — OpenCode Gateway Telemetry
   Fetches data from the Gateway API and renders all dashboard sections.
   ═══════════════════════════════════════════════════════════════════════════ */

// ── Configuration ──────────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 30000;
const DATE_RANGE_DAYS = 7;

// ── State ──────────────────────────────────────────────────────────────────

const state = {
  health: null,
  modelAggregates: [],
  totalAggregate: null,
  clients: [],
  sessions: [],
  records: [],
  errors: [],
};

// ── DOM refs ───────────────────────────────────────────────────────────────

const $loading = document.getElementById('loading-overlay');
const $errorBanner = document.getElementById('error-banner');
const $errorMessage = document.getElementById('error-message');
const $errorDismiss = document.getElementById('error-dismiss');
const $lastUpdated = document.getElementById('last-updated');
const $refreshIndicator = document.getElementById('refresh-indicator');
const $refreshBtn = document.getElementById('refresh-btn');

// ── Utilities ──────────────────────────────────────────────────────────────

/** ISO date string for N days ago. */
function daysAgo(days) {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString();
}

/** Current ISO date string. */
function nowISO() {
  return new Date().toISOString();
}

/**
 * Fetch a Gateway API endpoint and unwrap the envelope.
 * Returns parsed `data` on success, throws on error.
 */
async function apiFetch(url) {
  const res = await fetch(url);
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`HTTP ${res.status}: ${body}`);
  }
  const json = await res.json();

  // Gateway envelopes: {status: "ok", data: ...} or {status: "error", error: {...}}
  if (json.status === 'ok' && 'data' in json) {
    return json.data;
  }
  if (json.status === 'error') {
    throw new Error(json.error?.message || 'Unknown API error');
  }
  // Fall-back: return raw (unwrapped endpoint)
  return json;
}

/** Abbreviate token counts: 1.2K, 3.4M, 1.5B */
function formatTokens(n) {
  if (n == null || n === 0) return '0';
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return n.toLocaleString();
}

/** Format cost as USD. */
function formatCost(n) {
  if (n == null) return '$0.00';
  const num = typeof n === 'string' ? parseFloat(n) : n;
  if (isNaN(num)) return '$0.00';
  return '$' + num.toFixed(2);
}

/** Format a duration between two ISO timestamps. */
function formatDuration(startISO, endISO) {
  const start = new Date(startISO);
  const end = endISO ? new Date(endISO) : new Date();
  const ms = end - start;
  if (ms <= 0) return '<1m';

  const mins = Math.floor(ms / 60000);
  const hours = Math.floor(mins / 60);
  const remainingMins = mins % 60;

  if (hours > 0) {
    return `${hours}h ${remainingMins}m`;
  }
  return `${mins}m`;
}

/** Relative time string for a timestamp. */
function relativeTime(iso) {
  if (!iso) return '—';
  const now = new Date();
  const then = new Date(iso);
  const diffSec = Math.floor((now - then) / 1000);

  if (diffSec < 60) return `${diffSec}s ago`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
  if (diffSec < 604800) return `${Math.floor(diffSec / 86400)}d ago`;
  return then.toLocaleDateString();
}

/** Short date display. */
function shortDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

/** Format a raw number with commas. */
function formatNumber(n) {
  if (n == null) return '0';
  return n.toLocaleString();
}

// ── Error handling ─────────────────────────────────────────────────────────

function showError(msg) {
  state.errors.push(msg);
  $errorMessage.textContent = msg;
  $errorBanner.classList.remove('hidden');
}

function dismissError() {
  $errorBanner.classList.add('hidden');
}

// ── Loading state ──────────────────────────────────────────────────────────

function hideLoading() {
  $loading.classList.add('hidden');
}

// ── Data fetching ──────────────────────────────────────────────────────────

async function fetchHealth() {
  const data = await apiFetch('/health');
  state.health = data;
}

async function fetchModelAggregates() {
  const start = daysAgo(DATE_RANGE_DAYS);
  const end = nowISO();
  const data = await apiFetch(
    `/api/v1/usage/aggregates?start_date=${encodeURIComponent(start)}&end_date=${encodeURIComponent(end)}&group_by=model`
  );
  state.modelAggregates = Array.isArray(data) ? data : [];
}

async function fetchTotalAggregate() {
  const start = daysAgo(DATE_RANGE_DAYS);
  const end = nowISO();
  const data = await apiFetch(
    `/api/v1/usage/aggregates?start_date=${encodeURIComponent(start)}&end_date=${encodeURIComponent(end)}`
  );
  state.totalAggregate = Array.isArray(data) && data.length > 0 ? data[0] : null;
}

async function fetchClients() {
  const data = await apiFetch('/admin/clients?limit=200');
  state.clients = data.items || [];
}

async function fetchSessions() {
  const start = daysAgo(DATE_RANGE_DAYS);
  const end = nowISO();
  const data = await apiFetch(
    `/api/v1/usage/sessions?start_date=${encodeURIComponent(start)}&end_date=${encodeURIComponent(end)}&limit=50`
  );
  state.sessions = data.items || [];
}

async function fetchRecords() {
  const start = daysAgo(DATE_RANGE_DAYS);
  const end = nowISO();
  const data = await apiFetch(
    `/api/v1/usage/records?start_date=${encodeURIComponent(start)}&end_date=${encodeURIComponent(end)}&limit=50&sort_dir=desc`
  );
  state.records = data.items || [];
}

async function fetchAll() {
  state.errors = [];
  showRefreshIndicator(true);

  const tasks = [
    fetchHealth(),
    fetchModelAggregates(),
    fetchTotalAggregate(),
    fetchClients(),
    fetchSessions(),
    fetchRecords(),
  ];

  const results = await Promise.allSettled(tasks);
  const failures = results.filter((r) => r.status === 'rejected');
  if (failures.length > 0) {
    const msg = failures.map((f) => f.reason?.message || 'Unknown error').join('; ');
    showError(msg);
  }

  renderAll();
  updateLastUpdated();
  showRefreshIndicator(false);
  hideLoading();
}

// ── Refresh indicator ──────────────────────────────────────────────────────

function showRefreshIndicator(active) {
  if (active) {
    $refreshIndicator.classList.add('active');
  } else {
    $refreshIndicator.classList.remove('active');
  }
}

function updateLastUpdated() {
  const now = new Date();
  $lastUpdated.textContent = 'Updated ' + now.toLocaleTimeString();
}

// ── Client name lookup ─────────────────────────────────────────────────────

/** Build a map of client_id → client_name from health + clients data. */
function buildClientNameMap() {
  const map = {};

  // From health collectors
  if (state.health?.collectors) {
    for (const c of state.health.collectors) {
      if (c.credential_id) {
        // Store by client_name as key too
        map[c.client_name] = c.client_name;
      }
    }
  }

  // From clients list (id → name)
  for (const c of state.clients) {
    if (c.id) {
      map[c.id] = c.name;
    }
  }

  // From health source_databases
  if (state.health?.source_databases) {
    for (const s of state.health.source_databases) {
      if (s.client_name) {
        map[s.client_name] = s.client_name;
      }
    }
  }

  // From sessions (client_id lookup)
  for (const s of state.sessions) {
    if (s.client_id && !map[s.client_id]) {
      map[s.client_id] = s.client_id;
    }
  }

  // From records (client_id lookup)
  for (const r of state.records) {
    if (r.client_id && !map[r.client_id]) {
      map[r.client_id] = r.client_id;
    }
  }

  return map;
}

/** Resolve a client_id or name to a display name. */
function clientName(idOrName, nameMap) {
  if (!idOrName) return 'unknown';
  return nameMap[idOrName] || idOrName;
}

// ── Renderers ──────────────────────────────────────────────────────────────

function renderAll() {
  const nameMap = buildClientNameMap();
  renderKpis();
  renderModelMix();
  renderLiveEvents();
  renderCollectorDistribution(nameMap);
  renderCollectorsTable();
  renderAgentsTable(nameMap);
  renderSessionsTable(nameMap);
}

// ── 1. KPI Row ────────────────────────────────────────────────────────────

function renderKpis() {
  const total = state.totalAggregate;
  const health = state.health;

  // Total tokens
  let totalTokens = 0;
  if (total) {
    totalTokens = (total.total_input_tokens || 0)
      + (total.total_output_tokens || 0)
      + (total.total_cached_tokens || 0);
  }
  document.getElementById('kpi-total-tokens').textContent = formatTokens(totalTokens);
  document.getElementById('kpi-total-tokens-sub').textContent =
    total ? `${formatNumber(total.record_count || 0)} records` : '';

  // Cost
  const cost = total?.total_estimated_cost_usd;
  document.getElementById('kpi-cost').textContent = formatCost(cost);
  document.getElementById('kpi-cost-sub').textContent =
    total ? `${formatTokens((total.total_input_tokens || 0) + (total.total_output_tokens || 0))} non-cached` : '';

  // Sessions
  const sessionCount = state.sessions ? state.sessions.length : 0;
  document.getElementById('kpi-sessions').textContent = formatNumber(sessionCount);

  // Healthy collectors
  const collectors = health?.collectors || [];
  const healthyCount = collectors.filter((c) => c.health === 'healthy').length;
  document.getElementById('kpi-healthy-collectors').textContent = formatNumber(healthyCount);
  document.getElementById('kpi-total-collectors').textContent = `of ${formatNumber(collectors.length)} total`;

  // Source databases
  const sourceDbs = health?.source_databases || [];
  document.getElementById('kpi-source-dbs').textContent = formatNumber(sourceDbs.length);
  const activeDbs = sourceDbs.filter((s) => s.health === 'healthy').length;
  document.getElementById('kpi-source-dbs-sub').textContent = `${activeDbs} active`;
}

// ── 2. Model Mix ───────────────────────────────────────────────────────────

function renderModelMix() {
  const container = document.getElementById('model-mix-chart');
  const models = state.modelAggregates;

  if (!models || models.length === 0) {
    container.innerHTML = '<div class="empty-state">No model data available</div>';
    return;
  }

  // Sort by total tokens descending
  const sorted = [...models].sort((a, b) => {
    const aTotal = (a.total_input_tokens || 0) + (a.total_output_tokens || 0);
    const bTotal = (b.total_input_tokens || 0) + (b.total_output_tokens || 0);
    return bTotal - aTotal;
  });

  // Compute max for bar widths
  const maxTokens = sorted.length > 0
    ? (sorted[0].total_input_tokens || 0) + (sorted[0].total_output_tokens || 0)
    : 1;

  container.innerHTML = sorted.slice(0, 8).map((m, i) => {
    const totalM = (m.total_input_tokens || 0) + (m.total_output_tokens || 0);
    const pct = maxTokens > 0 ? Math.round((totalM / maxTokens) * 100) : 0;
    const modelName = m.group_value || 'unknown';
    return `
      <div class="model-bar-row">
        <span class="model-bar-label" title="${escapeHTML(modelName)}">${escapeHTML(modelName)}</span>
        <div class="model-bar-track">
          <div class="model-bar-fill color-${i % 8}" style="width:${Math.max(pct, 2)}%"></div>
        </div>
        <span class="model-bar-value">${formatTokens(totalM)}</span>
      </div>
    `;
  }).join('');
}

// ── 3. Live Events ─────────────────────────────────────────────────────────

function renderLiveEvents() {
  const container = document.getElementById('live-events-feed');
  const countEl = document.getElementById('live-events-count');

  // Build events from multiple sources
  const events = [];

  // Recent records → ingest events
  for (const r of state.records.slice(0, 10)) {
    const model = r.model_name || 'unknown';
    const total = (r.input_tokens || 0) + (r.output_tokens || 0);
    events.push({
      type: 'ingest',
      text: `Usage recorded: ${formatTokens(total)} tokens on <strong>${escapeHTML(model)}</strong>`,
      time: r.ingested_at || r.reported_at,
    });
  }

  // Collector status changes → health events
  if (state.health?.collectors) {
    for (const c of state.health.collectors) {
      if (c.health === 'stale') {
        events.push({
          type: 'warning',
          text: `Collector <strong>${escapeHTML(c.client_name)}</strong> is stale — last heartbeat ${relativeTime(c.last_heartbeat)}`,
          time: c.last_heartbeat,
        });
      } else if (c.health === 'healthy' && c.last_heartbeat) {
        events.push({
          type: 'info',
          text: `Collector <strong>${escapeHTML(c.client_name)}</strong> healthy — ${formatNumber(c.total_records_ingested)} records ingested`,
          time: c.last_heartbeat,
        });
      }
    }
  }

  // Source database status
  if (state.health?.source_databases) {
    for (const s of state.health.source_databases) {
      if (s.health === 'stale') {
        events.push({
          type: 'warning',
          text: `Source DB <strong>${escapeHTML(s.client_name)}</strong> stale`,
          time: s.last_push,
        });
      }
    }
  }

  // New sessions → success events
  for (const s of state.sessions.slice(0, 5)) {
    events.push({
      type: 'success',
      text: `Session active: ${formatNumber(s.message_count || 0)} messages, ${formatTokens((s.total_input_tokens || 0) + (s.total_output_tokens || 0))} tokens`,
      time: s.last_message_at,
    });
  }

  // Sort by time descending, most recent first
  events.sort((a, b) => {
    const ta = a.time ? new Date(a.time).getTime() : 0;
    const tb = b.time ? new Date(b.time).getTime() : 0;
    return tb - ta;
  });

  // Deduplicate — keep only first occurrence of each text
  const seen = new Set();
  const unique = events.filter((e) => {
    if (seen.has(e.text)) return false;
    seen.add(e.text);
    return true;
  });

  const display = unique.slice(0, 15);
  countEl.textContent = state.records.length;

  if (display.length === 0) {
    container.innerHTML = '<div class="empty-state">Waiting for events...</div>';
    return;
  }

  container.innerHTML = display.map((e) => `
    <div class="event-item">
      <span class="event-dot ${e.type}"></span>
      <div class="event-body">
        <div class="event-text">${e.text}</div>
        <div class="event-meta">${shortDate(e.time)}</div>
      </div>
    </div>
  `).join('');
}

// ── 4. Collector Distribution ─────────────────────────────────────────────

function renderCollectorDistribution(nameMap) {
  const container = document.getElementById('collector-dist-grid');
  const collectors = state.health?.collectors || [];

  if (collectors.length === 0) {
    container.innerHTML = '<div class="empty-state">No collectors connected</div>';
    return;
  }

  // Use collector record counts as proxy for token share
  const maxRecords = Math.max(...collectors.map((c) => c.total_records_ingested || 0), 1);

  // Sort by record count descending
  const sorted = [...collectors].sort((a, b) => (b.total_records_ingested || 0) - (a.total_records_ingested || 0));

  container.innerHTML = sorted.map((c) => {
    const pct = maxRecords > 0 ? Math.round(((c.total_records_ingested || 0) / maxRecords) * 100) : 0;
    return `
      <div class="collector-dist-item">
        <span class="collector-dist-indicator ${c.health}"></span>
        <span class="collector-dist-name" title="${escapeHTML(c.client_name)}">${escapeHTML(c.client_name)}</span>
        <span class="collector-dist-tokens">${formatNumber(c.total_records_ingested)} recs</span>
        <div class="collector-dist-bar-track">
          <div class="collector-dist-bar-fill" style="width:${Math.max(pct, 3)}%"></div>
        </div>
      </div>
    `;
  }).join('');
}

// ── 5. Collectors Table ────────────────────────────────────────────────────

function renderCollectorsTable() {
  const tbody = document.getElementById('collectors-tbody');
  const collectors = state.health?.collectors || [];

  if (collectors.length === 0) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="6">No collectors registered</td></tr>';
    return;
  }

  tbody.innerHTML = collectors.map((c) => {
    const statusClass = c.health || 'unknown';
    const tokens = formatTokens(c.total_records_ingested * 100); // rough estimate
    return `
      <tr>
        <td>${escapeHTML(c.client_name)}</td>
        <td><span class="status-pill ${statusClass}">${statusClass}</span></td>
        <td class="mono">${relativeTime(c.last_heartbeat)}</td>
        <td>${formatNumber(c.total_records_ingested)}</td>
        <td>${tokens}</td>
        <td class="mono">—</td>
      </tr>
    `;
  }).join('');
}

// ── 6. Agents & LLMs Table ─────────────────────────────────────────────────

function renderAgentsTable(nameMap) {
  const tbody = document.getElementById('agents-tbody');
  const records = state.records;

  if (!records || records.length === 0) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="6">No agent activity detected</td></tr>';
    return;
  }

  // Group records by client_id + model
  const groups = {};
  for (const r of records) {
    const key = `${r.client_id}|||${r.model_name || 'unknown'}`;
    if (!groups[key]) {
      groups[key] = {
        clientId: r.client_id,
        model: r.model_name || 'unknown',
        requests: 0,
        tokens: 0,
        cost: 0,
        latestTime: r.reported_at,
      };
    }
    groups[key].requests += 1;
    groups[key].tokens += (r.input_tokens || 0) + (r.output_tokens || 0) + (r.cached_tokens || 0);
    groups[key].cost += parseFloat(r.estimated_cost_usd || 0);
    if (r.reported_at && (!groups[key].latestTime || r.reported_at > groups[key].latestTime)) {
      groups[key].latestTime = r.reported_at;
    }
  }

  // Determine status from health data
  const collectorHealth = {};
  if (state.health?.collectors) {
    for (const c of state.health.collectors) {
      collectorHealth[c.client_name] = c.health;
    }
  }

  const rows = Object.values(groups).sort((a, b) => b.tokens - a.tokens);

  tbody.innerHTML = rows.map((row) => {
    const name = clientName(row.clientId, nameMap);
    const health = collectorHealth[name] || 'unknown';
    return `
      <tr>
        <td title="${escapeHTML(row.clientId)}">${escapeHTML(name)}</td>
        <td>${escapeHTML(row.model)}</td>
        <td>${formatNumber(row.requests)}</td>
        <td>${formatTokens(row.tokens)}</td>
        <td class="mono">${formatCost(row.cost)}</td>
        <td><span class="status-pill ${health}">${health}</span></td>
      </tr>
    `;
  }).join('');
}

// ── 7. Recent Sessions ────────────────────────────────────────────────────

function renderSessionsTable(nameMap) {
  const tbody = document.getElementById('sessions-tbody');
  const sessions = state.sessions;

  if (!sessions || sessions.length === 0) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="6">No sessions recorded</td></tr>';
    return;
  }

  tbody.innerHTML = sessions.slice(0, 20).map((s) => {
    const cName = clientName(s.client_id, nameMap);
    const totalTokens = (s.total_input_tokens || 0) + (s.total_output_tokens || 0) + (s.total_cached_tokens || 0);
    const duration = formatDuration(s.first_message_at, s.last_message_at);
    const status = s.last_message_at ? 'active' : 'inactive';

    return `
      <tr>
        <td title="${escapeHTML(s.client_id)}">${escapeHTML(cName)}</td>
        <td>—</td>
        <td>${formatTokens(totalTokens)}</td>
        <td class="mono">${formatCost(s.total_estimated_cost_usd)}</td>
        <td class="mono">${duration}</td>
        <td><span class="status-pill ${status}">${status}</span></td>
      </tr>
    `;
  }).join('');
}

// ── Helpers ────────────────────────────────────────────────────────────────

function escapeHTML(str) {
  if (!str) return '';
  if (typeof str !== 'string') str = String(str);
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ── Init ───────────────────────────────────────────────────────────────────

let pollTimer = null;

async function init() {
  $errorDismiss.addEventListener('click', dismissError);
  $refreshBtn.addEventListener('click', () => {
    fetchAll().catch((err) => {
      showError(err.message || 'Manual refresh failed');
    });
  });

  // Initial fetch
  try {
    await fetchAll();
  } catch (err) {
    showError(err.message || 'Failed to load dashboard');
    hideLoading();
  }

  // Auto-refresh polling
  pollTimer = setInterval(() => {
    fetchAll().catch(() => {
      // Silently handle polling errors — banner shows last error
    });
  }, POLL_INTERVAL_MS);
}

// ── Boot ───────────────────────────────────────────────────────────────────

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
