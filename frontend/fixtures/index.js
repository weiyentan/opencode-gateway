/**
 * Fixture registry and deterministic selector for AFK Outcomes tests.
 *
 * Provides selectFixture(provider, scenario) that returns the same
 * fixture detail object for identical inputs regardless of call order.
 * No randomness, no clock dependence, stable sort/order.
 *
 * Available scenarios:
 *   complete    - Full lifecycle (issue -> change request -> executions -> merge -> closure)
 *   incomplete  - Open/not-yet-merged lifecycle
 *   cycles      - Repeated develop/review cycles (multiple sessions)
 *   sessions    - Root/child session hierarchy with provisional links
 *   relationships - Provisional links and unresolved relationship states
 */

'use strict';

var githubComplete = require('./github_complete');
var githubIncomplete = require('./github_incomplete');
var githubCycles = require('./github_cycles');
var gitlabComplete = require('./gitlab_complete');
var gitlabIncomplete = require('./gitlab_incomplete');
var gitlabCycles = require('./gitlab_cycles');
var sessionsFixture = require('./sessions');
var relationshipsFixture = require('./relationships');

/**
 * Deterministic fixture registry — keyed by (provider, scenario).
 * Each entry is { build: Function, scenario: string, provider: string }.
 * build() returns a fresh detail object (callers should not mutate it).
 */
var REGISTRY = {
  'github:complete': githubComplete,
  'github:incomplete': githubIncomplete,
  'github:cycles': githubCycles,
  'github:sessions': sessionsFixture,
  'github:relationships': relationshipsFixture,
  'gitlab:complete': gitlabComplete,
  'gitlab:incomplete': gitlabIncomplete,
  'gitlab:cycles': gitlabCycles,
  'gitlab:sessions': sessionsFixture,
  'gitlab:relationships': relationshipsFixture
};

/** All available scenario names. */
var SCENARIOS = ['complete', 'incomplete', 'cycles', 'sessions', 'relationships'];

/** All available provider names. */
var PROVIDERS = ['github', 'gitlab'];

/**
 * Select a deterministic fixture by provider and scenario.
 * Returns a fresh detail object (callers should not mutate the cached build).
 * Returns null if the combination is unknown.
 *
 * @param {string} provider - 'github' or 'gitlab'
 * @param {string} scenario - 'complete', 'incomplete', 'cycles', 'sessions', 'relationships'
 * @returns {Object|null} detail object matching buildAfkChain input shape
 */
function selectFixture(provider, scenario) {
  var key = provider + ':' + scenario;
  var entry = REGISTRY[key];
  if (!entry) return null;
  return entry.build();
}

/**
 * List all available (provider, scenario) pairs.
 * Deterministic order: providers sorted, scenarios sorted within each provider.
 * @returns {Array<{provider: string, scenario: string}>}
 */
function listFixtures() {
  var result = [];
  PROVIDERS.forEach(function (p) {
    SCENARIOS.forEach(function (s) {
      result.push({ provider: p, scenario: s });
    });
  });
  return result;
}

/**
 * Check cross-provider parity: equivalent GitHub and GitLab fixtures
 * produce identical normalized shape (same keys, same semantic states).
 * Returns { equal: boolean, differences: string[] }.
 */
function checkCrossProviderParity(scenario) {
  var differences = [];
  var gh = selectFixture('github', scenario);
  var gl = selectFixture('gitlab', scenario);

  if (!gh || !gl) {
    return { equal: false, differences: ['Missing fixture for ' + scenario] };
  }

  // Both must have the same top-level keys
  var ghKeys = Object.keys(gh).sort();
  var glKeys = Object.keys(gl).sort();
  if (JSON.stringify(ghKeys) !== JSON.stringify(glKeys)) {
    differences.push('Top-level keys differ: ' + JSON.stringify(ghKeys) + ' vs ' + JSON.stringify(glKeys));
  }

  // Both must have the same run status and outcome status
  if (gh.run.status !== gl.run.status) {
    differences.push('Run status differs: ' + gh.run.status + ' vs ' + gl.run.status);
  }
  if (gh.run.outcome_status !== gl.run.outcome_status) {
    differences.push('Outcome status differs: ' + gh.run.outcome_status + ' vs ' + gl.run.outcome_status);
  }

  // Both must use change_request entity type (normalized vocabulary)
  var ghCRTypes = gh.change_requests.map(function (cr) { return cr.entity_type; });
  var glCRTypes = gl.change_requests.map(function (cr) { return cr.entity_type; });
  if (JSON.stringify(ghCRTypes) !== JSON.stringify(glCRTypes)) {
    differences.push('Change request entity types differ');
  }

  // Both must have the same number of issues, sessions, agents
  if (gh.issues.length !== gl.issues.length) {
    differences.push('Issue count differs: ' + gh.issues.length + ' vs ' + gl.issues.length);
  }
  if (gh.sessions.length !== gl.sessions.length) {
    differences.push('Session count differs: ' + gh.sessions.length + ' vs ' + gl.sessions.length);
  }
  if (gh.agents.length !== gl.agents.length) {
    differences.push('Agent count differs: ' + gh.agents.length + ' vs ' + gl.agents.length);
  }

  // Both must differ only in provider-specific identity fields
  if (gh.run.provider === gl.run.provider) {
    differences.push('Providers must differ but both are: ' + gh.run.provider);
  }
  if (gh.run.provider !== 'github') {
    differences.push('GitHub fixture must have provider=github, got: ' + gh.run.provider);
  }
  if (gl.run.provider !== 'gitlab') {
    differences.push('GitLab fixture must have provider=gitlab, got: ' + gl.run.provider);
  }

  return { equal: differences.length === 0, differences: differences };
}

module.exports = {
  REGISTRY: REGISTRY,
  SCENARIOS: SCENARIOS,
  PROVIDERS: PROVIDERS,
  selectFixture: selectFixture,
  listFixtures: listFixtures,
  checkCrossProviderParity: checkCrossProviderParity
};
