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
  window.renderAfkRunDetail(issue577GithubRunDetail);
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
  assert(html.indexOf('Token Usage') !== -1, 'GitHub: usage step Token Usage');
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
  window.renderAfkRunDetail(issue577GitlabRunDetail);
  var html = afkDetailBodyEl.innerHTML;
  assert(html.indexOf('afk-chain') !== -1, 'GitLab: chain div rendered');
  assert(html.indexOf('badge-completed') !== -1, 'GitLab: run status badge');
  assert(html.indexOf('badge-merged') !== -1, 'GitLab: outcome merged badge');
  assert(html.indexOf('change_request:601') !== -1, 'GitLab: change request entity id');
  assert(html.indexOf('commit:gl789abc') !== -1, 'GitLab: commit entity id');
  assert(html.indexOf('review:gl601') !== -1, 'GitLab: review entity id');
  assert(html.indexOf('merge_event:601') !== -1, 'GitLab: merge event entity id');
  assert(html.indexOf('cloudnative-pg') !== -1, 'GitLab: repository name');
  assert(html.indexOf('resolves #501') !== -1, 'GitLab: evidence detail');
  assert(html.indexOf('Token Usage') !== -1, 'GitLab: usage step Token Usage');
})();

console.log('\u25B6 issue #577 \u2014 provenance: cost + cache data');

(function () {
  window.renderAfkRunDetail(issue577GitlabRunDetail);
  var html = afkDetailBodyEl.innerHTML;
  assert(html.indexOf('estimated cost') !== -1 || html.indexOf('$') !== -1,
    'GitLab: estimated cost label visible');
  assert(html.indexOf('cache') !== -1, 'GitLab: cache data visible');
  window.renderAfkRunDetail(issue577GithubRunDetail);
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
  window.renderAfkRunDetail(issue577GithubAmbiguousDetail);
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
  window.renderAfkRunDetail(issue577GithubParkedDetail);
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
  window.renderAfkRunDetail(issue577GitlabUnresolvedDetail);
  var html = afkDetailBodyEl.innerHTML;
  assert(html.indexOf('badge-failed') !== -1, 'unresolved: run status failed');
  assert(html.indexOf('badge-abandoned') !== -1, 'unresolved: outcome abandoned');
  assert(html.indexOf('issue:801') !== -1, 'unresolved: noise issue visible');
  assert(html.indexOf('provisional') !== -1, 'unresolved: provisional links visible');
  assert(html.indexOf('temporal_inference') !== -1, 'unresolved: temporal method visible');
  assert(html.indexOf('No sessions linked') !== -1, 'unresolved: empty sessions state');
  assert(html.indexOf('cloudnative-pg') !== -1, 'unresolved: repository name visible');
})();

console.log('\u25B6 issue #577 \u2014 relationship: confidence/method/evidence/resolver_version');

(function () {
  // Verify all four provenance fields are rendered on each entity link
  window.renderAfkRunDetail(issue577GithubRunDetail);
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
  var savedConsoleError = appJsSandbox.console.error;
  appJsSandbox.console.error = function () {};
  window.openAfkRunDetail('not-found-run').then(function () {
    assert(afkDetailBodyEl.innerHTML.indexOf('AFK run not found') !== -1,
      'retry/error: 404 renders not-found message');
    appJsSandbox.console.error = savedConsoleError;
    pendingAsyncBlocks--;
  });
})();

console.log('\u25B6 issue #577 \u2014 retry/error: 500 handling');

(function () {
  pendingAsyncBlocks++;
  appJsSandbox.fetch = function () {
    return Promise.resolve({ ok: false, status: 500 });
  };
  var savedConsoleError = appJsSandbox.console.error;
  appJsSandbox.console.error = function () {};
  window.openAfkRunDetail('error-run').then(function () {
    assert(afkDetailBodyEl.innerHTML.indexOf('Failed to load') !== -1,
      'retry/error: 500 renders failure message');
    assert(afkDetailBodyEl.innerHTML.indexOf('AFK run not found') === -1,
      'retry/error: 500 does NOT show not-found message');
    appJsSandbox.console.error = savedConsoleError;
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
  var savedConsoleError = appJsSandbox.console.error;
  appJsSandbox.console.error = function () {};
  window.openAfkRunDetail('network-error').then(function () {
    assert(afkDetailBodyEl.innerHTML.indexOf('Failed to load') !== -1,
      'retry/error: network failure renders failure message');
    assert(afkDetailBodyEl.innerHTML.indexOf('network down') !== -1,
      'retry/error: network error message included');
    appJsSandbox.console.error = savedConsoleError;
    appJsSandbox.fetch = function () { return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } }); };
    pendingAsyncBlocks--;
  });
})();

// ── 7. Cross-provider parity (GitHub vs GitLab) ─────────────────────────

console.log('\u25B6 issue #577 \u2014 cross-provider parity: GitHub vs GitLab fixture flow');

(function () {
  // Both fixtures render the same chain structure: the same canonical step
  // order, the same badge types, the same provenance fields.
  window.renderAfkRunDetail(issue577GithubRunDetail);
  var ghHtml = afkDetailBodyEl.innerHTML;
  window.renderAfkRunDetail(issue577GitlabRunDetail);
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

  // Both carry Token Usage in usage step
  assert(ghHtml.indexOf('Token Usage') !== -1, 'parity: GitHub usage step');
  assert(glHtml.indexOf('Token Usage') !== -1, 'parity: GitLab usage step');

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
