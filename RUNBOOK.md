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
