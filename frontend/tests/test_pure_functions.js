/**
 * Unit tests for Aurora Glass pure helper functions.
 *
 * Run with: node frontend/tests/test_pure_functions.js
 *
 * These tests verify the formatting, derivation, and escaping logic
 * that is extracted from frontend/app.js into testable pure functions.
 */

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

// ── Summary ─────────────────────────────────────────────────────────────

console.log('');
console.log('\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550');
console.log('  Passed:', passed, ' / Failed:', failed);
console.log('\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550');

process.exit(failed > 0 ? 1 : 0);
