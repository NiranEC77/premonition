-- supabase/schema.sql — premonition
--
-- Run once in the Supabase SQL Editor.
--
-- Design notes:
--   * SQLite on the agents laptop is the system of record for RAW FACTS.
--     Supabase holds PUBLISHED artifacts: what we said, what happened, how wrong we were.
--   * watchlist_events is APPEND-ONLY on purpose. It is the only thing standing between
--     us and survivorship bias in the backtest. Never UPDATE or DELETE a row here.
--   * All writes come from the agents laptop using the service_role key.
--     The dashboard reads with the anon key, governed by RLS below.

-- ---------------------------------------------------------------------------
-- The watchlist, as an append-only event log
-- ---------------------------------------------------------------------------
-- To reconstruct the universe as it stood on any past date:
--
--   select ticker from (
--     select distinct on (ticker) ticker, action
--     from watchlist_events
--     where effective_date <= '2026-05-01'
--     order by ticker, effective_date desc, created_at desc
--   ) t where action = 'add';
--
-- This is why we do not just keep a mutable list of tickers.

create table watchlist_events (
  id             bigserial primary key,
  ticker         text        not null,
  action         text        not null check (action in ('add','remove')),
  effective_date date        not null,
  note           text,
  created_at     timestamptz not null default now()
);

create index on watchlist_events (ticker, effective_date desc);

