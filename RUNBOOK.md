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
