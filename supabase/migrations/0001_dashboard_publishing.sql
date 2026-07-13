-- supabase/migrations/0001_dashboard_publishing.sql — premonition
--
-- Adds the two tables the Vercel dashboard's /freshness page reads. Run this in the
-- Supabase SQL Editor AFTER supabase/schema.sql (which creates ticker_health, used by
-- /health, and already has its own RLS + anon read policy).
--
-- Same rule as everywhere else in this project: these are PUBLISHED summaries, computed
-- from the raw evidence in /srv/premonition/db/probe.sqlite on the agents laptop by
-- probes/publish_freshness.py, and written here with the service_role key. Nothing in
-- this schema computes anything — it only holds numbers that were computed elsewhere,
-- from real observations, with their own provenance (window_start/window_end,
-- sample_count) attached. The dashboard reads with the anon key, governed by RLS below,
-- same as every other table in this project.

-- ---------------------------------------------------------------------------
-- Per-source freshness summary. Overwritten each publish run (one row per
-- probe+source) — this is a current snapshot, not an event log, same pattern
-- as ticker_health in schema.sql.
-- ---------------------------------------------------------------------------
create table probe_source_freshness (
  probe             text        not null check (probe in ('freshness','friction')),
  source            text        not null,   -- 'yfinance_quote' | 'yfinance_bars_1m' | 'yfinance_daily' | 'finnhub_quote'
  sample_count      int         not null,   -- how many status='ok' observations this summary is built from
  median_lag_secs   numeric,                -- fetched_at - source_ts, across sample_count observations
  min_lag_secs      numeric,
  max_lag_secs      numeric,
  attempt_count     int         not null,   -- ok + error + no_data + rate_limited, for this probe+source+window
  error_count       int         not null default 0,
  error_rate        numeric,                -- error_count::numeric / attempt_count
  last_error        text,                   -- one sample error message, so a 100% error rate is diagnosable
  window_start      timestamptz,            -- earliest fetched_at considered
  window_end        timestamptz,            -- latest fetched_at considered
  computed_at       timestamptz not null default now(),
  primary key (probe, source)
);

-- ---------------------------------------------------------------------------
-- Per-Yahoo-field freeze/update determination. This is what Phase 1c's
-- after-hours run exists to answer: does regularMarketPrice/Volume keep
-- changing after the 16:00 close (dangerous — indistinguishable from a live
-- feed) or correctly freeze while postMarket* takes over. One row per field,
-- overwritten each publish run.
-- ---------------------------------------------------------------------------
create table probe_field_behavior (
  field                         text        primary key,  -- 'regular_market_price', 'postmarket_volume', etc.
  yahoo_field_name              text        not null,     -- 'regularMarketPrice', as Yahoo names it
  status                        text        not null check (status in (
                                  'updates_after_close', 'freezes_after_close', 'insufficient_data')),
  evidence_note                 text,                     -- human-readable summary of what was observed
  classification_formula        text,                     -- versioned rule id, e.g. 'trailing_run_v1'
  distinct_values_before_close  int,
  distinct_values_after_close   int,
  ticks_after_close             int         not null default 0,
  tickers_covered               text[],
  window_start                  timestamptz,
  window_end                    timestamptz,
  computed_at                   timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Row Level Security — same shape as schema.sql: service_role (agents laptop)
-- bypasses RLS and writes; anon (the browser) gets read-only.
-- ---------------------------------------------------------------------------
alter table probe_source_freshness enable row level security;
alter table probe_field_behavior   enable row level security;

create policy "public read" on probe_source_freshness for select to anon using (true);
create policy "public read" on probe_field_behavior   for select to anon using (true);
