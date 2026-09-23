/**
 * Deterministic fixture data for AFK Dashboard Summary (issue #732).
 *
 * Provides a realistic response matching the GET /api/v1/afk/dashboard/summary
 * contract shape: { interval, from_date, to_date, provider, repository,
 * buckets[], derived_at, oldest_derived_at }.
 *
 * Consumed by test_afk_dashboard_summary.js — does NOT run in the browser.
 */

'use strict';

var AFK_DASH_FIXTURE = {
  /** A complete summary response with two daily buckets (no double-counting). */
  dailyResponse: function () {
    return {
      interval: 'daily',
      from_date: '2026-08-01',
      to_date: '2026-08-07',
      provider: null,
      repository: null,
      buckets: [
        {
          period_start: '2026-08-01',
          provider: 'github',
          repository: 'acme/web-app',
          runs_started: 3,
          change_requests_opened: 2,
          change_requests_merged: 1,
          change_requests_closed: 0,
          execution_count: 5,
          successful_execution_count: 4,
          failed_execution_count: 1,
          cancelled_execution_count: 0,
          session_count: 4,
          input_tokens: 50000,
          output_tokens: 15000,
          cache_read_tokens: 30000,
          cache_write_tokens: 1000,
          estimated_cost_usd: 0.42,
          derived_at: '2026-08-08T01:00:00Z',
          oldest_derived_at: '2026-08-01T00:00:00Z'
        },
        {
          period_start: '2026-08-02',
          provider: 'github',
          repository: 'acme/web-app',
          runs_started: 2,
          change_requests_opened: 1,
          change_requests_merged: 1,
          change_requests_closed: 0,
          execution_count: 4,
          successful_execution_count: 3,
          failed_execution_count: 1,
          cancelled_execution_count: 0,
          session_count: 3,
          input_tokens: 40000,
          output_tokens: 12000,
          cache_read_tokens: 25000,
          cache_write_tokens: 800,
          estimated_cost_usd: 0.35,
          derived_at: '2026-08-08T01:00:00Z',
          oldest_derived_at: '2026-08-02T00:00:00Z'
        }
      ],
      derived_at: '2026-08-08T01:00:00Z',
      oldest_derived_at: '2026-08-01T00:00:00Z'
    };
  },

  /** A response with multiple providers — no double-counting when summed. */
  multiProviderResponse: function () {
    return {
      interval: 'daily',
      from_date: '2026-08-01',
      to_date: '2026-08-03',
      provider: null,
      repository: null,
      buckets: [
        {
          period_start: '2026-08-01',
          provider: 'github',
          repository: 'acme/web-app',
          runs_started: 2,
          change_requests_opened: 1,
          change_requests_merged: 0,
          change_requests_closed: 0,
          execution_count: 3,
          successful_execution_count: 2,
          failed_execution_count: 1,
          cancelled_execution_count: 0,
          session_count: 2,
          input_tokens: 30000,
          output_tokens: 8000,
          cache_read_tokens: 15000,
          cache_write_tokens: 500,
          estimated_cost_usd: 0.25,
          derived_at: '2026-08-04T01:00:00Z',
          oldest_derived_at: '2026-08-01T00:00:00Z'
        },
        {
          period_start: '2026-08-01',
          provider: 'gitlab',
          repository: 'cloudnative-pg/cloudnative-pg',
          runs_started: 1,
          change_requests_opened: 0,
          change_requests_merged: 1,
          change_requests_closed: 0,
          execution_count: 2,
          successful_execution_count: 2,
          failed_execution_count: 0,
          cancelled_execution_count: 0,
          session_count: 1,
          input_tokens: 20000,
          output_tokens: 6000,
          cache_read_tokens: 10000,
          cache_write_tokens: 300,
          estimated_cost_usd: 0.18,
          derived_at: '2026-08-04T01:00:00Z',
          oldest_derived_at: '2026-08-01T00:00:00Z'
        }
      ],
      derived_at: '2026-08-04T01:00:00Z',
      oldest_derived_at: '2026-08-01T00:00:00Z'
    };
  },

  /** Empty buckets response. */
  emptyResponse: function () {
    return {
      interval: 'daily',
      from_date: '2026-08-01',
      to_date: '2026-08-07',
      provider: null,
      repository: null,
      buckets: [],
      derived_at: null,
      oldest_derived_at: null
    };
  },

  /** Monthly interval response. */
  monthlyResponse: function () {
    return {
      interval: 'monthly',
      from_date: '2026-01-01',
      to_date: '2026-08-01',
      provider: null,
      repository: null,
      buckets: [
        {
          period_start: '2026-07-01',
          provider: 'github',
          repository: 'acme/web-app',
          runs_started: 15,
          change_requests_opened: 8,
          change_requests_merged: 5,
          change_requests_closed: 2,
          execution_count: 25,
          successful_execution_count: 20,
          failed_execution_count: 3,
          cancelled_execution_count: 2,
          session_count: 18,
          input_tokens: 500000,
          output_tokens: 150000,
          cache_read_tokens: 300000,
          cache_write_tokens: 10000,
          estimated_cost_usd: 4.20,
          derived_at: '2026-08-01T01:00:00Z',
          oldest_derived_at: '2026-07-01T00:00:00Z'
        }
      ],
      derived_at: '2026-08-01T01:00:00Z',
      oldest_derived_at: '2026-07-01T00:00:00Z'
    };
  }
};

module.exports = AFK_DASH_FIXTURE;