-- ---------------------------------------------------------------------------
-- Per-ticker health. Overwritten each run. Powers the "Watchlist Health" panel.
-- A ticker we cannot see is a fact she needs, not an embarrassment to hide.
-- ---------------------------------------------------------------------------
create table ticker_health (
  ticker      text primary key,
  status      text not null check (status in (
                'ok','degraded','insufficient_history','no_data',
                'unresolved','excluded_tradability','restricted')),
  reason      text,                       -- human-readable: "no option chain", "did you mean CIFR?"
  cluster     text,
  missing     text[],                     -- which features were unavailable
  checked_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- One row per run that produced output
-- ---------------------------------------------------------------------------
create table briefs (
  id             bigserial primary key,
  session_date   date        not null,    -- the trading day being forecast
  stage          text        not null check (stage in ('prep','early','draft','lock')),
  status         text        not null check (status in ('published','suppressed','failed')),
  regime         text,                    -- 'normal' | 'cpi' | 'fomc' | 'jobs' | 'half_day'
  regime_note    text,                    -- "CPI at 08:30 — index gap will swamp single-name signal"
  cluster_story  text,                    -- "Crypto complex: BTC -6% overnight"
  data_age_secs  int,                     -- staleness of the freshest pre-market pull
  sources_failed text[],
  verify_rejects int not null default 0,  -- claims dropped by the verifier
  published_at   timestamptz not null default now(),
  unique (session_date, stage)
);

create index on briefs (session_date desc);

-- ---------------------------------------------------------------------------
-- The picks themselves
-- ---------------------------------------------------------------------------
create table picks (
  id                 bigserial primary key,
  brief_id           bigint  not null references briefs(id) on delete cascade,
  ticker             text    not null,
  rank               int     not null,
  score              numeric not null,           -- normalized surprise score
  expected_move_pct  numeric,                    -- raw expected opening range %
  premarket_gap_pct  numeric,
  premarket_rvol     numeric,                    -- the single most important feature
  spread_est         numeric,                    -- or proxy; friction is the model
  p_continuation     numeric check (p_continuation between 0 and 1),
  cluster            text,
  halt_prone         boolean not null default false,
  recent_ipo         boolean not null default false,
  levels             jsonb,   -- {premarket_high, premarket_low, prior_close, prior_high, prior_low}
  reasons            jsonb,   -- [{text, source_url, source_ts}] — every claim carries its receipt
  demoted_note       text,    -- why the orchestrator overrode the raw score, if it did
  unique (brief_id, ticker)
);

create index on picks (ticker);

-- ---------------------------------------------------------------------------
-- What actually happened. Graded at 16:15. Published whether it flatters us or not.
-- ---------------------------------------------------------------------------
create table grades (
  id                bigserial primary key,
  pick_id           bigint not null references picks(id) on delete cascade,
  session_date      date   not null,
  open_price        numeric,
  high_0935         numeric,
  low_0935          numeric,
  price_0935        numeric,
  actual_range_pct  numeric,        -- the target: opening range magnitude
  continued         boolean,        -- did the gap run, or fill?
  brier             numeric,        -- on the continuation call
  actual_rank       int,            -- where it really landed among the universe
  was_tradable      boolean,        -- did the tradability gate leak?
  graded_at         timestamptz not null default now(),
  unique (pick_id)
);

create index on grades (session_date desc);

-- ---------------------------------------------------------------------------
-- The baseline we must beat. Stored per session so the scoreboard is not a claim,
-- it is a comparison. If we cannot beat "sort by gap size", the dashboard says so.
-- ---------------------------------------------------------------------------
create table baselines (
  session_date      date primary key,
  naive_gap_top5    text[],    -- the 5 biggest pre-market gaps
  naive_mean_range  numeric,   -- what that dumb strategy would have shown
  naive_rvol_top5   text[],    -- the 5 highest pre-market RVOL
  rvol_mean_range   numeric,
  universe_mean_range numeric,
  our_mean_range    numeric,
  computed_at       timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Row Level Security
--
-- service_role (agents laptop) bypasses RLS entirely and can write.
-- anon (the browser) gets read-only. Without this, anyone holding the public
-- anon key could rewrite her briefs.
-- ---------------------------------------------------------------------------
alter table watchlist_events enable row level security;
alter table ticker_health    enable row level security;
alter table briefs           enable row level security;
alter table picks            enable row level security;
alter table grades           enable row level security;
alter table baselines        enable row level security;

create policy "public read" on ticker_health for select to anon using (true);
create policy "public read" on picks         for select to anon using (true);
create policy "public read" on grades        for select to anon using (true);
create policy "public read" on baselines     for select to anon using (true);

-- Only published briefs are visible. A suppressed brief (stale data, too many verify
-- rejects) must never render as if it were real.
create policy "public read published" on briefs
  for select to anon using (status = 'published');

-- watchlist_events: readable, never writable by anon. The editor UI writes through a
-- server-side route holding the service key — never from the browser.
create policy "public read" on watchlist_events for select to anon using (true);

-- ---------------------------------------------------------------------------
-- Seed the watchlist. effective_date is the day the universe took this shape.
-- ---------------------------------------------------------------------------
insert into watchlist_events (ticker, action, effective_date, note)
select unnest(array[
  'META','APP','LITE','TSLA','NVDA','HOOD','ASTS','RKLB','NBIS','CRDO',
  'SPCX','ALAB','STX','MRVL','WDC','BE','LRCX','MU','SNDK','CRCL',
  'COIN','AMD','MSFT','MSTR','SHOP','SPOT','TWLO','CBRS','TEAM','SEI',
  'RDDT','BABA','AMZN','NOW','ROKU','PLTR','W','CF','CRWV','ANET',
  'ZS','RBLX','TTD','MP','OKLO','GSAT','PSIX','IREN','CCL','ZETA',
  'QBTS','OKTA','QUBT','AAPL','EOSE','ORCL','FCX','TE','RDW','POET',
  'LUNR','NVTS','TEM','ECHO','HON','IRDM','ICLR','NFLX','RVMD','COHR',
  'GOOG','AAOI','DDOG','RMBS','INTC','CIEN','POWL','ARM','AEIS','TSM',
  'VRT','FN','MKSI','NVMI','SMCI','WULF','APLD','MARA','RGTI','CIFER','FCEL'
]), 'add', '2026-07-13', 'initial seed';
