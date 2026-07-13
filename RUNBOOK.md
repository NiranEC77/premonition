# RUNBOOK — premonition

Operational notes for running this system. Kept current as the system is built —
see CLAUDE.md's "Operating notes" section for why.

---

## Running the probes

Phase 1 exists to answer one question before anything else gets built: **can we see
a fresh pre-market price and volume, for free, at 09:15 ET, accurately enough to rank
91 tickers?** Probe A measures freshness (latency). Probe B measures friction (spread).
Neither probe scores, ranks, or interprets anything — they only write raw rows to
`/srv/premonition/db/probe.sqlite`. See `probes/db.py` for the schema, `probes/sources.py`
for what each source actually returns, and `probes/freshness.py` / `probes/friction.py`
for what each probe does with them.

### Prerequisites

```bash
cd ~/code/premonition
source .venv/bin/activate          # yfinance, requests, pytz already installed here
```

`FINNHUB_API_KEY` is read from the environment (normally sourced from
`/etc/premonition/env`, mode 600 — never commit it, never print it). If it's unset,
`finnhub_quote` rows will record `status='error', error='missing FINNHUB_API_KEY'` on
every tick instead of failing the run — that's a legitimate result, not a bug, and the
run still produces useful data from the other sources.

```bash
set -a; source /etc/premonition/env; set +a
```

### Firing a real pre-market run

Both probes take the same flags. `--start` / `--end` are ET clock times (`HH:MM`); the
runner waits for the window to open if launched early, and polls every 30 seconds
(`--interval` to change) until the window closes.

```bash
# Probe A — freshness (price, volume, source timestamp)
bin/premonition-probe --probe a --start 08:00 --end 09:30

# Probe B — friction (bid/ask where available, plus the range/volume spread proxy)
bin/premonition-probe --probe b --start 08:00 --end 09:30
```

Run them in two terminals (or two `tmux` panes) simultaneously — they write to the same
`probe.sqlite` but are independent processes, independent polling loops, and independent
log files, so one crashing does not take the other down.

Defaults:
- Tickers: `NVDA, AAPL, SMCI, RGTI, POET, SPCX` (spans the liquidity range: mega-cap down
  to a recent IPO with no gap history). Override with `--tickers TSLA,MARA,...`.
- DB: `/srv/premonition/db/probe.sqlite`. Override with `--db`.
- Logs: `/srv/premonition/logs/probe-{freshness,friction}-YYYY-MM-DD.log`, and mirrored to
  stdout. Override the directory with `--log-dir`.

Each tick is logged with its duration. A tick that takes longer than `--interval` logs a
warning rather than silently drifting or skipping a tick — the run does not try to "catch
up" by tick-storming; it just resyncs to the next scheduled tick.

### Smoke-testing without waiting for pre-market

