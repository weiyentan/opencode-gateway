/**
 * Aurora Glass — OpenCode Gateway Telemetry Dashboard
 *
 * Consumes the Gateway REST API and renders a glass-themed observability
 * dashboard.  Works with the existing endpoints only (no new endpoints).
 *
 * API endpoints consumed (read-only):
 *   GET /health                         — collector/source-db health
 *   GET /api/v1/usage/aggregates        — aggregate tokens/cost
 *   GET /api/v1/usage/records           — paginated usage records
 *   GET /api/v1/usage/sessions          — session summaries
 */

/* ==========================================================================
   Configuration
   ========================================================================== */

const CONFIG = {
    /** Polling interval in milliseconds (30 seconds per AC). */
    POLL_INTERVAL: 30000,

    /** How many recent sessions to display. */
    MAX_SESSIONS: 15,

    /** How many live events to keep in the feed. */
    MAX_EVENTS: 50,

    /** Time window for dashboard data (past 24 hours). */
    LOOKBACK_MS: 24 * 60 * 60 * 1000,

    /** Staleness threshold in seconds (matches GATEWAY_HEARTBEAT_THRESHOLD default). */
    HEARTBEAT_THRESHOLD: 300,
};

/* ==========================================================================
   State
   ========================================================================== */

const STATE = {
    apiKey: localStorage.getItem('aurora_api_key') || '',
    connected: false,
    /** Health response from GET /health. */
    healthData: null,
    /** Aggregates keyed by group dimension. */
    aggregatesByModel: [],
    aggregatesByClient: [],
    aggregatesTotal: null,
    /** Sessions summary. */
    sessions: [],
    /** Records (for model/LLM details). */
    records: [],
    /** Event log for the live-events feed. */
    events: [],
    /** Timer handle for polling. */
    pollTimer: null,
};

/* ==========================================================================
   Utility Helpers
   ========================================================================== */

/** Format a number with locale-aware separators. */
function fmtNumber(n) {
    if (n == null || isNaN(n)) return '—';
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
    return n.toLocaleString();
}

/** Format a cost value in USD. */
function fmtCost(c) {
    if (c == null || isNaN(c)) return '—';
    const n = Number(c);
    if (n < 0.01) return '<$0.01';
    return '$' + n.toFixed(2);
}

/** Format a duration in a human-friendly way. */
function fmtDuration(ms) {
    if (!ms || ms < 0) return '—';
    const sec = Math.floor(ms / 1000);
    if (sec < 60) return sec + 's';
    const min = Math.floor(sec / 60);
    if (min < 60) return min + 'm ' + (sec % 60) + 's';
    const hr = Math.floor(min / 60);
    return hr + 'h ' + (min % 60) + 'm';
}

/** Return a short relative-time string. */
function timeAgo(isoString) {
    if (!isoString) return '—';
    const diff = Date.now() - new Date(isoString).getTime();
    const sec = Math.floor(diff / 1000);
    if (sec < 60) return 'just now';
    if (sec < 3600) return Math.floor(sec / 60) + 'm ago';
    if (sec < 86400) return Math.floor(sec / 3600) + 'h ago';
    return Math.floor(sec / 86400) + 'd ago';
}

