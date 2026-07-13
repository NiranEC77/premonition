"""seismo/facts_db.py — schema and connection for facts.sqlite.

System of record for collected facts (DESIGN.md's "SQLite on the agents
laptop as system of record"), distinct from Phase 1's probe.sqlite (which
was diagnostic latency/friction evidence, not a feature store). Same rule as
everywhere else in this project: every value carries its source and
fetched_at, and a missing value is a NULL row, never a zero.

Known, load-bearing limitation (found 2026-07-13, see RUNBOOK.md): yfinance's
free chart API reports ZERO volume for every extended-hours (pre-market and
post-market) minute bar — confirmed across three tickers over 7 real trading
days, and confirmed again in tonight's real after-hours probe run. Price
updates correctly in extended hours; volume does not, from bars. The only
other candidate is the quoteSummary `preMarketVolume` field, whose behavior
during a real PRE state is UNKNOWN until tomorrow's live 08:00-09:30 ET probe
run. `quotes.premarket_volume_source` records which method (if either)
actually produced a number, so this is never silently faked.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = "/srv/premonition/db/facts.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_bars (
    ticker      TEXT    NOT NULL,
    date        TEXT    NOT NULL,   -- YYYY-MM-DD, trading date (ET)
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      REAL,
    source      TEXT    NOT NULL,
    fetched_at  TEXT    NOT NULL,
    PRIMARY KEY (ticker, date)
);

-- Overwritten each collector run: one row per ticker, derived from daily_bars.
-- Formula is versioned (stats_formula) so a future change never gets confused
-- with rows computed under an older definition.
CREATE TABLE IF NOT EXISTS daily_stats (
    ticker              TEXT    PRIMARY KEY,
    atr14               REAL,               -- 14-day average true range
    avg_dollar_vol20    REAL,               -- 20-day average close * volume
    typical_gap_pct     REAL,               -- mean |open/prev_close - 1| * 100, trailing window
    sample_days         INTEGER NOT NULL,   -- how many daily_bars rows this was computed from
    stats_formula       TEXT    NOT NULL,
    computed_at         TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS fundamentals (
    ticker          TEXT    PRIMARY KEY,
    company_name    TEXT,
    float_shares    REAL,
    shares_outstanding REAL,
    short_pct_float REAL,
    source          TEXT    NOT NULL,
    fetched_at      TEXT    NOT NULL
);

-- Append-only: headlines accumulate over time, most recent per ticker used by the scorer.
CREATE TABLE IF NOT EXISTS catalysts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker       TEXT    NOT NULL,
    headline     TEXT    NOT NULL,
    source       TEXT    NOT NULL,   -- 'yfinance_news' (Finnhub company-news when a key exists)
    source_url   TEXT,
    published_at TEXT,               -- source's claimed publish time, if given
    fetched_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_catalysts_ticker_time ON catalysts (ticker, fetched_at DESC);

-- One row per (ticker, source) so disagreement between sources is visible,
-- never averaged away.
CREATE TABLE IF NOT EXISTS earnings_dates (
    ticker         TEXT    NOT NULL,
    source         TEXT    NOT NULL,  -- 'yfinance' | 'finnhub'
    earnings_date  TEXT,              -- YYYY-MM-DD; NULL if the source has nothing
    error          TEXT,              -- why earnings_date is NULL, if it's not just "nothing scheduled"
    fetched_at     TEXT    NOT NULL,
    PRIMARY KEY (ticker, source)
);

-- Live snapshot used at lock time. Overwritten each run (one row per ticker).
CREATE TABLE IF NOT EXISTS quotes (
    ticker                   TEXT    PRIMARY KEY,
    market_state             TEXT,
    price                    REAL,
    prev_close               REAL,
    premarket_price          REAL,
    premarket_gap_pct        REAL,              -- (premarket_price - prev_close) / prev_close * 100
    premarket_high           REAL,              -- session-to-date high/low from today's 1m bars, 04:00-now
    premarket_low            REAL,
    premarket_volume         REAL,
    premarket_volume_source  TEXT,              -- 'quote_field' | 'bar_sum_completed' | NULL (unavailable)
    bid                      REAL,
    ask                      REAL,
    source                   TEXT    NOT NULL,
    fetched_at               TEXT    NOT NULL
);

-- Builds a REAL historical pre-market-volume baseline over time, one row per
-- (ticker, date), appended only when a genuine pre-market volume was
-- observed (market_state == 'PRE' and a value actually came back — see
-- collect_quotes.py). There is no way to backfill this from free sources
-- (see facts_db.py's module docstring) — it starts empty and accumulates one
-- real trading day at a time. The scorer requires a minimum number of rows
-- before it will compute RVOL for a ticker; until then, RVOL is
-- 'insufficient_history', not a guess.
CREATE TABLE IF NOT EXISTS premarket_volume_history (
    ticker      TEXT    NOT NULL,
    date        TEXT    NOT NULL,
    premarket_volume REAL NOT NULL,
    source      TEXT    NOT NULL,
    fetched_at  TEXT    NOT NULL,
    PRIMARY KEY (ticker, date)
);

-- Overnight world signals: BTC-USD, ETH-USD, ES=F, NQ=F, ^N225, ^TWII, ^GDAXI, DX-Y.NYB.
CREATE TABLE IF NOT EXISTS macro_quotes (
    symbol        TEXT    PRIMARY KEY,
    label         TEXT    NOT NULL,
    price         REAL,
    prev_close    REAL,
    change_pct    REAL,
    source        TEXT    NOT NULL,
    fetched_at    TEXT    NOT NULL
);
"""


# Columns added after a table's first CREATE. Same lesson as probes/db.py:
# CREATE TABLE IF NOT EXISTS only creates on a brand-new file — an existing
# facts.sqlite is otherwise left with the old columns and every insert fails.
_COLUMN_MIGRATIONS = {
    "earnings_dates": [("error", "TEXT")],
    "fundamentals": [("company_name", "TEXT")],
    "quotes": [("premarket_high", "REAL"), ("premarket_low", "REAL")],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _COLUMN_MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, sqltype in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sqltype}")


def connect(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn
