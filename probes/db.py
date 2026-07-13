"""probes/db.py — schema and connection for probe.sqlite.

Phase 1 (probes) only. One wide table, `observations`, shared by both probes.
Every row is a single raw hit against a single source for a single ticker at
a single moment. Nothing in here aggregates, averages, or interprets — that
is explicitly out of scope for this phase. See CLAUDE.md: "You never produce
a number. You only report numbers that exist in facts.sqlite" — the probe
equivalent of that rule is "you never smooth a number before it hits disk."

Column notes:
  fetched_at   — OUR clock (UTC, ISO 8601), set the moment we received the
                 response. Always present, even on error.
  source_ts    — the SOURCE's own claimed timestamp for the datapoint,
                 converted to UTC ISO 8601 where the source gives one.
                 NULL if the source didn't provide one.
  source_ts_raw— the source's timestamp exactly as given (e.g. a raw epoch
                 int, or a string), before any conversion, so a conversion
                 bug never silently destroys the ground truth.
  status       — 'ok' | 'error' | 'no_data' | 'rate_limited'. A row is
                 written for every attempt, including failures. A missing
                 value must never be recorded as a zero or simply omitted —
                 it is recorded as a row with status != 'ok' and an `error`.
  volume_field — which field on the source's raw payload `volume` was read
                 from (sources disagree on what "volume" means — a 1-minute
                 bar volume is not a cumulative session volume, and
                 conflating them would be exactly the kind of silent
                 aggregation this phase must not do).
  spread_proxy* — Probe B only. See probes/friction.py for the formula and
                 its version tag (spread_proxy_formula), so a future change
                 to the formula never gets confused with old rows.
  raw_payload  — the full raw response, JSON-encoded, always populated
                 (an empty-but-valid JSON object at minimum). This is the
                 source of truth; every other column is a convenience
                 extract from it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = "/srv/premonition/db/probe.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    probe                TEXT    NOT NULL,   -- 'freshness' | 'friction'
    source               TEXT    NOT NULL,   -- 'yfinance_quote' | 'yfinance_bars_1m' | 'yfinance_daily' | 'finnhub_quote'
    ticker               TEXT    NOT NULL,
    fetched_at           TEXT    NOT NULL,   -- our clock, UTC ISO 8601
    source_ts            TEXT,               -- source's claimed timestamp, converted to UTC ISO 8601
    source_ts_raw         TEXT,               -- source's claimed timestamp, unconverted
    status               TEXT    NOT NULL,   -- 'ok' | 'error' | 'no_data' | 'rate_limited'
    http_status          INTEGER,
    error                TEXT,
    market_state         TEXT,               -- e.g. PRE / REGULAR / POST, if the source reports it
    price                REAL,
    volume               REAL,
    volume_field         TEXT,               -- which raw field `volume` was read from
    bid                  REAL,
    ask                  REAL,
    bid_size             REAL,
    ask_size             REAL,
    day_high             REAL,               -- most recent completed daily bar
    day_low              REAL,
    day_close            REAL,
    day_volume           REAL,
    day_bar_date         TEXT,               -- calendar date the day_* fields refer to
    spread_proxy         REAL,               -- (day_high - day_low) / day_volume
    spread_proxy_pct      REAL,               -- (day_high - day_low) / day_close * 100
    spread_proxy_formula TEXT,               -- versioned formula id, e.g. 'range_over_volume_v1'
    raw_payload          TEXT    NOT NULL,   -- full raw response, JSON-encoded
    created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_observations_probe_ticker_time
    ON observations (probe, ticker, fetched_at);

CREATE INDEX IF NOT EXISTS idx_observations_source
    ON observations (source, fetched_at);

CREATE TABLE IF NOT EXISTS rate_limit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT    NOT NULL,
    ticker      TEXT,
    fetched_at  TEXT    NOT NULL,
    http_status INTEGER,
    detail      TEXT
);
"""


def connect(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


OBSERVATION_COLUMNS = [
    "probe", "source", "ticker", "fetched_at", "source_ts", "source_ts_raw",
    "status", "http_status", "error", "market_state", "price", "volume",
    "volume_field", "bid", "ask", "bid_size", "ask_size", "day_high",
    "day_low", "day_close", "day_volume", "day_bar_date", "spread_proxy",
    "spread_proxy_pct", "spread_proxy_formula", "raw_payload",
]


def insert_observation(conn: sqlite3.Connection, row: dict) -> None:
    """Insert one raw observation. `row` may omit any column not in
    OBSERVATION_COLUMNS's required set — missing keys become NULL, never 0."""
    cols = OBSERVATION_COLUMNS
    values = [row.get(c) for c in cols]
    placeholders = ", ".join(["?"] * len(cols))
    conn.execute(
        f"INSERT INTO observations ({', '.join(cols)}) VALUES ({placeholders})",
        values,
    )


def log_rate_limit(conn: sqlite3.Connection, source: str, ticker: str | None,
                    fetched_at: str, http_status: int | None, detail: str) -> None:
    conn.execute(
        "INSERT INTO rate_limit_events (source, ticker, fetched_at, http_status, detail) "
        "VALUES (?, ?, ?, ?, ?)",
        (source, ticker, fetched_at, http_status, detail),
    )