/** Return a short time string. */
function shortTime(isoString) {
    if (!isoString) return '—';
    return new Date(isoString).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/** Get date range for the lookback window. */
function dateRange() {
    const end = new Date();
    const start = new Date(end.getTime() - CONFIG.LOOKBACK_MS);
    return {
        start_date: start.toISOString(),
        end_date: end.toISOString(),
    };
}

/** Derive status from a heartbeat timestamp. */
function deriveStatus(lastSeen) {
    if (!lastSeen) return 'unknown';
    const diff = (Date.now() - new Date(lastSeen).getTime()) / 1000;
    return diff <= CONFIG.HEARTBEAT_THRESHOLD ? 'healthy' : 'stale';
}

/** Infer an LLM provider from a model name string. */
function inferProvider(modelName) {
    if (!modelName) return '—';
    const m = modelName.toLowerCase();
    if (m.includes('gpt') || m.includes('o1') || m.includes('o3') || m.includes('o4') || m.includes('text-')) return 'OpenAI';
    if (m.includes('claude')) return 'Anthropic';
    if (m.includes('gemini')) return 'Google';
    if (m.includes('llama') || m.includes('codellama') || m.includes('mixtral') || m.includes('mistral')) return 'Meta / Mistral';
    if (m.includes('deepseek')) return 'DeepSeek';
    if (m.includes('bedrock')) return 'AWS Bedrock';
    if (m.includes('vertex')) return 'GCP Vertex';
    if (m.includes('azure')) return 'Azure';
    return 'Other';
}

/* ==========================================================================
   API Client
   ========================================================================== */

/** Build headers for authenticated requests. */
function authHeaders() {
    const h = { 'Content-Type': 'application/json' };
    if (STATE.apiKey) {
        h['Authorization'] = 'Bearer ' + STATE.apiKey;
    }
    return h;
}

/** Fetch and unwrap the standard envelope: { status: "ok", data: ... } or { status: "error", ... }. */
async function apiFetch(url, options = {}) {
    const resp = await fetch(url, { ...options, headers: { ...authHeaders(), ...(options.headers || {}) } });
    const json = await resp.json();

    // The backend wraps successful responses in { status: "ok", data: ... }
    // Error responses are { status: "error", error: { code, message } }
    // Some endpoints like /health return raw data without the envelope.
    if (json && json.status === 'ok' && json.data !== undefined) {
        if (!resp.ok) {
            console.warn('API error:', json.error || json);
        }
        return { ok: resp.ok, status: resp.status, data: json.data };
    }

    // Raw response (e.g., /health) or already-unwrapped
    return { ok: resp.ok, status: resp.status, data: json };
}

/* ==========================================================================
   Data Fetching
   ========================================================================== */

/** Fetch health data (collectors, source-dbs, last ingest). */
async function fetchHealth() {
    try {
        const result = await apiFetch('/health');
        if (result.ok && result.data) {
            STATE.healthData = result.data;
            addEvent('info', 'Health data refreshed');
            return result.data;
        }
        addEvent('alert', 'Health endpoint returned unexpected response');
    } catch (err) {
        console.error('fetchHealth failed:', err);
        addEvent('alert', 'Health endpoint unreachable: ' + err.message);
    }
    return null;
}

/** Fetch aggregate data grouped by model. */
async function fetchAggregatesByModel() {
    try {
        const range = dateRange();
        const params = new URLSearchParams({
            ...range,
            group_by: 'model',
        });
        const result = await apiFetch('/api/v1/usage/aggregates?' + params);
        if (result.ok && Array.isArray(result.data)) {
            STATE.aggregatesByModel = result.data;
            return result.data;
        }
    } catch (err) {
        console.error('fetchAggregatesByModel failed:', err);
    }
    return [];
}

/** Fetch aggregate data grouped by client. */
async function fetchAggregatesByClient() {
    try {
        const range = dateRange();
        const params = new URLSearchParams({
            ...range,
            group_by: 'client',
        });
        const result = await apiFetch('/api/v1/usage/aggregates?' + params);
        if (result.ok && Array.isArray(result.data)) {
            STATE.aggregatesByClient = result.data;
            return result.data;
        }
    } catch (err) {
        console.error('fetchAggregatesByClient failed:', err);
    }
    return [];
}

/** Fetch total aggregates (no group_by). */
async function fetchAggregatesTotal() {
    try {
        const range = dateRange();
        const params = new URLSearchParams(range);
        const result = await apiFetch('/api/v1/usage/aggregates?' + params);
        if (result.ok && Array.isArray(result.data) && result.data.length > 0) {
            STATE.aggregatesTotal = result.data[0];
            return result.data[0];
        }
    } catch (err) {
        console.error('fetchAggregatesTotal failed:', err);
    }
    return null;
}

/** Fetch recent sessions. */
async function fetchSessions() {
    try {
        const range = dateRange();
        const params = new URLSearchParams({
            ...range,
            limit: String(CONFIG.MAX_SESSIONS),
            offset: '0',
        });
        const result = await apiFetch('/api/v1/usage/sessions?' + params);
        if (result.ok && result.data && Array.isArray(result.data.items)) {
            STATE.sessions = result.data.items;
            return result.data.items;
        }
    } catch (err) {
        console.error('fetchSessions failed:', err);
    }
    return [];
}

/** Fetch records (for populating model details in Agents & LLMs table). */
async function fetchRecords() {
    try {
        const range = dateRange();
        const params = new URLSearchParams({
            ...range,
            limit: '200',
            offset: '0',
        });
        const result = await apiFetch('/api/v1/usage/records?' + params);
        if (result.ok && result.data && Array.isArray(result.data.items)) {
            STATE.records = result.data.items;
            return result.data.items;
        }
    } catch (err) {
        console.error('fetchRecords failed:', err);
    }
    return [];
}

/* ==========================================================================
   Event Feed
   ========================================================================== */

/** Add an event to the live-events feed. */
function addEvent(type, message) {
    STATE.events.unshift({
        type: type,       // 'warning', 'alert', 'info', 'recovery'
        message: message,
        time: new Date().toISOString(),
    });
    if (STATE.events.length > CONFIG.MAX_EVENTS) {
        STATE.events.length = CONFIG.MAX_EVENTS;
    }
}

/* ==========================================================================
   Rendering: KPI Row
   ========================================================================== */

function renderKpiRow() {
    const totals = STATE.aggregatesTotal || {};
    const health = STATE.healthData || {};
    const collectors = health.collectors || [];
    const sourceDbs = health.source_databases || [];
    const sessions = STATE.sessions || [];

    const totalTokens = (totals.total_input_tokens || 0) + (totals.total_output_tokens || 0);

    setKpi('kpi-value-tokens', fmtNumber(totalTokens));
    setKpi('kpi-value-cost', fmtCost(totals.total_estimated_cost_usd));
    setKpi('kpi-value-sessions', fmtNumber(sessions.length));

    const healthyCollectors = collectors.filter(c => c.health === 'healthy').length;
    setKpi('kpi-value-collectors', healthyCollectors + '/' + collectors.length);
    setKpi('kpi-value-source-dbs', fmtNumber(sourceDbs.length));
}

/** Update a single KPI value element. */
function setKpi(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
}

/* ==========================================================================
   Rendering: Model Mix (Bar Chart)
   ========================================================================== */

function renderModelMix() {
    const container = document.getElementById('model-mix-chart');
    const countEl = document.getElementById('model-mix-count');
    if (!container) return;

    const models = (STATE.aggregatesByModel || []).slice();
    if (models.length === 0) {
        container.innerHTML = '<div class="empty-state">No model data yet</div>';
        if (countEl) countEl.textContent = '0 models';
        return;
    }

    // Sort by token count descending
    models.sort((a, b) => {
        const ta = a.total_input_tokens + a.total_output_tokens;
        const tb = b.total_input_tokens + b.total_output_tokens;
        return tb - ta;
    });

    // Total tokens for percentage calculation
    const maxTokens = Math.max(1, models[0].total_input_tokens + models[0].total_output_tokens);

    let html = '<div class="bar-chart">';
    models.forEach((m, i) => {
        const tokens = m.total_input_tokens + m.total_output_tokens;
        const pct = Math.round((tokens / maxTokens) * 100);
        const modelClass = 'model-' + (i % 6);
        html += `
            <div class="bar-row fade-up">
                <span class="bar-label" title="${escHtml(m.group_value)}">${escHtml(m.group_value)}</span>
                <div class="bar-track">
                    <div class="bar-fill ${modelClass}" style="width:${pct}%">
                        <span class="bar-value">${fmtNumber(tokens)}</span>
                    </div>
                </div>
            </div>`;
    });
    html += '</div>';
    container.innerHTML = html;
    if (countEl) countEl.textContent = models.length + ' models';
}

/* ==========================================================================
   Rendering: Live Events
   ========================================================================== */

function generateEvents() {
    const health = STATE.healthData;
    if (!health) return;

    const now = Date.now();

    // Check for stale collectors
    (health.collectors || []).forEach(c => {
        if (c.health === 'stale') {
            addEvent('warning', `Stale collector: ${escHtml(c.client_name)} — last heartbeat ${timeAgo(c.last_heartbeat)}`);
        }
    });

    // Check for stale source databases
    (health.source_databases || []).forEach(s => {
        if (s.health === 'stale') {
            addEvent('warning', `Stale source DB: ${escHtml(s.client_name)} — last push ${timeAgo(s.last_push)}`);
        }
    });

    // Session alerts: sessions that ended recently with zero tokens
    STATE.sessions.forEach(s => {
        const totalTokens = (s.total_input_tokens || 0) + (s.total_output_tokens || 0);
        const lastMsg = s.last_message_at ? new Date(s.last_message_at).getTime() : 0;
        const age = now - lastMsg;
        if (totalTokens === 0 && age < 600_000 && age > 0) { // within last 10 min
            addEvent('info', `Session ${shortId(s.id)} has zero token usage`);
        }
    });
}

/** Render the live-events feed. */
function renderLiveEvents() {
    const feed = document.getElementById('live-events-feed');
    const countEl = document.getElementById('live-events-count');
    if (!feed) return;

    if (STATE.events.length === 0) {
        feed.innerHTML = '<div class="empty-state">Listening for events&hellip;</div>';
        if (countEl) countEl.textContent = '0';
        return;
    }

    let html = '<div class="event-feed">';
    STATE.events.forEach(e => {
        html += `
            <div class="event-item fade-up">
                <div class="event-icon ${e.type}">${eventIcon(e.type)}</div>
                <div class="event-content">
                    <div class="event-message">${escHtml(e.message)}</div>
                    <div class="event-time">${timeAgo(e.time)}</div>
                </div>
            </div>`;
    });
    html += '</div>';
    feed.innerHTML = html;
    if (countEl) countEl.textContent = STATE.events.length;
}

function eventIcon(type) {
    switch (type) {
        case 'warning': return '!';
        case 'alert': return '!!';
        case 'recovery': return '\u2713';
        case 'info': default: return 'i';
    }
}

/* ==========================================================================
   Rendering: Collector Distribution
   ========================================================================== */

function renderCollectorDistribution() {
    const container = document.getElementById('collector-dist-chart');
    const countEl = document.getElementById('collector-dist-count');
    if (!container) return;

    const health = STATE.healthData;
    const collectors = health ? (health.collectors || []) : [];

    if (collectors.length === 0) {
        container.innerHTML = '<div class="empty-state">No collector data yet</div>';
        if (countEl) countEl.textContent = '0';
        return;
    }

    // Calculate max records for scaling
    const maxRecords = collectors.reduce((m, c) => Math.max(m, c.total_records_ingested || 0), 1);

    let html = '<div class="dist-chart">';
    collectors.forEach(c => {
        const pct = Math.max(5, Math.round(((c.total_records_ingested || 0) / maxRecords) * 100));
        html += `
            <div class="dist-row fade-up">
                <div class="dist-label">
                    <span class="dist-name" title="${escHtml(c.client_name)}">${escHtml(c.client_name)}</span>
                    <span class="dist-status-line">${c.health} · ${fmtNumber(c.total_records_ingested)} recs</span>
                </div>
                <div class="dist-track">
                    <div class="dist-fill ${c.health}" style="width:${pct}%"></div>
                </div>
            </div>`;
    });
    html += '</div>';
    container.innerHTML = html;
    if (countEl) countEl.textContent = collectors.length;
}

/* ==========================================================================
   Rendering: Collectors Table
   ========================================================================== */

function renderCollectorsTable() {
    const tbody = document.getElementById('collectors-table-body');
    const countEl = document.getElementById('collectors-table-count');
    if (!tbody) return;

    const health = STATE.healthData;
    const collectors = health ? (health.collectors || []) : [];

    if (collectors.length === 0) {
        tbody.innerHTML = '<tr class="empty-row"><td colspan="6" class="empty-cell">No collectors connected</td></tr>';
        if (countEl) countEl.textContent = '0';
        return;
    }

    // Build a map of client_name -> aggregate data
    const clientAggMap = {};
    (STATE.aggregatesByClient || []).forEach(a => {
        clientAggMap[a.group_value] = a;
    });

    // Build a session count map per client (client name -> session count)
    const clientSessionCount = {};
    STATE.sessions.forEach(s => {
        const key = s.client_id; // sessions don't have client_name inline
        // Try to match via collectors
    });

    let html = '';
    collectors.forEach(c => {
        const agg = clientAggMap[c.client_name] || {};
        const sessions = agg.record_count || 0;
        const tokens = (agg.total_input_tokens || 0) + (agg.total_output_tokens || 0);
        const cost = agg.total_estimated_cost_usd;
        html += `
            <tr class="fade-up">
                <td title="${escHtml(c.credential_id)}">${escHtml(c.client_name)}</td>
                <td><span class="status-badge ${c.health}">${c.health}</span></td>
                <td>${timeAgo(c.last_heartbeat)}</td>
                <td class="number-mono">${fmtNumber(sessions)}</td>
                <td class="number-mono">${fmtNumber(tokens)}</td>
                <td class="number-mono">${fmtCost(cost)}</td>
            </tr>`;
    });

    tbody.innerHTML = html;
    if (countEl) countEl.textContent = collectors.length;
}

/* ==========================================================================
   Rendering: Agents & LLMs In Use
   ========================================================================== */

function renderAgentsLLMs() {
    const tbody = document.getElementById('agents-llms-body');
    const countEl = document.getElementById('agents-llms-count');
    if (!tbody) return;

    const health = STATE.healthData;
    const collectors = health ? (health.collectors || []) : [];

    if (collectors.length === 0) {
        tbody.innerHTML = '<tr class="empty-row"><td colspan="7" class="empty-cell">No agent data available</td></tr>';
        if (countEl) countEl.textContent = '0';
        return;
    }

    // Derive model details from records to get per-client model info
    const clientModelMap = {};
    STATE.records.forEach(r => {
        // Records have client_id but not client_name
        // We'll map via collector matching
    });

    // Build per-agent rows from collectors and aggregates by model
    // For each collector, show the models they use (from per-client aggregates)
    const clientAggMap = {};
    (STATE.aggregatesByClient || []).forEach(a => {
        clientAggMap[a.group_value] = a;
    });

    // Build a model->provider map from records
    const modelProviderMap = {};
    STATE.records.forEach(r => {
        if (r.model_name && !modelProviderMap[r.model_name]) {
            modelProviderMap[r.model_name] = inferProvider(r.model_name);
        }
    });
    (STATE.aggregatesByModel || []).forEach(a => {
        const name = a.group_value;
        if (name && !modelProviderMap[name]) {
            modelProviderMap[name] = inferProvider(name);
        }
    });

    let rowsRendered = 0;
    let html = '';

    // For each collector, show the dominant model
    collectors.forEach(c => {
        const agg = clientAggMap[c.client_name] || {};
        const requests = agg.record_count || 0;
        const tokens = (agg.total_input_tokens || 0) + (agg.total_output_tokens || 0);
        const cost = agg.total_estimated_cost_usd;

        // Find the dominant model for this client by looking at model aggregates
        // For now, default to the top model in the system
        const topModel = STATE.aggregatesByModel.length > 0
            ? STATE.aggregatesByModel[0].group_value
            : '—';
        const provider = modelProviderMap[topModel] || inferProvider(topModel);

        html += `
            <tr class="fade-up">
                <td title="${escHtml(c.credential_id)}">${escHtml(c.client_name)}</td>
                <td>${escHtml(provider)}</td>
                <td>${escHtml(topModel)}</td>
                <td class="number-mono">${fmtNumber(requests)}</td>
                <td class="number-mono">${fmtNumber(tokens)}</td>
                <td class="number-mono">${fmtCost(cost)}</td>
                <td><span class="status-badge ${c.health === 'healthy' ? 'active' : c.health === 'stale' ? 'stale' : 'inactive'}">${c.health}</span></td>
            </tr>`;
        rowsRendered++;
    });

    tbody.innerHTML = html;
    if (countEl) countEl.textContent = rowsRendered;
}

/* ==========================================================================
   Rendering: Recent Sessions
   ========================================================================== */

function renderRecentSessions() {
    const tbody = document.getElementById('recent-sessions-body');
    const countEl = document.getElementById('recent-sessions-count');
    if (!tbody) return;

    const sessions = STATE.sessions || [];

    if (sessions.length === 0) {
        tbody.innerHTML = '<tr class="empty-row"><td colspan="7" class="empty-cell">No recent sessions</td></tr>';
        if (countEl) countEl.textContent = '0';
        return;
    }

    // Build client name lookup from health data
    const clientNameMap = {};
    const health = STATE.healthData;
    if (health && health.collectors) {
        health.collectors.forEach(c => {
            // Sessions have client_id (UUID), collectors have client_name
            // We need to cross-reference via source_database_id or collector data
        });
    }

    // Build model name lookup from records for each session
    const sessionModelMap = {};
    STATE.records.forEach(r => {
        if (r.session_id && !sessionModelMap[r.session_id]) {
            sessionModelMap[r.session_id] = r.model_name;
        }
    });

    let html = '';
    const maxDisplay = Math.min(sessions.length, CONFIG.MAX_SESSIONS);

    for (let i = 0; i < maxDisplay; i++) {
        const s = sessions[i];
        const totalTokens = (s.total_input_tokens || 0) + (s.total_output_tokens || 0);
        const duration = s.last_message_at && s.first_message_at
            ? new Date(s.last_message_at).getTime() - new Date(s.first_message_at).getTime()
            : null;
        const model = sessionModelMap[s.id] || '—';
        const isActive = s.last_message_at && (Date.now() - new Date(s.last_message_at).getTime()) < CONFIG.HEARTBEAT_THRESHOLD * 1000;

        html += `
            <tr class="fade-up">
                <td class="number-mono" title="${escHtml(s.id)}">${shortId(s.id)}</td>
                <td title="${escHtml(s.client_id)}">${shortId(s.client_id)}</td>
                <td>${escHtml(model)}</td>
                <td class="number-mono">${fmtNumber(totalTokens)}</td>
                <td class="number-mono">${fmtCost(s.total_estimated_cost_usd)}</td>
                <td>${fmtDuration(duration)}</td>
                <td><span class="status-badge ${isActive ? 'active' : 'inactive'}">${isActive ? 'active' : 'ended'}</span></td>
            </tr>`;
    }

    tbody.innerHTML = html;
    if (countEl) countEl.textContent = sessions.length;
}

/* ==========================================================================
   Rendering: Connection & Footer
   ========================================================================== */

function updateConnectionStatus() {
    const dot = document.querySelector('#connection-status .status-dot');
    const label = document.querySelector('#connection-status .status-label');
    if (!dot || !label) return;

    dot.className = 'status-dot ' + (STATE.connected ? 'connected' : 'disconnected');
    label.textContent = STATE.connected ? 'Connected' : 'Disconnected';
}

function updateFooter() {
    const el = document.getElementById('footer-last-update');
    if (el) {
        el.textContent = 'Last update: ' + new Date().toLocaleTimeString();
    }
}

/* ==========================================================================
   Full Dashboard Refresh
   ========================================================================== */

async function refreshDashboard() {
    try {
        // Fetch all data in parallel
        const healthPromise = fetchHealth();
        const modelAggPromise = fetchAggregatesByModel();
        const clientAggPromise = fetchAggregatesByClient();
        const totalAggPromise = fetchAggregatesTotal();
        const sessionsPromise = fetchSessions();
        const recordsPromise = fetchRecords();

        await Promise.all([
            healthPromise, modelAggPromise, clientAggPromise,
            totalAggPromise, sessionsPromise, recordsPromise
        ]);

        // Generate events from fresh data
        generateEvents();

        // Update connection state
        STATE.connected = !!STATE.healthData;
        updateConnectionStatus();

        // Render all sections
        renderKpiRow();
        renderModelMix();
        renderLiveEvents();
        renderCollectorDistribution();
        renderCollectorsTable();
        renderAgentsLLMs();
        renderRecentSessions();
        updateFooter();

    } catch (err) {
        console.error('Dashboard refresh failed:', err);
        STATE.connected = false;
        updateConnectionStatus();
        addEvent('alert', 'Dashboard refresh failed: ' + err.message);
        renderLiveEvents();
        updateFooter();
    }
}

/* ==========================================================================
   Polling
   ========================================================================== */

function startPolling() {
    if (STATE.pollTimer) clearInterval(STATE.pollTimer);
    STATE.pollTimer = setInterval(refreshDashboard, CONFIG.POLL_INTERVAL);
}

function stopPolling() {
    if (STATE.pollTimer) {
        clearInterval(STATE.pollTimer);
        STATE.pollTimer = null;
    }
}

/* ==========================================================================
   API Key Management
   ========================================================================== */

function setupApiKeyPanel() {
    const input = document.getElementById('api-key-input');
    const btn = document.getElementById('api-key-btn');
    if (!input || !btn) return;

    // Pre-fill from localStorage
    if (STATE.apiKey) {
        input.value = STATE.apiKey;
    }

    btn.addEventListener('click', () => {
        const key = input.value.trim();
        STATE.apiKey = key;
        if (key) {
            localStorage.setItem('aurora_api_key', key);
        } else {
            localStorage.removeItem('aurora_api_key');
        }
        addEvent('info', 'API key updated — reconnecting');
        stopPolling();
        refreshDashboard().then(startPolling);
    });

    // Allow Enter key to submit
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') btn.click();
    });
}

/* ==========================================================================
   Manual Refresh Button
   ========================================================================== */

function setupRefreshButton() {
    const btn = document.getElementById('refresh-btn');
    if (!btn) return;
    btn.addEventListener('click', () => {
        btn.style.transform = 'rotate(360deg)';
        btn.style.transition = 'transform 0.6s ease';
        setTimeout(() => {
            btn.style.transform = 'rotate(0deg)';
            btn.style.transition = 'none';
        }, 600);
        refreshDashboard();
    });
}

/* ==========================================================================
   HTML Escaping
   ========================================================================== */

function escHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
}

/** Truncate a UUID for display. */
function shortId(id) {
    if (!id) return '—';
    const s = String(id);
    return s.length > 12 ? s.slice(0, 8) + '...' + s.slice(-4) : s;
}

/* ==========================================================================
   Initialisation
   ========================================================================== */

document.addEventListener('DOMContentLoaded', () => {
    setupApiKeyPanel();
    setupRefreshButton();
    updateConnectionStatus();
    // Initial fetch and start polling
    refreshDashboard().then(startPolling);
});