`--once` runs a single tick immediately, ignoring `--start`/`--end`, and exits. Use this to
confirm the environment is sane before relying on an actual pre-market window (e.g. after
touching `probes/sources.py`, or on a machine that hasn't run this before):

```bash
bin/premonition-probe --probe a --once --tickers NVDA,SPCX
bin/premonition-probe --probe b --once --tickers NVDA,SPCX
```

### Reading the results

Nothing is aggregated for you on purpose — that analysis is the point of running the
probes, and doing it inside the probe would risk baking in an assumption before the data
justifies one. Query `probe.sqlite` directly:

```bash
sqlite3 /srv/premonition/db/probe.sqlite

-- freshness: how stale was each source's claimed timestamp vs. our own clock?
select source, ticker, fetched_at, source_ts, status,
       (julianday(fetched_at) - julianday(source_ts)) * 86400 as lag_seconds
from observations
where probe = 'freshness' and status = 'ok'
order by ticker, fetched_at;

-- which sources/tickers errored, and how, across the whole run
select source, ticker, status, error, count(*)
from observations
group by source, ticker, status, error
order by source, ticker;

-- friction: bid/ask coverage vs. the daily range/volume proxy
select ticker, source, bid, ask, spread_proxy, spread_proxy_pct, fetched_at
from observations
where probe = 'friction'
order by ticker, fetched_at;

-- rate limiting encountered during the run
select * from rate_limit_events order by fetched_at;
```

A `status != 'ok'` row is itself a result — a ticker/source pair that never returns data
during the window is exactly as informative as one that does, and probes never fill that
gap with a value from another source or a retry.

### The after-hours freeze test (tonight, 15:55–18:00 ET)

The single most dangerous failure mode for this system: `regularMarketPrice` /
`regularMarketVolume` keep reading as valid numbers after the 16:00 close, indistinguishable
from a live feed, and get published at 08:30/09:15 as if they were fresh. The after-hours
window is the cheapest place to catch that, because we already know the ground truth
(the regular session closed at 16:00) and can watch what each field actually does across it.

Runs automatically via the systemd timer (`systemd/premonition-probe-afterhours.timer`) —
see "Installing the after-hours timer" below. No manual start needed. To fire it by hand
instead:

```bash
bin/premonition-probe --probe a --start 15:55 --end 18:00
```

Queries, once it's run:

```bash
sqlite3 /srv/premonition/db/probe.sqlite

-- Does regular_market_price / regular_market_volume change after 16:00, or freeze?
-- A frozen field will show the SAME value repeated across every fetched_at past 16:00.
select fetched_at, regular_market_price, regular_market_volume, regular_market_time,
       postmarket_price, postmarket_volume, postmarket_time, market_state
from observations
where probe = 'freshness' and source = 'yfinance_quote' and status = 'ok'
order by ticker, fetched_at;

-- Collapse to one row per distinct value, per ticker, to see freeze points at a glance —
-- if regular_market_price has one row before 16:00 and never changes again while
-- postmarket_price keeps producing new rows, that confirms the freeze.
select ticker, regular_market_price, regular_market_volume,
       min(fetched_at) as first_seen, max(fetched_at) as last_seen, count(*) as ticks
from observations
where probe = 'freshness' and source = 'yfinance_quote' and status = 'ok'
group by ticker, regular_market_price, regular_market_volume
order by ticker, first_seen;

-- Bar-volume forensics: confirm the forming bar was never used, and see the
-- cumulative total build up tick over tick.
select ticker, fetched_at, last_completed_bar_volume, forming_bar_volume,
       cumulative_volume_since_open, cumulative_bar_count
from observations
where probe = 'freshness' and source = 'yfinance_bars_1m' and status = 'ok'
order by ticker, fetched_at;
```

### Installing the after-hours timer

The `claude-orch` account has no sudo/wheel membership on this box, so these are **user**
systemd units (`systemctl --user`), not system units — no root needed, and nothing for
anyone to run by hand beyond this one-time install:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/premonition-probe-afterhours.service ~/.config/systemd/user/
cp systemd/premonition-probe-afterhours.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now premonition-probe-afterhours.timer

# Without linger, the user manager (and this timer) only runs while claude-orch
# has an active session. Enable linger so it fires even if nothing is logged in
# at 15:55 — this does not require root, only for the user to enable it for itself.
loginctl enable-linger claude-orch

systemctl --user list-timers premonition-probe-afterhours.timer   # confirm next fire time
```

The timer fires `OnCalendar=Mon-Fri 15:55 America/New_York` and runs the service, which
invokes `bin/premonition-probe --probe a --start 15:55 --end 18:00`. Output goes to the
user journal (`journalctl --user -u premonition-probe-afterhours.service`) as well as the
usual `/srv/premonition/logs/probe-freshness-YYYY-MM-DD.log`. A run that starts late (e.g.
the box was asleep at 15:55) still covers today's window correctly as long as it starts
before 18:00 — `Persistent=true` on the timer catches a missed fire and runs it once the
system is back, rather than silently skipping the day.

If this box later gains a proper deploy account with root (matching CLAUDE.md's eventual
`/opt/premonition` runtime), migrate these two files to `/etc/systemd/system/` and drop the
`--user` flag everywhere above — the unit file contents do not need to change, since `%h`
still resolves correctly under `User=` on a system unit.

---

## Running the real pipeline (Phase 2b/2c)

Collectors (`seismo/`), scorer (`epicenter/`), and publisher now exist and run automatically.
Everything writes to `/srv/premonition/db/facts.sqlite` — a new database, separate from Phase
1's `probe.sqlite` (diagnostic data) and distinct from Supabase (published artifacts only).

### The known volume gap — read this before trusting an RVOL number

Found 2026-07-13, while building the collectors: **yfinance's free chart API reports zero
volume for every extended-hours (pre-market and post-market) 1-minute bar** — confirmed
across three tickers over 7 real trading days, and again in the after-hours probe run.
Price updates correctly outside regular hours; volume does not, from bars. This means:

- **Historical pre-market volume for ~120 sessions, as originally hoped, is not available
  from any free source found so far.** Not "capped at 60 days" — completely absent at every
  interval tried (1m, 5m, 15m).
- The only remaining candidate is the quoteSummary `preMarketVolume` field. Its behavior
  during a REAL pre-market state was unknown until the `premonition-probe-premarket.timer`
  (08:00-09:30 ET, installed 2026-07-13) actually observes one live.
- `seismo/facts_db.py`'s `premarket_volume_history` table has no backfill — it starts empty
  and builds a real baseline one trading day at a time, forward from today. The scorer
  requires `min_rvol_history_days` (see `weights.yaml`) real rows before it will compute
  RVOL for a ticker; until then RVOL is `insufficient_history`, never guessed.

Check `probes/probe.sqlite`'s `probe_field_behavior` freshness page (`/freshness` in the
dashboard) after a pre-market run to see what was actually observed.

### Collectors

```bash
source .venv/bin/activate
set -a; source /etc/premonition/env; set +a

python3 -m seismo.collect_daily --days 90       # daily OHLCV + ATR14 + typical gap %, all 87 resolved tickers
python3 -m seismo.collect_quotes                # live snapshot: price, gap %, premarket high/low/volume
python3 -m seismo.collect_fundamentals          # float, short interest, company name
python3 -m seismo.collect_earnings              # yfinance + Finnhub, disagreement flagged not averaged
python3 -m seismo.collect_catalysts             # Finnhub company-news (primary) + yfinance .news (fallback)
python3 -m seismo.collect_macro                 # BTC, ETH, ES=F, NQ=F, ^N225, ^TWII, ^GDAXI, DX-Y.NYB
```

Every collector takes `--dry-run` (limits to 3 tickers, useful after touching adapter code).
`collect_earnings` and `collect_catalysts` pace themselves at ~1 req/sec against Finnhub's
free-tier rate limit — expect the full 87-ticker run to take a minute or two, not instant.

### Scorer, gate, and publisher

```bash
python3 -m epicenter.score            # prints candidates/gate/picks, writes nothing
python3 -m epicenter.publish_lock --dry-run   # same, plus the exact brief/pick payload
python3 -m epicenter.publish_lock             # writes to Supabase: briefs (shadow_mode=true) + picks
```

Zero picks is a valid, expected outcome any time pre-market data genuinely isn't available
(e.g. run outside 08:00-09:30 ET) — the tradability gate correctly rejects everyone rather
than publish on missing data, and a `briefs` row is still written so the run is a recorded
fact, not silence.

`tradability.yaml` holds the hard floors; `weights.yaml` holds the scorer's feature weights
and the continuation-probability heuristic — both are starting values, not fitted to
anything (the backtest gate has not run). Six picks max, two per cluster max, `recent_ipo`
tickers (SPCX, SNDK) excluded from ranking entirely rather than given a fabricated
normalized score.

### Grader

```bash
python3 -m epicenter.grade --dry-run   # today only — needs real 1m bars, only available same-day
python3 -m epicenter.grade             # writes grades + baselines to Supabase
```

Must run the same day as the session it's grading (yfinance's 1-minute bars only go back 8
days, and the grader needs the ACTUAL 09:30-09:35 window). Grades every resolved ticker's
09:30-09:35 opening range — not just picks — since that's also what the two baselines
("biggest overnight movers," "most overnight trading") need to know what they'd have shown.

### Automatic schedule

Five systemd user timers now run Mon-Fri, ET, automatically (`systemctl --user list-timers`):

| Time | Timer | What |
|---|---|---|
| 08:00 | `premonition-probe-premarket` | Phase 1 Probe A, live — the original pre-market latency/volume test |
| 08:30 | `premonition-draft` | refreshes all collectors |
| 09:15 | `premonition-lock` | fresh quote snapshot, scored, published (shadow mode) |
| 15:55 | `premonition-probe-afterhours` | Phase 1c after-hours freeze probe |
| 16:15 | `premonition-grade` | grades today's picks + both baselines |

Install/reinstall any of these the same way as the after-hours timer above: copy the
`.service`/`.timer` pair from `systemd/` into `~/.config/systemd/user/`, `daemon-reload`,
`enable --now`.

### Pending Supabase migrations

Three migrations exist in `supabase/migrations/` and must be run in the SQL Editor, in
order, before the dashboard's home page or the publisher will fully work:

1. `0001_dashboard_publishing.sql` — `probe_source_freshness`, `probe_field_behavior` (Phase 2a)
2. `0002_shadow_mode.sql` — `briefs.shadow_mode`, renames `baselines.naive_gap_top5` →
   `naive_gap_top6` and `naive_rvol_top5` → `naive_rvol_top6`
3. `0003_pick_card_fields.sql` — `picks.score_breakdown`, `picks.company_name`

### Dashboard write path (ticker editing on /health)

Three server-only environment variables must be set in the Vercel project dashboard
(Settings → Environment Variables) — never committed, unlike the anon key:

- `SUPABASE_SERVICE_ROLE_KEY` — same value as `/etc/premonition/env`
- `EDIT_PASSPHRASE` — chosen by whoever will edit the watchlist from the dashboard
- `FINNHUB_API_KEY` — same value as `/etc/premonition/env`, used server-side for live
  ticker validation on Add/Fix

The passphrase gates an httpOnly, `Secure`, `SameSite=Strict` session cookie (stateless
HMAC, 4-hour expiry — see `web/src/lib/session.ts`). All writes go through
`web/src/pages/api/tickers.ts`, which holds the service_role key server-side only
(`web/src/lib/supabaseServiceOnly.ts` — must never be imported from anything that runs in
the browser) and only ever INSERTs into `watchlist_events`, never updates or deletes.
